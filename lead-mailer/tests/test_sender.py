from datetime import datetime, timezone

import pytest
from conftest import make_settings

from leadmailer import guards, states
from leadmailer.db import Database
from leadmailer.finder import run_finder
from leadmailer.llm import NoneProvider
from leadmailer.sender import BounceError, Transport, effective_daily_cap, run_sender
from leadmailer.writer import run_writer

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


class FakeTransport(Transport):
    def __init__(self, bounce_on=()):
        self.sent, self.bounce_on = [], set(bounce_on)

    def send(self, msg):
        if msg["To"] in self.bounce_on:
            raise BounceError("550 no such user")
        self.sent.append(msg)
        return msg["Message-ID"]


def seed(db, settings, n=2):
    run_finder(db, settings, "pars_default", NoneProvider())
    run_writer(db, settings, "pars_default", NoneProvider())
    for lead in db.leads_by_status(states.DRAFTED):
        db.transition(lead["id"], states.APPROVED)


def transports():
    t = FakeTransport()
    return t, {"kian": t, "sales": t}


def test_dry_run_sends_nothing_and_keeps_status(db, settings):
    seed(db, settings)
    t, tr = transports()
    report = run_sender(db, settings, "pars_default", transports=tr, now=NOW)
    assert report.dry_run and len(report.sent) == 2 and t.sent == []
    assert db.counts()[states.APPROVED] == 2
    assert db.sends_today("2026-09-19", dry_run=True) == 2


def test_real_send_requires_confirm_phrase(tmp_path):
    s = make_settings(tmp_path, **{"sender.dry_run": False})
    with Database(s.db_path()) as db:
        seed(db, s)
        t, tr = transports()
        report = run_sender(db, s, "pars_default", confirm_phrase="yes", transports=tr, now=NOW)
        assert report.sent == [] and "APPROVE SEND" in report.stopped_because and t.sent == []
        report = run_sender(db, s, "pars_default", confirm_phrase=guards.CONFIRM_PHRASE, transports=tr, now=NOW)
        assert len(report.sent) == 2 and len(t.sent) == 2
        assert db.counts()[states.SENT] == 2
        assert {m["From"] for m in t.sent} == {"Kian - Pars Painting Melbourne <kian@parspaintingteam.com>",
                                                "Pars Painting Melbourne <sales@parspaintingteam.com>"}
        assert "unsubscribe" in t.sent[0].get_content().lower() and t.sent[0]["List-Unsubscribe"]


def test_duplicate_send_and_suppression_gates(tmp_path):
    s = make_settings(tmp_path, **{"sender.dry_run": False})
    with Database(s.path("db.sqlite")) as db:
        seed(db, s)
        t, tr = transports()
        run_sender(db, s, "pars_default", guards.CONFIRM_PHRASE, tr, now=NOW)
        # a new lead with an already-mailed address must never be mailed again
        lid = db.insert_lead("pars_default", "Alpha again", "s", "Fitzroy", "hello@alpha.test", "x", NOW.isoformat())
        db.transition(lid, states.DRAFTED); db.insert_draft(lid, "s", "b", "template"); db.transition(lid, states.APPROVED)
        guards.unsubscribe(db, "office@beta.test")
        report = run_sender(db, s, "pars_default", guards.CONFIRM_PHRASE, tr, now=NOW)
        assert report.sent == [] and [r[2] for r in report.skipped] == ["duplicate_send"]
        assert db.lead(lid)["status"] == states.QUARANTINED
        assert len(t.sent) == 2


def test_bounce_pauses_everything(tmp_path):
    s = make_settings(tmp_path, **{"sender.dry_run": False})
    with Database(s.path("db.sqlite")) as db:
        seed(db, s)
        t = FakeTransport(bounce_on={"hello@alpha.test"})
        report = run_sender(db, s, "pars_default", guards.CONFIRM_PHRASE, {"kian": t, "sales": t}, now=NOW)
        assert report.sent == [] and "bounce" in report.stopped_because
        assert guards.is_paused(db) and db.is_suppressed("hello@alpha.test")
        assert db.counts()[states.APPROVED] == 1  # the other lead was not touched
        report = run_sender(db, s, "pars_default", guards.CONFIRM_PHRASE, {"kian": t, "sales": t}, now=NOW)
        assert report.sent == [] and report.stopped_because.startswith("paused")
        guards.resume(db)
        report = run_sender(db, s, "pars_default", guards.CONFIRM_PHRASE, {"kian": t, "sales": t}, now=NOW)
        assert len(report.sent) == 1


def test_caps_and_spacing(tmp_path):
    s = make_settings(tmp_path, **{"sender.hourly_cap": 1, "sender.spacing_seconds": 7, "sender.warmup.enabled": False})
    with Database(s.path("db.sqlite")) as db:
        seed(db, s)
        for i in range(3):  # 5 approved leads total
            lid = db.insert_lead("pars_default", f"C{i}", "s", "X", f"c{i}@x.test", "x", NOW.isoformat())
            db.transition(lid, states.DRAFTED); db.insert_draft(lid, "s", "b", "template"); db.transition(lid, states.APPROVED)
        sleeps = []
        report = run_sender(db, s, "pars_default", transports=transports()[1], sleep=sleeps.append, now=NOW)
        assert len(report.sent) == 2          # 1/hour per mailbox x 2 mailboxes
        assert {b for _, _, b in report.sent} == {"kian", "sales"}
        assert sleeps == [7]                  # spacing between sends, not before the first
        assert "caps reached" in report.stopped_because


def test_warmup_steps(db, settings):
    assert effective_daily_cap(settings, db, NOW) == 5           # day 1 before any real send
    db.state_set("warmup_started_on", "2026-09-17T09:00:00+00:00")
    assert effective_daily_cap(settings, db, NOW) == 15          # day index 2 -> steps[2]
    db.state_set("warmup_started_on", "2026-09-01T09:00:00+00:00")
    assert effective_daily_cap(settings, db, NOW) == 20          # past warm-up -> daily_cap
