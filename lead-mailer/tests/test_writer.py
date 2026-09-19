import pytest

from leadmailer import states
from leadmailer.finder import run_finder
from leadmailer.llm import LLMError, NoneProvider, Provider
from leadmailer.writer import check_rules, load_brief, run_writer


class FakeLLM(Provider):
    name = "fake"

    def __init__(self, text=None, fail=False):
        self.text, self.fail = text, fail

    def available(self):
        return True

    def complete(self, prompt, system=None):
        if self.fail:
            raise LLMError("down")
        return self.text


@pytest.fixture
def seeded(db, settings):
    run_finder(db, settings, "pars_default", NoneProvider())
    return db


def test_template_writer_creates_drafts_only(seeded, settings):
    results = run_writer(seeded, settings, "pars_default", NoneProvider())
    assert {r.status for r in results} == {states.DRAFTED}
    draft = seeded.latest_draft(results[0].lead_id)
    assert "Alpha Realty" in draft["body"] and "Fitzroy" in draft["subject"]
    assert draft["generator"] == "template"
    assert seeded.conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 0


def test_llm_output_violating_rules_is_quarantined(seeded, settings):
    llm = FakeLLM("We can finish within 3 days for just $500, guaranteed! 200 clients love us.")
    results = run_writer(seeded, settings, "pars_default", llm)
    assert {r.status for r in results} == {states.QUARANTINED}
    assert "pricing" in results[0].reason and "timeline_promise" in results[0].reason
    assert "unverified_number:200 clients" in results[0].reason
    assert seeded.latest_draft(results[0].lead_id)["generator"] == "fake:rejected"


def test_clean_llm_output_is_drafted(seeded, settings):
    results = run_writer(seeded, settings, "pars_default", FakeLLM("Hi team, we are fully insured painters. Kind regards, Kian"))
    assert {r.status for r in results} == {states.DRAFTED}
    assert seeded.latest_draft(results[0].lead_id)["generator"] == "fake"


def test_llm_down_falls_back_to_template(seeded, settings):
    results = run_writer(seeded, settings, "pars_default", FakeLLM(fail=True))
    assert {r.generator for r in results} == {"template"}
    assert {r.status for r in results} == {states.DRAFTED}


def test_rules(settings):
    brief = load_brief(settings.path("briefs/pars_default.yaml"))
    assert check_rules("Hi, we are fully insured.", brief) == []
    assert "forbidden:cheapest" in check_rules("we are the cheapest", brief)
    assert "pricing" in check_rules("only $99", brief)
    assert "timeline_promise" in check_rules("done by Friday", brief)
