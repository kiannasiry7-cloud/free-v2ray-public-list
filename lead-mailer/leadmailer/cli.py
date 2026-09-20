"""leadmailer CLI: finder | writer | approve | reject | sender | status | unsubscribe | bounce | complaint | resume | release"""
import argparse
import os
import sys
from datetime import datetime, timezone

from .config import ConfigError, Settings
from .db import Database, DatabaseLocked
from .finder import run_finder
from .llm import provider_for
from .sender import effective_daily_cap, run_sender
from .writer import run_writer
from . import guards, states

DEFAULT_SETTINGS = os.environ.get("LEADMAILER_SETTINGS", "settings.yaml")


def _profiles(settings: Settings, name: str | None) -> list[str]:
    if name:
        settings.profile(name)  # raises ConfigError for unknown names
        return [name]
    return settings.profile_names()


def cmd_finder(args, settings, db):
    provider = provider_for(settings, "finder")
    for prof in _profiles(settings, args.profile):
        results = run_finder(db, settings, prof, provider, log=lambda msg: print(msg))
        print(f"[{prof}] {len(results)} leads observed")
        for r in results:
            print(f"  #{r.lead_id:<5} {r.status:<19} q={r.quality_score:<3} {r.verification:<11} "
                  f"{r.company[:30]:<30} {r.suburb:<14} {r.email}  {r.reason or ''}")


def cmd_writer(args, settings, db):
    provider = provider_for(settings, "writer")
    for prof in _profiles(settings, args.profile):
        results = run_writer(db, settings, prof, provider, limit=args.limit, template_name=args.template)
        print(f"[{prof}] {len(results)} drafts processed via {provider.name}")
        for r in results:
            print(f"  #{r.lead_id:<5} {r.status:<12} {r.generator:<12} {r.email}  {r.reason or ''}")


def cmd_approve(args, settings, db):
    leads = [db.lead(args.id)] if args.id else db.leads_by_status(states.DRAFTED, args.profile)
    for lead in leads:
        if lead is None or lead["status"] != states.DRAFTED:
            print(f"lead {args.id} is not in drafted state")
            continue
        draft = db.latest_draft(lead["id"])
        print(f"\n--- #{lead['id']} {lead['company']} <{lead['email']}> [{lead['profile']}]")
        print(f"Subject: {draft['subject']}\n\n{draft['body']}\n")
        if args.all:
            db.transition(lead["id"], states.APPROVED, "approve --all")
            print("approved")
            continue
        answer = input("approve? [y/N/q] ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            db.transition(lead["id"], states.APPROVED, "approved interactively")
            print("approved")


def cmd_reject(args, settings, db):
    db.transition(args.id, states.QUARANTINED, f"rejected: {args.reason}")
    print(f"lead {args.id} quarantined")


def cmd_release(args, settings, db):
    db.transition(args.id, states.CANDIDATE, f"released: {args.reason}")
    print(f"lead {args.id} back to candidate")


def cmd_sender(args, settings, db):
    confirm = args.confirm
    if not settings.get("sender.dry_run") and confirm is None:
        print(f"dry_run is false. Type exactly '{guards.CONFIRM_PHRASE}' to send real email, anything else aborts.")
        confirm = input("> ").strip()
    for prof in _profiles(settings, args.profile):
        report = run_sender(db, settings, prof, confirm_phrase=confirm)
        mode = "DRY RUN" if report.dry_run else "REAL"
        print(f"[{prof}] {mode}: {len(report.sent)} sent, {len(report.skipped)} skipped")
        for lead_id, email, box in report.sent:
            print(f"  sent    #{lead_id} {email} via {box}")
        for lead_id, email, reason in report.skipped:
            print(f"  skipped #{lead_id} {email}: {reason}")
        if report.stopped_because:
            print(f"  stopped: {report.stopped_because}")


def cmd_status(args, settings, db):
    now = datetime.now(timezone.utc)
    day = now.isoformat()[:10]
    paused = guards.is_paused(db)
    print(f"dry_run: {settings.get('sender.dry_run')}   paused: {paused or 'no'}")
    print(f"caps: {settings.get('sender.hourly_cap')}/hour, {effective_daily_cap(settings, db, now)}/day today "
          f"(configured daily {settings.get('sender.daily_cap')}), spacing {settings.get('sender.spacing_seconds')}s")
    print(f"sends today: real {db.sends_today(day, False)}, dry-run {db.sends_today(day, True)}   "
          f"suppression list: {db.suppression_count()}")
    for prof in _profiles(settings, args.profile):
        counts = db.counts(prof)
        print(f"[{prof}] " + "  ".join(f"{s}={counts[s]}" for s in states.ALL))


def _guard_cmd(fn, label):
    def run(args, settings, db):
        fn(db, args.email)
        print(f"{label}: {guards.normalize_email(args.email)}")
    return run


def cmd_resume(args, settings, db):
    reason = guards.is_paused(db)
    guards.resume(db)
    print(f"resumed (was paused: {reason or 'no'})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadmailer")
    p.add_argument("--settings", default=DEFAULT_SETTINGS, help="path to settings.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    s = add("finder", cmd_finder, "discover leads for a profile (or all)"); s.add_argument("--profile")
    s = add("writer", cmd_writer, "draft emails for candidate leads")
    s.add_argument("--profile"); s.add_argument("--limit", type=int); s.add_argument("--template", default="default")
    s = add("approve", cmd_approve, "review drafts and approve them")
    s.add_argument("--profile"); s.add_argument("--id", type=int); s.add_argument("--all", action="store_true")
    s = add("reject", cmd_reject, "quarantine a lead"); s.add_argument("--id", type=int, required=True); s.add_argument("--reason", required=True)
    s = add("release", cmd_release, "move a quarantined lead back to candidate"); s.add_argument("--id", type=int, required=True); s.add_argument("--reason", required=True)
    s = add("sender", cmd_sender, "send approved drafts (dry_run unless settings say otherwise)")
    s.add_argument("--profile"); s.add_argument("--confirm", help=f"pass '{guards.CONFIRM_PHRASE}' non-interactively")
    s = add("status", cmd_status, "counts, caps, pause state"); s.add_argument("--profile")
    for name, fn, label in (("unsubscribe", guards.unsubscribe, "unsubscribed"),
                            ("bounce", guards.record_bounce, "bounce recorded, sending paused"),
                            ("complaint", guards.record_complaint, "complaint recorded, sending paused")):
        s = add(name, _guard_cmd(fn, label), f"record {name} for an address"); s.add_argument("email")
    add("resume", cmd_resume, "clear the auto-pause after you have checked the cause")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.settings)
        with Database(settings.db_path()) as db:
            args.fn(args, settings, db)
    except (ConfigError, DatabaseLocked, states.IllegalTransition, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
