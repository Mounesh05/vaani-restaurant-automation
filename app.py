"""
app.py — VAANI Flask Application (Production-Ready)

Security & Production Improvements over v1:
  1.  CSRF protection via Flask-WTF on all state-changing forms
  2.  Rate limiting via Flask-Limiter (per-IP)
  3.  Admin login brute-force lockout (5 attempts → 5-min ban)
  4.  SESSION_COOKIE_HTTPONLY / SAMESITE / SECURE flags
  5.  Security response headers (X-Content-Type-Options, X-Frame-Options, etc.)
  6.  Admin cancel/restore changed from GET → POST (prevents CSRF via link prefetch)
  7.  Input length cap on /send_message
  8.  FLASK_DEBUG controlled via env var — never True in production
  9.  /health endpoint for load-balancer / uptime monitoring
  10. Open-redirect hardening in admin redirect helper
"""

import os
import csv
import io
import time
import logging
from collections import defaultdict
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask, render_template, request, redirect,
    session, jsonify, url_for, Response, abort,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

from brain import get_ai_response
from database import (
    admin_cancel,
    admin_restore,
    get_admin_bookings,
    get_admin_bookings_for_export,
    get_admin_stats,
)
from rag_store import build_index

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── RAG index bootstrap ───────────────────────────────────────────────────────
if not os.path.exists("chroma_db"):
    logger.info("Building RAG index for the first time…")
    build_index()

# ── App factory ───────────────────────────────────────────────────────────────
app = Flask(__name__)

# Validate required env vars upfront — fail fast, never silently
_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
if not _secret or len(_secret) < 32:
    raise RuntimeError(
        "FLASK_SECRET_KEY missing or too short (≥32 chars required). "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

ADMIN_PIN: str = os.getenv("ADMIN_PIN", "").strip()
if not ADMIN_PIN:
    raise RuntimeError("ADMIN_PIN missing in .env")

IS_PRODUCTION = os.getenv("FLASK_ENV", "development").lower() == "production"

app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,   # HTTPS-only cookie in prod
    SESSION_COOKIE_NAME="vaani_sess",
    PERMANENT_SESSION_LIFETIME=3600,       # 1-hour idle timeout
    WTF_CSRF_TIME_LIMIT=3600,
    MAX_CONTENT_LENGTH=64 * 1024,          # 64 KB max request body
)

# ── CSRF ──────────────────────────────────────────────────────────────────────
csrf = CSRFProtect(app)

# ── Rate limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["300 per hour", "60 per minute"],
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
)

# ── Brute-force protection for admin login ────────────────────────────────────
_login_state: dict = defaultdict(lambda: {"fails": 0, "locked_until": 0.0})
_MAX_FAILS    = 5
_LOCKOUT_SECS = 300  # 5 minutes


def _is_locked(ip: str) -> bool:
    rec = _login_state[ip]
    if rec["locked_until"] > time.monotonic():
        return True
    return False


def _record_fail(ip: str) -> None:
    rec = _login_state[ip]
    rec["fails"] += 1
    if rec["fails"] >= _MAX_FAILS:
        rec["locked_until"] = time.monotonic() + _LOCKOUT_SECS
        rec["fails"] = 0
        logger.warning("Admin login locked for IP %s (%d failed attempts)", ip, _MAX_FAILS)


def _clear_fails(ip: str) -> None:
    _login_state.pop(ip, None)


# ── Security headers ──────────────────────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers["ngrok-skip-browser-warning"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Admin session guard ───────────────────────────────────────────────────────
@app.before_request
def enforce_admin_session():
    path = request.path
    if path.startswith("/admin") and not path.startswith("/admin/login"):
        if not session.get("admin_logged_in"):
            session.clear()
            return redirect(url_for("admin_login"))


# ── Health check (for load balancers / uptime monitors) ──────────────────────
@app.route("/health")
@csrf.exempt
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"}), 200


# ── Voice assistant ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/send_message", methods=["POST"])
@csrf.exempt          # JSON API — protected by SameSite cookie + rate limit
@limiter.limit("60 per minute; 300 per hour")
def send_message():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "message is required"}), 400
    if len(msg) > 500:
        return jsonify({"error": "message too long (max 500 chars)"}), 400

    if "state" not in session:
        session["state"] = {}

    try:
        reply, audio, new_state = get_ai_response(msg, session["state"])
    except Exception as exc:
        logger.exception("get_ai_response crashed: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    session["state"] = new_state
    session.modified = True
    return jsonify({"response": reply, "audio": audio})


@app.route("/reset_state", methods=["POST"])
@csrf.exempt
@limiter.limit("30 per minute")
def reset_state():
    session["state"] = {}
    session.modified = True
    return jsonify({"ok": True})


# ── Admin login ───────────────────────────────────────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def admin_login():
    if request.method == "POST":
        ip = get_remote_address()

        if _is_locked(ip):
            return render_template(
                "admin_login.html",
                error="Too many failed attempts. Try again in 5 minutes."
            ), 429

        if request.form.get("pin") == ADMIN_PIN:
            _clear_fails(ip)
            session.clear()
            session["admin_logged_in"] = True
            logger.info("Admin login success from %s", ip)
            return redirect(url_for("admin"))

        _record_fail(ip)
        logger.warning("Admin login failure from %s", ip)
        return render_template("admin_login.html", error="Incorrect PIN."), 401

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ── Admin dashboard ───────────────────────────────────────────────────────────
def _parse_admin_filters(source) -> dict:
    """Parse and sanitize all admin filter parameters."""
    query = (source.get("query") or "").strip()[:200]

    status = (source.get("status") or "all").strip().lower()
    if status not in ("all", "confirmed", "cancelled"):
        status = "all"

    date_from = (source.get("date_from") or "").strip()
    date_to   = (source.get("date_to")   or "").strip()

    # Validate date formats; reject silently-malformed values
    for label, val in (("date_from", date_from), ("date_to", date_to)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                if label == "date_from":
                    date_from = ""
                else:
                    date_to = ""

    try:
        page = max(1, int(source.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    try:
        page_size = int(source.get("page_size") or 10)
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in (10, 20, 50, 100):
        page_size = 10

    return {
        "query": query, "status": status,
        "date_from": date_from, "date_to": date_to,
        "page": page, "page_size": page_size,
    }


@app.route("/admin")
def admin():
    filters = _parse_admin_filters(request.args)

    bookings, total = get_admin_bookings(
        query=filters["query"], status=filters["status"],
        date_from=filters["date_from"], date_to=filters["date_to"],
        page=filters["page"], page_size=filters["page_size"],
    )

    total_pages = max(1, ((total - 1) // filters["page_size"]) + 1) if total else 1

    # Clamp page to valid range
    if filters["page"] > total_pages:
        filters["page"] = total_pages
        bookings, total = get_admin_bookings(
            query=filters["query"], status=filters["status"],
            date_from=filters["date_from"], date_to=filters["date_to"],
            page=filters["page"], page_size=filters["page_size"],
        )

    stats = get_admin_stats(
        today=date.today().isoformat(),
        query=filters["query"], status=filters["status"],
        date_from=filters["date_from"], date_to=filters["date_to"],
    )

    return render_template(
        "admin.html",
        bookings=bookings, stats=stats, filters=filters,
        page=filters["page"], page_size=filters["page_size"],
        total=total, total_pages=total_pages,
    )


# ── Admin cancel / restore — POST only (CSRF-protected) ──────────────────────
@app.route("/admin/cancel/<int:id>", methods=["POST"])
def admin_cancel_route(id: int):
    admin_cancel(id)
    logger.info("Admin cancelled booking id=%d", id)
    return _admin_redirect(request.args)


@app.route("/admin/restore/<int:id>", methods=["POST"])
def admin_restore_route(id: int):
    admin_restore(id)
    logger.info("Admin restored booking id=%d", id)
    return _admin_redirect(request.args)


def _admin_redirect(args) -> Response:
    """Safe redirect back to the admin dashboard, preserving filters."""
    next_url = (args.get("next") or "").strip()
    # Only redirect to relative paths within /admin (prevent open-redirect)
    if next_url.startswith("/admin") and not next_url.startswith("//"):
        return redirect(next_url)
    filters = _parse_admin_filters(args)
    return redirect(url_for("admin", **filters))


# ── Admin CSV export ──────────────────────────────────────────────────────────
@app.route("/admin/export")
def admin_export():
    filters = _parse_admin_filters(request.args)
    rows = get_admin_bookings_for_export(
        query=filters["query"], status=filters["status"],
        date_from=filters["date_from"], date_to=filters["date_to"],
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Date", "Time", "Guests", "Name", "Phone", "Status"])
    writer.writerows(rows)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bookings_export_{ts}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Error handlers ────────────────────────────────────────────────────────────
@app.errorhandler(CSRFError)
def csrf_error(e):
    logger.warning("CSRF validation failed: %s | path=%s", e, request.path)
    if request.path.startswith("/admin"):
        return render_template(
            "admin_login.html",
            error="Session expired or invalid request. Please log in again."
        ), 400
    return jsonify({"error": "CSRF validation failed"}), 400


@app.errorhandler(429)
def ratelimit_error(e):
    if request.path.startswith("/admin"):
        return render_template("admin_login.html", error=str(e.description)), 429
    return jsonify({"error": "Too many requests. Please slow down."}), 429


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(_e):
    return jsonify({"error": "Internal server error"}), 500


# ── Entrypoint (dev only — use wsgi.py + gunicorn for production) ─────────────
if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.getenv("PORT", 5000))
    app.run(debug=debug, host="127.0.0.1", port=port)