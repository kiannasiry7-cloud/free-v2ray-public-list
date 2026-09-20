"""Sender: sends approved drafts through a mailbox, under caps, spacing, warm-up and the safety gates.
dry_run (settings) simulates everything and changes no lead status. Real sends need the confirm phrase."""
import platform
import smtplib
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
import os

from .config import ConfigError, Settings
from .db import Database
from . import guards, states

WARMUP_KEY = "warmup_started_on"
UNSUBSCRIBE_TEXT = "If you'd rather not hear from us, reply with \"unsubscribe\" and we will not contact you again."


class TransportError(Exception):
    pass


class BounceError(TransportError):
    """The receiving side refused the recipient — treated as a bounce."""


class Transport:
    def send(self, msg: EmailMessage) -> str:  # returns message id
        raise NotImplementedError


def smtp_password(box: dict) -> str:
    """password_source: env  -> read $password_env
                        keychain -> macOS Keychain (`security find-internet-password`), no secret on disk."""
    smtp = box["smtp"]
    source = smtp.get("password_source", "env")
    if source == "keychain":
        if platform.system() != "Darwin":
            raise ConfigError(f"mailbox {box['name']}: password_source keychain only works on macOS")
        kc = smtp.get("keychain") or {}
        server = kc.get("server", smtp["host"])
        account = kc.get("account", smtp["username"])
        try:
            out = subprocess.run(
                ["security", "find-internet-password", "-s", server, "-a", account, "-w"],
                check=True, capture_output=True, text=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as e:
            raise ConfigError(f"mailbox {box['name']}: keychain lookup failed ({server}/{account}): {e}") from e
        return out.stdout.strip()
    if source == "env":
        return os.environ.get(smtp["password_env"], "")
    raise ConfigError(f"mailbox {box['name']}: password_source must be env or keychain")


class SmtpTransport(Transport):
    def __init__(self, box: dict):
        smtp = box["smtp"]
        self.host, self.port = smtp["host"], int(smtp["port"])
        self.username, self.starttls = smtp["username"], bool(smtp["starttls"])
        self.password = smtp_password(box)
        if not self.host or not self.password:
            raise ConfigError(f"mailbox {box['name']}: smtp host or password is empty")

    def send(self, msg: EmailMessage) -> str:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                if self.starttls:
                    s.starttls()
                s.login(self.username, self.password)
                s.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise BounceError(str(e)) from e
        except (smtplib.SMTPException, OSError) as e:
            raise TransportError(str(e)) from e
        return msg["Message-ID"]


class AppleMailTransport(Transport):
    """Sends through Mail.app via osascript. Only works on macOS."""

    def __init__(self, box: dict):
        if platform.system() != "Darwin":
            raise ConfigError(f"mailbox {box['name']}: apple_mail only works on macOS")
        self.account = box.get("apple_mail", {}).get("account", box["from_email"])

    def send(self, msg: EmailMessage) -> str:
        def q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "Mail"
            set m to make new outgoing message with properties {{subject:"{q(msg["Subject"])}", content:"{q(msg.get_content())}", visible:false}}
            tell m
                make new to recipient at end of to recipients with properties {{address:"{q(msg["To"])}"}}
                set sender to "{q(self.account)}"
            end tell
            send m
        end tell'''
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as e:
            raise TransportError(str(e)) from e
        return msg["Message-ID"]


class DryRunTransport(Transport):
    def send(self, msg: EmailMessage) -> str:
        return msg["Message-ID"]


def build_transport(box: dict) -> Transport:
    return {"smtp": SmtpTransport, "apple_mail": AppleMailTransport}[box["method"]](box)


def build_message(settings: Settings, box: dict, lead, draft) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f'{box["from_name"]} <{box["from_email"]}>'
    msg["To"] = lead["email"]
    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = f"<{uuid.uuid4()}@{box['from_email'].split('@')[-1]}>"
    reply_to = box["reply_to"] or box["from_email"]
    msg["Reply-To"] = reply_to
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    body = draft["body"]
    if settings.get("sender.unsubscribe_footer"):
        body = f"{body}\n\n--\n{UNSUBSCRIBE_TEXT}"
    msg.set_content(body)
    return msg


def effective_daily_cap(settings: Settings, db: Database, today: datetime) -> int:
    cap = int(settings.get("sender.daily_cap"))
    if not settings.get("sender.warmup.enabled"):
        return cap
    steps = [int(s) for s in settings.get("sender.warmup.steps")]
    started = db.state_get(WARMUP_KEY)
    if not started:
        return min(cap, steps[0]) if steps else cap
    day_index = (today.date() - datetime.fromisoformat(started).date()).days
    if day_index < len(steps):
        return min(cap, steps[day_index])
    return cap


@dataclass
class SendReport:
    dry_run: bool
    sent: list[tuple[int, str, str]] = field(default_factory=list)  # (lead_id, email, mailbox)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)  # (lead_id, email, reason)
    stopped_because: str | None = None


def run_sender(db: Database, settings: Settings, profile_name: str, confirm_phrase: str | None = None,
               transports: dict[str, Transport] | None = None, sleep=time.sleep,
               now: datetime | None = None) -> SendReport:
    profile = settings.profile(profile_name)
    dry_run = bool(settings.get("sender.dry_run"))
    report = SendReport(dry_run=dry_run)
    now = now or datetime.now(timezone.utc)

    paused = guards.is_paused(db)
    if paused:
        report.stopped_because = f"paused: {paused} (run `leadmailer resume` after checking)"
        return report
    if not dry_run and confirm_phrase != guards.CONFIRM_PHRASE:
        report.stopped_because = f"real sending requires typing exactly: {guards.CONFIRM_PHRASE}"
        return report

    boxes = [settings.mailbox(n) for n in profile["mailboxes"]]
    transports = transports or {}
    hourly_cap = int(settings.get("sender.hourly_cap"))
    daily_cap = effective_daily_cap(settings, db, now)
    spacing = float(settings.get("sender.spacing_seconds"))
    hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    day_prefix = now.isoformat()[:10]
    # stamp the ledger with the same clock the caps are measured against
    sent_at = now.isoformat(timespec="seconds")

    def capacity(box_name: str) -> bool:
        return (db.sends_since(hour_ago, box_name, dry_run) < hourly_cap
                and db.sends_since(day_prefix, box_name, dry_run) < daily_cap)

    leads = db.leads_by_status(states.APPROVED, profile_name)
    box_i = 0
    for lead in leads:
        blocked = guards.blocked_reason(db, lead["email"])
        if blocked:
            db.transition(lead["id"], states.QUARANTINED, blocked)
            report.skipped.append((lead["id"], lead["email"], blocked))
            continue
        draft = db.latest_draft(lead["id"])
        if draft is None:
            db.transition(lead["id"], states.QUARANTINED, "no_draft")
            report.skipped.append((lead["id"], lead["email"], "no_draft"))
            continue

        # pick the next mailbox with capacity (round robin)
        box = None
        for _ in range(len(boxes)):
            candidate = boxes[box_i % len(boxes)]
            box_i += 1
            if capacity(candidate["name"]):
                box = candidate
                break
        if box is None:
            report.stopped_because = f"caps reached (hourly {hourly_cap} / daily {daily_cap} per mailbox)"
            break

        if report.sent and spacing > 0:
            sleep(spacing)

        msg = build_message(settings, box, lead, draft)
        if dry_run:
            db.log_send(lead["id"], draft["id"], box["name"], lead["email"], True, "sent", msg["Message-ID"], sent_at)
            report.sent.append((lead["id"], lead["email"], box["name"]))
            continue

        db.transition(lead["id"], states.QUEUED)
        transport = transports.get(box["name"]) or build_transport(box)
        transports[box["name"]] = transport
        try:
            message_id = transport.send(msg)
        except BounceError as e:
            db.log_send(lead["id"], draft["id"], box["name"], lead["email"], False, "bounce", msg["Message-ID"], sent_at)
            guards.record_bounce(db, lead["email"])
            report.skipped.append((lead["id"], lead["email"], "bounce"))
            report.stopped_because = f"auto-paused after bounce: {e}"
            break
        except TransportError as e:
            db.log_send(lead["id"], draft["id"], box["name"], lead["email"], False, f"error:{e}", msg["Message-ID"], sent_at)
            db.transition(lead["id"], states.QUARANTINED, f"transport_error:{e}")
            report.skipped.append((lead["id"], lead["email"], f"transport_error:{e}"))
            report.stopped_because = f"transport error: {e}"
            break
        if not db.state_get(WARMUP_KEY):
            db.state_set(WARMUP_KEY, now.isoformat())
        db.log_send(lead["id"], draft["id"], box["name"], lead["email"], False, "sent", message_id, sent_at)
        db.transition(lead["id"], states.SENT)
        report.sent.append((lead["id"], lead["email"], box["name"]))
    return report
