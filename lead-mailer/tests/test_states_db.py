import pytest

from leadmailer import states
from leadmailer.db import Database, DatabaseLocked
from leadmailer.states import IllegalTransition


def test_only_allowed_transitions(db):
    lid = db.insert_lead("p", "Co", "seg", "Fitzroy", "a@b.test", "src", "2026-01-01T00:00:00+00:00")
    db.transition(lid, states.DRAFTED)
    with pytest.raises(IllegalTransition):
        db.transition(lid, states.SENT)  # cannot skip approve/queue
    db.transition(lid, states.APPROVED)
    db.transition(lid, states.QUEUED)
    db.transition(lid, states.SENT)
    with pytest.raises(IllegalTransition):
        db.transition(lid, states.QUARANTINED)  # sent is terminal
    assert [e["to_status"] for e in db.conn.execute("SELECT to_status FROM events ORDER BY id")] == [
        "candidate", "drafted", "approved", "queued", "sent"]


def test_quarantine_release_roundtrip(db):
    lid = db.insert_lead("p", "Co", "seg", "S", "a@b.test", "src", "2026-01-01T00:00:00+00:00")
    db.transition(lid, states.QUARANTINED, "manual")
    db.transition(lid, states.CANDIDATE, "released")
    assert db.lead(lid)["status"] == states.CANDIDATE


def test_single_writer_lock(settings):
    path = settings.db_path()
    with Database(path):
        with pytest.raises(DatabaseLocked):
            with Database(path):
                pass
    with Database(path):  # released after the block
        pass
