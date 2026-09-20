"""One SQLite database, one writer at a time (file lock). Records are appended and updated, never deleted."""
import fcntl
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import states

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  profile TEXT NOT NULL,
  company TEXT NOT NULL,
  segment TEXT NOT NULL,
  suburb TEXT NOT NULL,
  email TEXT NOT NULL,
  source_url TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  status TEXT NOT NULL,
  status_reason TEXT,
  duplicate_of INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS leads_status ON leads(profile, status);

CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  generator TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_log (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL,
  draft_id INTEGER,
  mailbox TEXT NOT NULL,
  to_email TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  message_id TEXT,
  dry_run INTEGER NOT NULL,
  result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS send_log_to ON send_log(to_email, dry_run);

CREATE TABLE IF NOT EXISTS suppression (
  email TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Added after the first release; applied with ALTER TABLE so existing ledgers keep their rows.
# They are provenance and grading columns on the same lead record — not a second store.
LEAD_EXTRA_COLUMNS = {
    "website": "TEXT",
    "phone": "TEXT",
    "evidence_url": "TEXT",          # the exact public page the contact was read from
    "provenance": "TEXT",            # which source/adapter produced this lead
    "verification": "TEXT",          # valid | invalid | risky | catch_all | unknown | unverified
    "verification_detail": "TEXT",
    "quality_score": "INTEGER",
    "quality_reason": "TEXT",
}


class DatabaseLocked(Exception):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Usage: with Database(path) as db: ...  — the lock is held for the whole block."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.conn: sqlite3.Connection | None = None
        self._lock_fh = None

    def __enter__(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self.lock_path, "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_fh.close()
            self._lock_fh = None
            raise DatabaseLocked(f"another leadmailer process holds {self.lock_path}")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        return self

    def __exit__(self, *exc):
        if self.conn:
            if exc[0] is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()
            self.conn = None
        if self._lock_fh:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(leads)")}
        for column, sql_type in LEAD_EXTRA_COLUMNS.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {sql_type}")

    # ---- leads -------------------------------------------------------------
    def insert_lead(self, profile, company, segment, suburb, email, source_url, observed_at,
                    status=states.CANDIDATE, reason=None, duplicate_of=None, extra: dict | None = None) -> int:
        """extra may carry any of LEAD_EXTRA_COLUMNS (provenance, verification, quality); unknown keys are refused."""
        now = utcnow()
        extra = {k: v for k, v in (extra or {}).items() if v is not None}
        unknown = set(extra) - set(LEAD_EXTRA_COLUMNS)
        if unknown:
            raise KeyError(f"unknown lead columns: {', '.join(sorted(unknown))}")
        columns = ["profile", "company", "segment", "suburb", "email", "source_url", "observed_at",
                   "status", "status_reason", "duplicate_of", "created_at", "updated_at", *extra]
        values = [profile, company, segment, suburb, email, source_url, observed_at,
                  status, reason, duplicate_of, now, now, *extra.values()]
        cur = self.conn.execute(
            f"INSERT INTO leads({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
            values,
        )
        self.conn.execute(
            "INSERT INTO events(lead_id,from_status,to_status,reason,at) VALUES(?,?,?,?,?)",
            (cur.lastrowid, None, status, reason, now),
        )
        return cur.lastrowid

    def lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def lead_by_email(self, email: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM leads WHERE email=? AND status!=? ORDER BY id LIMIT 1",
            (email, states.REJECTED_DUPLICATE),
        ).fetchone()

    def leads_by_status(self, status: str, profile: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM leads WHERE status=?"
        args: list = [status]
        if profile:
            sql += " AND profile=?"
            args.append(profile)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def transition(self, lead_id: int, target: str, reason: str | None = None) -> None:
        row = self.lead(lead_id)
        if row is None:
            raise KeyError(f"lead {lead_id} not found")
        states.check_transition(row["status"], target)
        now = utcnow()
        self.conn.execute(
            "UPDATE leads SET status=?, status_reason=?, updated_at=? WHERE id=?",
            (target, reason, now, lead_id),
        )
        self.conn.execute(
            "INSERT INTO events(lead_id,from_status,to_status,reason,at) VALUES(?,?,?,?,?)",
            (lead_id, row["status"], target, reason, now),
        )

    def leads_observed_on(self, profile: str, day_prefix: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE profile=? AND observed_at LIKE ?", (profile, day_prefix + "%")
        ).fetchone()[0]

    def counts(self, profile: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) FROM leads"
        args: tuple = ()
        if profile:
            sql += " WHERE profile=?"
            args = (profile,)
        sql += " GROUP BY status"
        out = {s: 0 for s in states.ALL}
        for status, n in self.conn.execute(sql, args):
            out[status] = n
        return out

    # ---- drafts ------------------------------------------------------------
    def insert_draft(self, lead_id: int, subject: str, body: str, generator: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts(lead_id,subject,body,generator,created_at) VALUES(?,?,?,?,?)",
            (lead_id, subject, body, generator, utcnow()),
        )
        return cur.lastrowid

    def latest_draft(self, lead_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead_id,)
        ).fetchone()

    # ---- sending -----------------------------------------------------------
    def log_send(self, lead_id, draft_id, mailbox, to_email, dry_run: bool, result: str, message_id=None, sent_at=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO send_log(lead_id,draft_id,mailbox,to_email,sent_at,message_id,dry_run,result) VALUES(?,?,?,?,?,?,?,?)",
            (lead_id, draft_id, mailbox, to_email, sent_at or utcnow(), message_id, 1 if dry_run else 0, result),
        )
        return cur.lastrowid

    def sends_since(self, since_iso: str, mailbox: str, dry_run: bool) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM send_log WHERE mailbox=? AND dry_run=? AND result='sent' AND sent_at>=?",
            (mailbox, 1 if dry_run else 0, since_iso),
        ).fetchone()[0]

    def real_send_exists(self, to_email: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM send_log WHERE to_email=? AND dry_run=0 AND result='sent' LIMIT 1", (to_email,)
        ).fetchone() is not None

    def sends_today(self, day_prefix: str, dry_run: bool | None = None) -> int:
        sql = "SELECT COUNT(*) FROM send_log WHERE result='sent' AND sent_at LIKE ?"
        args: list = [day_prefix + "%"]
        if dry_run is not None:
            sql += " AND dry_run=?"
            args.append(1 if dry_run else 0)
        return self.conn.execute(sql, args).fetchone()[0]

    # ---- suppression -------------------------------------------------------
    def suppress(self, email: str, reason: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression(email,reason,added_at) VALUES(?,?,?)", (email, reason, utcnow())
        )

    def is_suppressed(self, email: str) -> bool:
        return self.conn.execute("SELECT 1 FROM suppression WHERE email=?", (email,)).fetchone() is not None

    def suppression_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]

    # ---- key/value state ---------------------------------------------------
    def state_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def state_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, value))

    def state_delete(self, key: str) -> None:
        # state is operational flags (pause, warm-up start), not lead records.
        self.conn.execute("DELETE FROM state WHERE key=?", (key,))
