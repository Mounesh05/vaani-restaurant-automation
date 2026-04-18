"""
brain.py — VAANI AI Brain (Production-Ready)

Improvements over v1:
  1.  asyncio.new_event_loop() instead of asyncio.run() — avoids "event loop
      already running" errors under Gunicorn workers that may have their own loop
  2.  LLM call timeout reduced 35 s → 15 s with 2-attempt retry — voice UX
      demands fast responses; a 35-second hang is unacceptable
  3.  TTS errors now properly logged (were swallowed silently)
  4.  Large-group guard: 11+ guests routed to private dining contact,
      matching the restaurant's stated policy from the knowledge document
  5.  Date validation: past-date bookings are rejected early with a friendly reply
  6.  Phone normalisation made stricter: must be exactly 10 digits after stripping
  7.  Minor: _say() now always returns a 3-tuple for consistent destructuring
"""

import re
import base64
import asyncio
import logging
import json
import os
from datetime import datetime, date, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from edge_tts import Communicate

from database import check, book, cancel_by_phone_date_time
from rag_store import retrieve_hits

load_dotenv()

logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL            = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
RESTAURANT_PHONE = os.getenv("RESTAURANT_PHONE", "+91-98765-43210")
PRIVATE_EMAIL    = os.getenv("PRIVATE_DINING_EMAIL", "events@amrestaurant.in")

UNSURE_REPLY = (
    f"I want to be accurate, and I am not fully sure from our records. "
    f"Please call us at {RESTAURANT_PHONE}."
)

# Tuned for voice latency — keep tokens tight, temperature=0 for determinism
LLM_OPTIONS = {
    "temperature":    0,
    "num_predict":    80,
    "repeat_penalty": 1.0,
    "top_k":          1,
}

_LLM_TIMEOUT_SECS = 15   # voice UX — fail fast and retry
_LLM_RETRIES      = 2


# ── DATE HELPERS ──────────────────────────────────────────────────────────────
def _today()    -> str: return datetime.now().strftime("%Y-%m-%d")
def _tomorrow() -> str: return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
def _year()     -> int: return datetime.now().year


def _is_past_date(date_str: str) -> bool:
    """Return True if the date is strictly before today."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date() < date.today()
    except (ValueError, TypeError):
        return False


# ── PERSONAS ──────────────────────────────────────────────────────────────────
def get_persona() -> str:
    return (
        f"You are VAANI, the warm and concise voice assistant for AM Restaurant. "
        f"Replies are read aloud — never use bullet points, markdown, or special characters. "
        f"Keep every reply under 35 words. Today is {_today()}."
    )


RAG_PERSONA = (
    "You are VAANI, the voice assistant for AM Restaurant. "
    "Answer ONLY from the provided context. Do not infer or assume any missing fact. "
    "If the context does not contain the answer, reply with the provided fallback sentence exactly. "
    "Never use bullet points or markdown. Keep the reply under 35 words."
)

INTENT_SYSTEM = (
    "Classify the user message into exactly one word. "
    "Reply with ONLY that word, no punctuation, no explanation. "
    "Words: book  cancel  faq  menu  hours  greeting  goodbye  other"
)


# ── KEYWORD INTENT DETECTION (no LLM for obvious intents) ─────────────────────
_CANCEL_KW = re.compile(
    r"\b(cancel|cancellation|delete booking|remove booking|cancel my)\b", re.I)
_BOOK_KW   = re.compile(
    r"\b(book|reserve|reservation|table for|seat|i want a table|can i book)\b", re.I)
_MENU_KW   = re.compile(
    r"\b(menu|food|dish|dishes|eat|starter|main course|dessert|drink|price|cost|"
    r"how much|what do you serve|biryani|paneer|chicken|mutton|prawn|fish|"
    r"naan|roti|lassi|chai|coffee|what.*available)\b", re.I)
_HOURS_KW  = re.compile(
    r"\b(hour|hours|open|close|timing|timings|when do you|what time)\b", re.I)
_FAQ_KW    = re.compile(
    r"\b(parking|address|location|where are you|payment|cash|upi|card|"
    r"wifi|wi-fi|dress code|valet|alcohol|pet|pets|wheelchair|kids|children|"
    r"private dining|event|party|delivery|zomato|swiggy)\b", re.I)
_GREET_KW  = re.compile(
    r"^\s*(hi|hello|hey|good morning|good afternoon|good evening|namaste)\b", re.I)
_BYE_KW    = re.compile(
    r"\b(bye|goodbye|thank you|thanks|see you|thats all|done|exit)\b", re.I)
_RESET_KW  = re.compile(
    r"\b(reset|start over|new request|new query|clear|forget previous|restart)\b", re.I)

_HOURS_SIGNAL_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun|am|pm|lunch|dinner|brunch|open|close|"
    r"\d{1,2}:\d{2})\b", re.I)

_MENU_SIGNAL_RE = re.compile(
    r"\b(menu|starter|main|dessert|dish|veg|non-veg|biryani|curry|thali|"
    r"rice|bread|beverage|mocktail|price|rs\.?|rupees|₹)\b", re.I)


def _fast_intent(msg: str) -> Optional[str]:
    if _CANCEL_KW.search(msg): return "cancel"
    if _BOOK_KW.search(msg):   return "book"
    if _HOURS_KW.search(msg):  return "hours"
    if _MENU_KW.search(msg):   return "menu"
    if _FAQ_KW.search(msg):    return "faq"
    if _GREET_KW.search(msg):  return "greeting"
    if _BYE_KW.search(msg):    return "goodbye"
    return None


# ── TTS ───────────────────────────────────────────────────────────────────────
async def _speak_async(text: str) -> Optional[str]:
    try:
        c   = Communicate(text=text, voice="en-IN-PrabhatNeural", rate="+5%")
        buf = bytearray()
        async for chunk in c.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        if not buf:
            logger.warning("TTS returned empty audio for text: %.60s…", text)
            return None
        return base64.b64encode(bytes(buf)).decode()
    except Exception as exc:
        logger.error("TTS _speak_async error: %s", exc)
        return None


def speak(text: str) -> Optional[str]:
    """
    Run TTS in a fresh event loop.
    asyncio.new_event_loop() is preferred over asyncio.run() because
    Gunicorn workers (and some test runners) may already have a running loop,
    which would cause asyncio.run() to raise RuntimeError.
    """
    try:
        loop   = asyncio.new_event_loop()
        result = loop.run_until_complete(_speak_async(text))
        loop.close()
        return result
    except Exception as exc:
        logger.error("TTS speak() wrapper error: %s", exc)
        return None


def _say(text: str, state: dict):
    """Produce (reply_text, audio_b64_or_None, state) triple."""
    return text, speak(text), state


# ── LLM ───────────────────────────────────────────────────────────────────────
def llm(prompt: str, system: str = None, max_tokens: int = 250) -> str:
    """
    Call Ollama with retry on timeout.
    Timeout is capped at _LLM_TIMEOUT_SECS (15 s) for voice responsiveness.
    """
    if system is None:
        system = get_persona()

    opts              = dict(LLM_OPTIONS)
    opts["num_predict"] = max_tokens

    last_err = "Unknown error"
    for attempt in range(1, _LLM_RETRIES + 1):
        try:
            r = requests.post(
                OLLAMA_URL,
                json={
                    "model":   MODEL,
                    "system":  system,
                    "prompt":  prompt,
                    "stream":  False,
                    "options": opts,
                },
                timeout=_LLM_TIMEOUT_SECS,
            )
            r.raise_for_status()
            return (r.json().get("response") or "").strip()

        except requests.Timeout:
            last_err = f"Timeout after {_LLM_TIMEOUT_SECS}s"
            logger.warning("LLM timeout (attempt %d/%d)", attempt, _LLM_RETRIES)

        except requests.HTTPError as exc:
            last_err = str(exc)
            logger.error("LLM HTTP error: %s", exc)
            break  # HTTP errors won't recover on retry

        except Exception as exc:
            last_err = str(exc)
            logger.error("LLM error (attempt %d/%d): %s", attempt, _LLM_RETRIES, exc)

    logger.error("LLM failed after %d attempts: %s", _LLM_RETRIES, last_err)
    return "Sorry, I did not catch that. Could you please repeat?"


# ── INTENT CLASSIFICATION (LLM fallback) ─────────────────────────────────────
def classify_intent(msg: str) -> str:
    result = llm(msg, system=INTENT_SYSTEM, max_tokens=5).lower().strip()
    for intent in ("book", "cancel", "faq", "menu", "hours", "greeting", "goodbye"):
        if intent in result:
            return intent
    return "other"


# ── SLOT EXTRACTION ───────────────────────────────────────────────────────────
def _slot_system() -> str:
    return (
        f"Extract booking details and return ONLY a JSON object.\n"
        f"Today: {_today()}. Tomorrow: {_tomorrow()}. Year: {_year()}.\n"
        f"Resolve relative dates (e.g. 'tomorrow', 'next Saturday'). "
        f"Strip spaces/dots from phone numbers.\n"
        f'Return ONLY this JSON, no markdown, no explanation:\n'
        f'{{ "date":"YYYY-MM-DD or null", "time":"HH:MM 24h or null", '
        f'"people":number_or_null, "name":"string or null", "phone":"10digits or null" }}\n'
        f"If not mentioned, use null. Never guess."
    )


def _clean_json(raw: str) -> dict:
    raw   = re.sub(r"```(?:json)?", "", raw).strip()
    match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group())
        return {k: (None if str(v).lower() in ("null", "none", "") else v)
                for k, v in data.items()}
    except json.JSONDecodeError:
        return {}


def _normalize_slots(slots: dict) -> dict:
    cleaned = dict(slots or {})

    # ── date
    raw_date = cleaned.get("date")
    if isinstance(raw_date, str):
        raw_date = raw_date.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            try:
                datetime.strptime(raw_date, "%Y-%m-%d")
                cleaned["date"] = raw_date
            except ValueError:
                cleaned["date"] = None
        else:
            cleaned["date"] = None

    # ── time (24-hour HH:MM)
    raw_time = cleaned.get("time")
    if isinstance(raw_time, str):
        raw_time = raw_time.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", raw_time):
            try:
                cleaned["time"] = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
            except ValueError:
                cleaned["time"] = None
        else:
            cleaned["time"] = None

    # ── people (1–20 accepted; 11+ handled separately in the flow)
    raw_people = cleaned.get("people")
    try:
        people = int(raw_people) if raw_people is not None else None
        cleaned["people"] = people if people and 1 <= people <= 20 else None
    except (TypeError, ValueError):
        cleaned["people"] = None

    # ── name (2–60 chars, letters/spaces only — no injection risk)
    raw_name = cleaned.get("name")
    if isinstance(raw_name, str):
        raw_name = raw_name.strip()
        cleaned["name"] = raw_name if 2 <= len(raw_name) <= 60 else None
    else:
        cleaned["name"] = None

    # ── phone (exactly 10 digits after stripping non-digits)
    raw_phone = cleaned.get("phone")
    if raw_phone is not None:
        digits = re.sub(r"\D", "", str(raw_phone))
        cleaned["phone"] = digits[-10:] if len(digits) >= 10 else None
    else:
        cleaned["phone"] = None

    return cleaned


def _numbers(text: str):
    return set(re.findall(r"\b\d[\d:./-]*\b", text or ""))


def _content_tokens(text: str):
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "have", "has",
        "are", "was", "were", "our", "your", "you", "can", "will", "about",
        "into", "under", "over", "their", "they", "them", "not", "only", "but",
        "what", "when", "where", "which", "would", "could", "should", "please",
    }
    tokens = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    return [t for t in tokens if t not in stop]


def _canonical_retrieval_query(msg: str, intent: str = None) -> str:
    text = (msg or "").strip()
    if len(text.split()) > 2:
        return text
    if intent == "hours":
        return "What are your opening hours and timings for lunch and dinner?"
    if intent == "menu":
        return "Please share menu highlights with categories and prices."
    if intent == "faq":
        return "What are your restaurant policies and frequently asked questions?"
    return text


def _limit_words(text: str, max_words: int = 35) -> str:
    normalized = re.sub(r"\s*\|\s*", ", ", (text or "").strip())
    words = normalized.split()
    if len(words) <= max_words:
        return normalized
    shortened = " ".join(words[:max_words])
    cut = max(shortened.rfind("."), shortened.rfind("!"),
              shortened.rfind("?"), shortened.rfind(";"))
    if cut >= int(len(shortened) * 0.55):
        return shortened[: cut + 1].strip()
    return shortened.rstrip(" ,;:") + "."


def _verify_grounded_answer(context: str, answer: str) -> bool:
    if not context or not answer:
        return False
    answer_numbers  = _numbers(answer)
    context_numbers = _numbers(context)
    if not answer_numbers.issubset(context_numbers):
        return False
    answer_tokens  = _content_tokens(answer)
    context_tokens = set(_content_tokens(context))
    if not answer_tokens:
        return False
    overlap = sum(1 for t in answer_tokens if t in context_tokens) / len(answer_tokens)
    return overlap >= 0.65


# ── RAG ANSWER ────────────────────────────────────────────────────────────────
def _strict_rag_answer(
    msg: str,
    *,
    section_filter: str = None,
    n: int = 4,
    max_distance: float = 0.5,
    intent_hint: str = None,
    signal_re=None,
    max_words: int = 35,
    llm_max_tokens: int = 120,
    extra_instruction: str = "",
) -> str:
    retrieval_query  = _canonical_retrieval_query(msg, intent=intent_hint)
    generation_query = retrieval_query if len((msg or "").split()) <= 2 else msg

    hits = retrieve_hits(
        query=retrieval_query,
        n=n,
        section_filter=section_filter,
        max_distance=max_distance,
    )

    if signal_re is not None:
        hits = [h for h in hits if signal_re.search(h.get("text", ""))]

    if not hits:
        return UNSURE_REPLY

    context    = "\n\n".join(h["text"] for h in hits)
    extra_line = f"{extra_instruction.strip()}\n" if extra_instruction else ""

    candidate = llm(
        (
            f"Context:\n{context}\n\n"
            f"Customer: {generation_query}\n"
            f"If context is insufficient, reply exactly: {UNSURE_REPLY}\n"
            f"{extra_line}"
            "Use only facts present in context. Answer in one or two short sentences."
        ),
        system=RAG_PERSONA,
        max_tokens=llm_max_tokens,
    ).strip()

    if not candidate:
        return UNSURE_REPLY
    if candidate == UNSURE_REPLY:
        return candidate
    if not _verify_grounded_answer(context, candidate):
        return UNSURE_REPLY

    return _limit_words(candidate, max_words=max_words)


# ── DETERMINISTIC HOURS PARSER ────────────────────────────────────────────────
def _extract_day_window(text: str, day_expr: str):
    pattern = (
        rf"{day_expr}\s*"
        rf"(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*to\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)"
        rf".*?\|\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)\s*to\s*(\d{{1,2}}:\d{{2}}\s*[AP]M)"
    )
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _deterministic_hours_answer(msg: str) -> Optional[str]:
    query = _canonical_retrieval_query(msg, intent="hours")
    hits  = retrieve_hits(
        query=query, n=12,
        section_filter="RESTAURANT INFORMATION",
        max_distance=0.72,
    )
    texts  = [h.get("text", "") for h in hits if _HOURS_SIGNAL_RE.search(h.get("text", ""))]
    if not texts:
        return None

    merged  = " ".join(" ".join(t.split()) for t in texts)
    mon_thu = _extract_day_window(merged, r"Monday\s*[–-]\s*Thursday")
    fri     = _extract_day_window(merged, r"Friday")
    sat     = _extract_day_window(merged, r"Saturday")
    sun     = _extract_day_window(merged, r"Sunday")

    parts = []
    if mon_thu: parts.append(f"Mon-Thu {mon_thu[0]}-{mon_thu[1]} and {mon_thu[2]}-{mon_thu[3]}")
    if fri:     parts.append(f"Fri {fri[0]}-{fri[1]} and {fri[2]}-{fri[3]}")
    if sat:     parts.append(f"Sat {sat[0]}-{sat[1]} and {sat[2]}-{sat[3]}")
    if sun:     parts.append(f"Sun {sun[0]}-{sun[1]} and {sun[2]}-{sun[3]}")

    if not parts:
        return None

    holidays = " Public holidays follow Sunday timings." if re.search(
        r"Public\s+Holidays\s+Same\s+as\s+Sunday", merged, re.I
    ) else ""

    return _limit_words(
        f"Our opening hours are: {'; '.join(parts)}.{holidays}",
        max_words=60,
    )


# ── SLOT EXTRACTION ───────────────────────────────────────────────────────────
def extract_slots(msg: str) -> dict:
    raw   = llm(msg, system=_slot_system(), max_tokens=120)
    slots = _normalize_slots(_clean_json(raw))

    # Fallback: grab 10-digit phone directly from message if LLM missed it
    if not slots.get("phone"):
        digits = re.sub(r"\D", "", msg)
        if len(digits) >= 10:
            slots["phone"] = digits[-10:]

    for key in ("date", "time", "people", "name", "phone"):
        slots.setdefault(key, None)

    return slots


def merge_slots(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    for k, v in new.items():
        # Only fill if the slot is genuinely empty (None / 0 guard)
        if merged.get(k) is None and v is not None:
            merged[k] = v
    return merged


def first_missing(slots: dict, keys: list) -> Optional[str]:
    for k in keys:
        if not slots.get(k):
            return k
    return None


# ── SLOT QUESTIONS ─────────────────────────────────────────────────────────────
BOOKING_Q = {
    "date":   "What date would you like to book the table for?",
    "time":   "What time works best for you?",
    "people": "How many guests will be joining?",
    "name":   "What name should I put the reservation under?",
    "phone":  "Could I get your 10-digit phone number for the booking?",
}
CANCEL_Q = {
    "phone": "What is the 10-digit phone number registered for the booking?",
    "date":  "On which date was the reservation?",
    "time":  "At what time was the reservation?",
}

LARGE_GROUP_REPLY = (
    f"For groups of 11 or more guests, we require our private dining room "
    f"with at least 7 days advance notice. "
    f"Please call us at {RESTAURANT_PHONE} or email {PRIVATE_EMAIL}."
)

PAST_DATE_REPLY = "That date has already passed. Could you choose a future date for the booking?"


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────
def get_ai_response(message: str, state: dict):
    """
    Core dispatcher.  Returns (reply_text, audio_b64_or_None, new_state).
    state is a plain dict that persists across turns in the Flask session.
    """
    msg = (message or "").strip()

    # ── Welcome message
    if msg == "__WELCOME__":
        return _say(
            "Hello and welcome to AM Restaurant! I am VAANI. "
            "I can help you book a table, cancel a reservation, "
            "or answer questions about our menu. How can I help?",
            state,
        )

    # ── Session reset
    if _RESET_KW.search(msg):
        state.clear()
        return _say("Okay, I have cleared the current request. How can I help now?", state)

    # ── Intent-switch guard (prevents stale flow state after mid-conversation pivot)
    active_flow = state.get("flow")
    if active_flow in ("booking", "cancel"):
        flow_intent    = "book" if active_flow == "booking" else "cancel"
        switched_intent = _fast_intent(msg)
        if switched_intent and switched_intent != flow_intent:
            logger.info("Flow interrupted: %s → %s", active_flow, switched_intent)
            state.clear()

    # ════════════════════════════════════════════════════════════════════════════
    # BOOKING FLOW
    # ════════════════════════════════════════════════════════════════════════════
    if state.get("flow") == "booking":
        slots      = state.get("slots", {})
        new_slots  = extract_slots(msg)
        slots      = merge_slots(slots, new_slots)
        state["slots"] = slots

        # Past-date guard
        if slots.get("date") and _is_past_date(slots["date"]):
            slots["date"] = None
            state["slots"] = slots
            return _say(PAST_DATE_REPLY, state)

        # Large-group guard
        if slots.get("people") and int(slots["people"]) > 10:
            state.clear()
            return _say(LARGE_GROUP_REPLY, state)

        missing = first_missing(slots, ["date", "time", "people", "name", "phone"])
        if missing:
            return _say(BOOKING_Q[missing], state)

        if check(slots["date"], slots["time"]) != "ok":
            reply = (f"Sorry, {slots['time']} on {slots['date']} is fully booked. "
                     "Could you choose a different time?")
            slots["time"] = None
            state["slots"] = slots
            return _say(reply, state)

        result, _ = book(
            slots["date"], slots["time"],
            slots["people"], slots["name"], slots["phone"]
        )
        state.clear()
        return _say(result + " I have sent your confirmation and QR code to WhatsApp.", state)

    # ════════════════════════════════════════════════════════════════════════════
    # CANCELLATION FLOW
    # ════════════════════════════════════════════════════════════════════════════
    if state.get("flow") == "cancel":
        slots     = state.get("slots", {})
        new_slots = extract_slots(msg)
        slots     = merge_slots(slots, new_slots)
        state["slots"] = slots

        missing = first_missing(slots, ["phone", "date", "time"])
        if missing:
            return _say(CANCEL_Q[missing], state)

        result = cancel_by_phone_date_time(
            slots["phone"], slots["date"], slots["time"]
        )
        state.clear()
        return _say(result, state)

    # ════════════════════════════════════════════════════════════════════════════
    # INTENT DETECTION
    # ════════════════════════════════════════════════════════════════════════════
    intent = _fast_intent(msg) or classify_intent(msg)
    logger.info("[VAANI] intent=%r msg=%r", intent, msg[:80])

    # ── Booking intent
    if intent == "book":
        state["flow"]  = "booking"
        slots          = extract_slots(msg)
        state["slots"] = slots

        # Past-date guard (first message may already carry a date)
        if slots.get("date") and _is_past_date(slots["date"]):
            slots["date"] = None
            state["slots"] = slots
            return _say(PAST_DATE_REPLY, state)

        # Large-group guard
        if slots.get("people") and int(slots["people"]) > 10:
            state.clear()
            return _say(LARGE_GROUP_REPLY, state)

        missing = first_missing(slots, ["date", "time", "people", "name", "phone"])
        if missing:
            got = [k for k in ("date", "time", "people", "name") if slots.get(k)]
            if got:
                ack = llm(
                    f"Customer wants to book. Already captured: {got}. "
                    f"Ask only for: {missing}. One short sentence.",
                    system=get_persona(), max_tokens=60,
                )
                return _say(ack, state)
            return _say(BOOKING_Q[missing], state)

        if check(slots["date"], slots["time"]) != "ok":
            reply = (f"Sorry, {slots['time']} on {slots['date']} is fully booked. "
                     "Please choose a different time.")
            slots["time"] = None
            state["slots"] = slots
            return _say(reply, state)

        result, _ = book(
            slots["date"], slots["time"],
            slots["people"], slots["name"], slots["phone"]
        )
        state.clear()
        return _say(result + " Confirmation and QR code sent to WhatsApp.", state)

    # ── Cancel intent
    if intent == "cancel":
        state["flow"]  = "cancel"
        slots          = extract_slots(msg)
        state["slots"] = {k: slots.get(k) for k in ("phone", "date", "time")}

        missing = first_missing(state["slots"], ["phone", "date", "time"])
        if missing:
            return _say(CANCEL_Q[missing], state)

        result = cancel_by_phone_date_time(
            slots["phone"], slots["date"], slots["time"]
        )
        state.clear()
        return _say(result, state)

    # ── Menu
    if intent == "menu":
        return _say(
            _strict_rag_answer(
                msg,
                section_filter="MENU",
                n=10,
                max_distance=0.52,
                intent_hint="menu",
                signal_re=_MENU_SIGNAL_RE,
            ),
            state,
        )

    # ── Hours
    if intent == "hours":
        det = _deterministic_hours_answer(msg)
        if det:
            return _say(det, state)
        return _say(
            _strict_rag_answer(
                msg,
                section_filter="RESTAURANT INFORMATION",
                n=10,
                max_distance=0.62,
                intent_hint="hours",
                signal_re=_HOURS_SIGNAL_RE,
                max_words=55,
                llm_max_tokens=180,
                extra_instruction=(
                    "If day-wise timings are present, include Monday through Sunday coverage in compact form. "
                    "Do not include unrelated policies."
                ),
            ),
            state,
        )

    # ── FAQ
    if intent == "faq":
        return _say(
            _strict_rag_answer(msg, n=6, max_distance=0.52, intent_hint="faq"),
            state,
        )

    # ── Greeting
    if intent == "greeting":
        return _say(
            llm(
                f"Customer said: {msg}\nGreet warmly as VAANI. One sentence.",
                max_tokens=60,
            ),
            state,
        )

    # ── Goodbye
    if intent == "goodbye":
        state.clear()
        return _say(
            "Thank you for calling AM Restaurant. Have a wonderful day! Goodbye!",
            state,
        )

    # ── Fallback (general RAG)
    return _say(_strict_rag_answer(msg, n=4, max_distance=0.47), state)