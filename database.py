"""
database.py — VAANI Booking Database (Production-Ready)

Improvements over v1:
  1.  Added composite index on (date, time, status) — critical for check() and
      admin queries that filter by date/time/status simultaneously
  2.  Added individual indexes on phone, date, status for admin search/filter
  3.  Added CHECK constraint on status column — database enforces valid values
  4.  Added NOT NULL constraints on all required booking columns
  5.  Thread-safe connection creation (check_same_thread=False with WAL mode)
      — safe because each call creates its own short-lived connection
"""

import sqlite3
import os
import logging

from whatsapp import send_booking_whatsapp, send_cancellation_whatsapp
from qr_tool import generate_qr

DB           = os.getenv("DB_PATH", "bookings.db")
MAX_CAPACITY = int(os.getenv("MAX_RESTAURANT_CAPACITY", 5))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    con = sqlite3.connect(DB, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def _build_admin_filters(query="", status="all", date_from="", date_to=""):
    clauses: list = []
    params:  list = []

    query     = (query     or "").strip()
    status    = (status    or "all").strip().lower()
    date_from = (date_from or "").strip()
    date_to   = (date_to   or "").strip()

    if query:
        q = f"%{query}%"
        clauses.append("(name LIKE ? OR phone LIKE ?)")
        params.extend([q, q])

    if status in ("confirmed", "cancelled"):
        clauses.append("status = ?")
        params.append(status)

    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)

    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def init_db() -> None:
    with get_connection() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                date    TEXT    NOT NULL,
                time    TEXT    NOT NULL,
                people  INTEGER NOT NULL,
                name    TEXT    NOT NULL,
                phone   TEXT    NOT NULL,
                status  TEXT    NOT NULL DEFAULT 'confirmed'
                            CHECK(status IN ('confirmed', 'cancelled'))
            )
        """)

        # ── Performance indexes ────────────────────────────────────────────────
        # Composite index — primary query path for availability check + admin
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_date_time_status "
            "ON bookings(date, time, status)"
        )
        # Admin search by phone
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_phone "
            "ON bookings(phone)"
        )
        # Admin filter by date range
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_date "
            "ON bookings(date)"
        )
        # Admin filter by status
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_status "
            "ON bookings(status)"
        )

    logger.info("Database initialised: %s", DB)


init_db()


# ── AVAILABILITY CHECK ────────────────────────────────────────────────────────
def check(date: str, time: str) -> str:
    """Return 'ok' if the slot has capacity, 'full' otherwise."""
    with get_connection() as con:
        cur = con.execute(
            "SELECT COUNT(*) FROM bookings "
            "WHERE date=? AND time=? AND status='confirmed'",
            (date, time),
        )
        count = cur.fetchone()[0]
    return "full" if count >= MAX_CAPACITY else "ok"


# ── BOOK ──────────────────────────────────────────────────────────────────────
def book(date: str, time: str, people: int, name: str, phone: str):
    with get_connection() as con:
        cur = con.execute(
            "INSERT INTO bookings(date, time, people, name, phone, status) "
            "VALUES (?, ?, ?, ?, ?, 'confirmed')",
            (date, time, people, name, phone),
        )
        booking_id = cur.lastrowid

    # QR + WhatsApp — non-fatal; booking is already persisted
    qr_file = None
    try:
        qr_file = generate_qr(booking_id, f"{name}\n{date} {time}\nGuests: {people}")
        send_booking_whatsapp(date, time, people, name, phone, qr_file)
    except Exception as exc:
        logger.error(
            "Post-booking side-effects failed for booking #%d: %s", booking_id, exc
        )

    return (
        f"Booking confirmed for {people} guests on {date} at {time} under {name}.",
        qr_file,
    )


# ── ADMIN: READ ───────────────────────────────────────────────────────────────
def get_all():
    with get_connection() as con:
        cur = con.execute(
            "SELECT id, date, time, people, name, phone, status "
            "FROM bookings ORDER BY date, time"
        )
        return cur.fetchall()


def get_admin_bookings(
    query="", status="all", date_from="", date_to="",
    page=1, page_size=10,
):
    page      = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))

    where_sql, params = _build_admin_filters(query, status, date_from, date_to)

    with get_connection() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM bookings {where_sql}", params
        ).fetchone()[0]

        offset = (page - 1) * page_size
        rows = con.execute(
            "SELECT id, date, time, people, name, phone, status "
            f"FROM bookings {where_sql} "
            "ORDER BY date DESC, time DESC, id DESC "
            "LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()

    return rows, total


def get_admin_stats(today: str, query="", status="all", date_from="", date_to=""):
    where_sql, params = _build_admin_filters(query, status, date_from, date_to)

    with get_connection() as con:
        row = con.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN status='confirmed'  THEN 1 ELSE 0 END) AS confirmed, "
            "  SUM(CASE WHEN status='cancelled'  THEN 1 ELSE 0 END) AS cancelled, "
            "  SUM(CASE WHEN date=?              THEN 1 ELSE 0 END) AS today_count "
            f"FROM bookings {where_sql}",
            [today, *params],
        ).fetchone()

    return {
        "total":     int((row[0] if row and row[0] is not None else 0) or 0),
        "confirmed": int((row[1] if row and row[1] is not None else 0) or 0),
        "cancelled": int((row[2] if row and row[2] is not None else 0) or 0),
        "today":     int((row[3] if row and row[3] is not None else 0) or 0),
    }


def get_admin_bookings_for_export(query="", status="all", date_from="", date_to=""):
    where_sql, params = _build_admin_filters(query, status, date_from, date_to)

    with get_connection() as con:
        rows = con.execute(
            "SELECT id, date, time, people, name, phone, status "
            f"FROM bookings {where_sql} "
            "ORDER BY date DESC, time DESC, id DESC",
            params,
        ).fetchall()

    return rows


# ── ADMIN: WRITE ──────────────────────────────────────────────────────────────
def admin_cancel(id: int) -> None:
    with get_connection() as con:
        con.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (id,))


def admin_restore(id: int) -> None:
    with get_connection() as con:
        con.execute("UPDATE bookings SET status='confirmed' WHERE id=?", (id,))


# ── ADMIN: SEARCH ─────────────────────────────────────────────────────────────
def search_bookings(query: str):
    q = f"%{query}%"
    with get_connection() as con:
        cur = con.execute(
            "SELECT id, date, time, people, name, phone, status "
            "FROM bookings "
            "WHERE name LIKE ? OR phone LIKE ? "
            "ORDER BY date, time",
            (q, q),
        )
        return cur.fetchall()


# ── USER: CANCEL BY PHONE + DATE + TIME ──────────────────────────────────────
def cancel_by_phone_date_time(phone: str, date: str, time: str) -> str:
    # Normalise phone — keep last 10 digits only
    phone = re.sub(r"\D", "", phone)[-10:] if phone else ""
    if len(phone) != 10:
        return "That phone number does not look right. Please check and try again."

    with get_connection() as con:
        cur = con.execute(
            "SELECT id, name FROM bookings "
            "WHERE phone=? AND date=? AND time=? AND status='confirmed'",
            (phone, date, time),
        )
        row = cur.fetchone()

        if not row:
            return "I could not find an active booking matching those details."

        booking_id, name = row
        con.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,)
        )

    try:
        send_cancellation_whatsapp(date, time, name, phone)
    except Exception as exc:
        logger.error(
            "Cancellation WhatsApp failed for booking #%d: %s", booking_id, exc
        )

    return "Your booking has been cancelled successfully."


# ── Lazy import to avoid circular dependency on module-level ──────────────────
import re  # noqa: E402  (used in cancel_by_phone_date_time above)