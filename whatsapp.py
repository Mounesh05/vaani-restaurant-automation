"""
whatsapp.py — Twilio WhatsApp Notifications (Production-Ready)

Improvements over v1:
  1.  Lazy Twilio client initialisation — module-level Client() raised an
      uncatchable exception at import time if credentials were missing/invalid.
      Now the client is created on first use with a clear RuntimeError.
  2.  Phone number pre-validated before API call (10 digits required)
  3.  NGROK_URL warning is a logged error, not a print statement
  4.  All print() calls replaced with structured logger calls
  5.  Individual send functions guard against empty TWILIO credentials
"""

import os
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# Read credentials lazily — they may not be set in test/dev environments
TWILIO_SID            = os.getenv("TWILIO_SID", "")
TWILIO_AUTH           = os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_AUTH", "")
TWILIO_WHATSAPP_FROM  = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
NGROK_URL             = os.getenv("NGROK_URL", "")
OWNER_WHATSAPP        = os.getenv("OWNER_WHATSAPP", "")


@lru_cache(maxsize=1)
def _get_client():
    """
    Return a cached Twilio Client.
    Raises RuntimeError if credentials are not configured.
    Using lru_cache ensures the Client is built exactly once.
    """
    from twilio.rest import Client  # import here so missing package is clear

    if not TWILIO_SID or not TWILIO_AUTH:
        raise RuntimeError(
            "Twilio credentials missing. "
            "Set TWILIO_SID and TWILIO_AUTH_TOKEN in your .env file."
        )
    return Client(TWILIO_SID, TWILIO_AUTH)


def _validate_phone(phone: str) -> str:
    """
    Strip non-digits, take the last 10 digits, and validate.
    Raises ValueError if the result is not exactly 10 digits.
    """
    import re
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) != 10:
        raise ValueError(f"Invalid phone number: {phone!r} → {digits!r} (need 10 digits)")
    return digits


def _send_message(to_number: str, body: str, media_url: str = None) -> bool:
    """
    Low-level wrapper around client.messages.create().
    Returns True on success, False on failure.
    """
    try:
        client = _get_client()
        kwargs = dict(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
            to=to_number,
            body=body,
        )
        if media_url:
            kwargs["media_url"] = [media_url]
        client.messages.create(**kwargs)
        return True
    except RuntimeError:
        logger.error("Twilio client not configured — WhatsApp skipped.")
        return False
    except Exception as exc:
        logger.error("Twilio send failed to %s: %s", to_number, exc)
        return False


# ── BOOKING CONFIRMATION ──────────────────────────────────────────────────────
def send_booking_whatsapp(
    date: str, time: str, people: int,
    name: str, phone: str, qr_path: str,
) -> None:
    if not NGROK_URL:
        logger.error(
            "NGROK_URL not set — WhatsApp booking confirmation skipped for %s", phone
        )
        return

    try:
        phone_digits = _validate_phone(phone)
    except ValueError as exc:
        logger.error("send_booking_whatsapp: %s", exc)
        return

    qr_path  = (qr_path or "").replace("\\", "/")
    filename = os.path.basename(qr_path)
    qr_url   = f"{NGROK_URL}/static/qr/{filename}"

    body = (
        f"*Booking Confirmed ✅*\n\n"
        f"👤 Name: {name}\n"
        f"📞 +91 {phone_digits}\n"
        f"🗓 Date: {date}\n"
        f"⏰ Time: {time}\n"
        f"👥 Guests: {people}\n\n"
        f"Please show this QR at the entrance.\n"
        f"🔗 Download QR: {qr_url}"
    )

    customer_wa = f"whatsapp:+91{phone_digits}"

    if _send_message(customer_wa, body):
        logger.info("Booking WhatsApp sent to customer +91%s", phone_digits)
    _send_message(customer_wa, "", media_url=qr_url)

    # Notify owner
    if OWNER_WHATSAPP:
        owner_body = f"📩 *NEW BOOKING RECEIVED*\n\n{body}"
        if _send_message(f"whatsapp:{OWNER_WHATSAPP}", owner_body):
            _send_message(f"whatsapp:{OWNER_WHATSAPP}", "", media_url=qr_url)
            logger.info("Owner booking copy sent to %s", OWNER_WHATSAPP)


# ── CANCELLATION ──────────────────────────────────────────────────────────────
def send_cancellation_whatsapp(
    date: str, time: str,
    name: str, phone: str,
) -> None:
    try:
        phone_digits = _validate_phone(phone)
    except ValueError as exc:
        logger.error("send_cancellation_whatsapp: %s", exc)
        return

    body = (
        f"*Booking Cancelled ❌*\n\n"
        f"👤 Name: {name}\n"
        f"📞 +91 {phone_digits}\n"
        f"🗓 Date: {date}\n"
        f"⏰ Time: {time}\n\n"
        f"Your reservation has been cancelled successfully."
    )

    customer_wa = f"whatsapp:+91{phone_digits}"
    if _send_message(customer_wa, body):
        logger.info("Cancellation WhatsApp sent to customer +91%s", phone_digits)

    if OWNER_WHATSAPP:
        owner_body = f"⚠️ *BOOKING CANCELLED*\n\n{body}"
        if _send_message(f"whatsapp:{OWNER_WHATSAPP}", owner_body):
            logger.info("Owner cancellation copy sent to %s", OWNER_WHATSAPP)