"""Safety gates. Nothing in this module reads settings.yaml: these cannot be configured or disabled."""
from . import states
from .db import Database

CONFIRM_PHRASE = "APPROVE SEND"
PAUSE_KEY = "paused_reason"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def blocked_reason(db: Database, email: str) -> str | None:
    """Why this address must not receive mail right now (None = ok)."""
    email = normalize_email(email)
    if db.is_suppressed(email):
        return "suppressed"
    if db.real_send_exists(email):
        return "duplicate_send"
    return None


def is_paused(db: Database) -> str | None:
    return db.state_get(PAUSE_KEY)


def pause(db: Database, reason: str) -> None:
    db.state_set(PAUSE_KEY, reason)


def resume(db: Database) -> None:
    db.state_delete(PAUSE_KEY)


def _suppress_and_quarantine(db: Database, email: str, reason: str) -> None:
    email = normalize_email(email)
    db.suppress(email, reason)
    for row in db.conn.execute("SELECT id, status FROM leads WHERE email=?", (email,)).fetchall():
        if states.QUARANTINED in states.ALLOWED[row["status"]]:
            db.transition(row["id"], states.QUARANTINED, reason)


def unsubscribe(db: Database, email: str) -> None:
    _suppress_and_quarantine(db, email, "unsubscribe")


def record_bounce(db: Database, email: str) -> None:
    _suppress_and_quarantine(db, email, "bounce")
    pause(db, f"bounce from {normalize_email(email)}")


def record_complaint(db: Database, email: str) -> None:
    _suppress_and_quarantine(db, email, "complaint")
    pause(db, f"complaint from {normalize_email(email)}")
