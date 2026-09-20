"""The canonical Finder flow with the optional components plugged in.

Everything external is faked: no Docker, no network, no subprocess, no mail. The point of
these tests is the contract the finder promises — provenance is recorded, the verification
gate holds, the ledger stays the single source of truth, and a missing component degrades
into the behaviour the project had before it existed.
"""
import json

import pytest
from conftest import make_settings

from leadmailer import adapters, states
from leadmailer.adapters import AdapterUnavailable, Crawl4AIAdapter, ReacherAdapter
from leadmailer.db import Database
from leadmailer.finder import run_finder
from leadmailer.llm import NoneProvider

MAPS_ROWS = [
    {"title": "Alpha Realty", "website": "https://alpha.test", "phone": "03 9000 0000",
     "complete_address": {"city": "Fitzroy"}, "link": "https://maps.google.com/?cid=1",
     "query": "property manager Fitzroy"},
    {"title": "Beta Strata", "website": "https://beta.test", "phone": "03 9111 1111",
     "complete_address": {"city": "Carlton"}, "link": "https://maps.google.com/?cid=2",
     "emails": ["office@beta.test"]},
]

CRAWLED = {
    "https://alpha.test/": "Alpha Realty. Our director Jane handles property manager work.",
    "https://alpha.test/contact": "Email director@alpha.test or careers@alpha.test",
    "https://beta.test/": "Beta Strata, Carlton.",
}


def maps_settings(tmp_path, rows=MAPS_ROWS, acquisition=None, **overrides):
    """A profile whose only source is a google-maps-scraper export."""
    over = {
        "profiles.pars_default.sources": [{"type": "maps_export", "path": "sources/maps.json"}],
        "acquisition": {"crawl4ai": {"enabled": True}, "reacher": {"enabled": True},
                        **(acquisition or {})},
        **overrides,
    }
    settings = make_settings(tmp_path, **over)
    (tmp_path / "sources" / "maps.json").write_text(json.dumps(rows), encoding="utf-8")
    return settings


def fake_crawler(pages=None, fail=False, **cfg):
    pages = CRAWLED if pages is None else pages

    def http(url, payload, headers, timeout, method="POST"):
        if fail:
            raise AdapterUnavailable("crawl4ai: connection refused")
        return {"results": [{"url": u, "success": u in pages, "markdown": {"raw_markdown": pages.get(u, "")}}
                            for u in payload["urls"]]}

    return Crawl4AIAdapter({"enabled": True, **cfg}, http=http)


def fake_verifier(states_by_email=None, default="safe", fail=False):
    states_by_email = states_by_email or {}
    calls = []

    def http(url, payload, headers, timeout, method="POST"):
        if fail:
            raise AdapterUnavailable("reacher: port 25 blocked")
        email = payload["to_email"].lower()
        calls.append(email)
        state = states_by_email.get(email, default)
        smtp = {"is_catch_all": state == "catch_all"}
        return {"is_reachable": "risky" if state == "catch_all" else state, "smtp": smtp}

    verifier = ReacherAdapter({"enabled": True}, http=http)
    verifier.calls = calls
    return verifier


def run(settings, crawler=None, verifier=None):
    with Database(settings.db_path()) as db:
        results = run_finder(db, settings, "pars_default", NoneProvider(),
                             crawler=crawler or fake_crawler(), verifier=verifier or fake_verifier())
        rows = {r["email"]: dict(r) for r in
                db.conn.execute("SELECT * FROM leads WHERE profile='pars_default'")}
        return results, rows


# ---- the happy path --------------------------------------------------------------------

def test_maps_export_is_crawled_verified_and_stored_with_full_provenance(tmp_path):
    results, rows = run(maps_settings(tmp_path))
    assert {r.status for r in results} == {states.CANDIDATE}

    alpha = rows["director@alpha.test"]                     # crawled off the company's own contact page
    assert alpha["company"] == "Alpha Realty"
    assert alpha["suburb"] == "Fitzroy"
    assert alpha["website"] == "https://alpha.test"
    assert alpha["phone"] == "03 9000 0000"
    assert alpha["evidence_url"] == "https://alpha.test/contact"
    assert alpha["source_url"] == "https://maps.google.com/?cid=1"
    assert alpha["provenance"] == "google_maps_scraper+crawl4ai"
    assert alpha["verification"] == adapters.VALID
    assert alpha["quality_score"] > 0 and "email_on_company_domain" in alpha["quality_reason"]

    beta = rows["office@beta.test"]                          # came straight off the scraper row
    assert not beta["evidence_url"] and beta["source_url"] == "https://maps.google.com/?cid=2"
    assert beta["provenance"] == "google_maps_scraper"


def test_the_best_contact_per_business_is_kept_not_every_address_on_the_page(tmp_path):
    _, rows = run(maps_settings(tmp_path))
    assert "director@alpha.test" in rows                      # decision maker beats careers@
    assert "careers@alpha.test" not in rows


def test_max_contacts_per_business_can_be_raised(tmp_path):
    settings = maps_settings(tmp_path, acquisition={"max_contacts_per_business": 2})
    _, rows = run(settings)
    assert {"director@alpha.test", "careers@alpha.test"} <= set(rows)


# ---- the verification gate -------------------------------------------------------------

@pytest.mark.parametrize("state,expected_reason", [
    ("invalid", "verification:invalid"),
    ("risky", "verification:risky"),
    ("catch_all", "verification:catch_all"),
    ("unknown", "verification:unknown"),
])
def test_non_valid_verification_never_becomes_send_ready(tmp_path, state, expected_reason):
    verifier = fake_verifier(default=state)
    results, rows = run(maps_settings(tmp_path), verifier=verifier)
    assert {r.status for r in results} == {states.QUARANTINED}
    assert {r.reason for r in results} == {expected_reason}
    # quarantined leads are still recorded, with their verification state, for a human to judge
    assert all(row["verification"] == state for row in rows.values())
    assert all(row["quality_score"] is not None for row in rows.values())


def test_a_quarantined_lead_only_reaches_the_writer_through_a_human_release(tmp_path):
    settings = maps_settings(tmp_path)
    with Database(settings.db_path()) as db:
        run_finder(db, settings, "pars_default", NoneProvider(),
                   crawler=fake_crawler(), verifier=fake_verifier(default="unknown"))
        assert db.leads_by_status(states.CANDIDATE) == []
        lead_id = db.leads_by_status(states.QUARANTINED)[0]["id"]
        db.transition(lead_id, states.CANDIDATE, "released: checked by hand")
        assert [l["id"] for l in db.leads_by_status(states.CANDIDATE)] == [lead_id]


def test_mixed_results_admit_only_the_valid_address(tmp_path):
    verifier = fake_verifier({"director@alpha.test": "safe", "office@beta.test": "invalid"})
    results, _ = run(maps_settings(tmp_path), verifier=verifier)
    assert {r.email: r.status for r in results} == {
        "director@alpha.test": states.CANDIDATE, "office@beta.test": states.QUARANTINED}


def test_require_verification_rejects_leads_no_verifier_could_check(tmp_path):
    settings = maps_settings(tmp_path, acquisition={"require_verification": True})
    results, _ = run(settings, verifier=fake_verifier(fail=True))
    assert {r.status for r in results} == {states.QUARANTINED}
    assert {r.reason for r in results} == {"verification:unverified"}


def test_a_verified_address_is_checked_once_even_when_it_appears_twice(tmp_path):
    rows = MAPS_ROWS + [dict(MAPS_ROWS[1], title="Beta Strata (second listing)")]
    verifier = fake_verifier()
    run(maps_settings(tmp_path, rows=rows), verifier=verifier)
    assert verifier.calls.count("office@beta.test") == 1


# ---- graceful fallback -----------------------------------------------------------------

def test_an_unreachable_crawler_falls_back_to_the_scraper_row(tmp_path):
    logged = []
    settings = maps_settings(tmp_path)
    with Database(settings.db_path()) as db:
        results = run_finder(db, settings, "pars_default", NoneProvider(),
                             crawler=fake_crawler(fail=True), verifier=fake_verifier(),
                             log=logged.append)
    assert {r.email for r in results} == {"office@beta.test"}        # Alpha had no address without the crawl
    assert any("crawl4ai unavailable" in line for line in logged)


def test_an_unreachable_verifier_leaves_leads_unverified_but_usable(tmp_path):
    logged = []
    settings = maps_settings(tmp_path)
    with Database(settings.db_path()) as db:
        results = run_finder(db, settings, "pars_default", NoneProvider(),
                             crawler=fake_crawler(), verifier=fake_verifier(fail=True), log=logged.append)
        rows = {r["email"]: dict(r) for r in db.conn.execute("SELECT * FROM leads")}
    assert {r.status for r in results} == {states.CANDIDATE}          # pre-existing behaviour preserved
    assert {r.verification for r in results} == {adapters.UNVERIFIED}
    assert rows["office@beta.test"]["verification_detail"] == "verifier_unavailable"
    assert any("reacher unavailable" in line for line in logged)


def test_a_missing_maps_export_skips_that_source_and_keeps_the_others(tmp_path):
    logged = []
    settings = make_settings(tmp_path, **{"profiles.pars_default.sources": [
        {"type": "maps_export", "path": "sources/not-exported-yet.json"},
        {"type": "csv_file", "path": "sources/pars_manual.csv"},
    ]})
    with Database(settings.db_path()) as db:
        results = run_finder(db, settings, "pars_default", NoneProvider(), log=logged.append)
    assert {r.email for r in results} == {"hello@alpha.test", "office@beta.test"}
    assert any("maps_export skipped" in line for line in logged)


def test_a_missing_maps_binary_skips_that_source(tmp_path):
    logged = []
    settings = make_settings(tmp_path, **{"profiles.pars_default.sources": [
        {"type": "maps_command", "command": ["definitely-not-installed-scraper", "-q", "x"]},
    ]})
    with Database(settings.db_path()) as db:
        assert run_finder(db, settings, "pars_default", NoneProvider(), log=logged.append) == []
    assert any("maps_command skipped" in line for line in logged)


def test_with_no_acquisition_block_the_finder_calls_nothing_external(tmp_path):
    """The shipped settings.yaml has no acquisition block: both adapters must stay off."""
    settings = make_settings(tmp_path)
    assert settings.acquisition()["crawl4ai"] == {"enabled": False}
    crawler, verifier = adapters.build_adapters(settings.acquisition())
    assert crawler.available() is False and verifier.available() is False
    with Database(settings.db_path()) as db:                          # unchanged CSV behaviour
        results = run_finder(db, settings, "pars_default", NoneProvider())
    assert {r.status for r in results} == {states.CANDIDATE}
    assert {r.verification for r in results} == {adapters.UNVERIFIED}


# ---- volume, de-duplication and the ledger ---------------------------------------------

def test_the_run_cap_holds_even_when_the_scraper_returns_more(tmp_path):
    rows = [dict(MAPS_ROWS[1], title=f"Strata {i}", website=f"https://s{i}.test",
                 emails=[f"office@s{i}.test"], link=f"https://maps.google.com/?cid={i}")
            for i in range(30)]
    settings = maps_settings(tmp_path, rows=rows, acquisition={"max_candidates_per_run": 20})
    results, _ = run(settings, crawler=fake_crawler(pages={}))
    assert len([r for r in results if r.status == states.CANDIDATE]) == 20


def test_the_default_run_cap_is_twenty(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.acquisition()["max_candidates_per_run"] == 20


def test_a_threshold_quarantines_the_weaker_lead_and_keeps_the_stronger_one(tmp_path):
    baseline, _ = run(maps_settings(tmp_path / "baseline"))
    scores = {r.email: r.quality_score for r in baseline}
    assert scores["director@alpha.test"] > scores["office@beta.test"]    # crawled evidence + decider mailbox

    settings = maps_settings(tmp_path / "threshold",
                             acquisition={"min_quality_score": scores["director@alpha.test"]})
    results, _ = run(settings)
    by_email = {r.email: r for r in results}
    assert by_email["director@alpha.test"].status == states.CANDIDATE
    weak = by_email["office@beta.test"]
    assert weak.status == states.QUARANTINED
    assert weak.reason == f"quality_below_threshold:{weak.quality_score}<{scores['director@alpha.test']}"


def test_no_duplicate_contact_is_ever_introduced(tmp_path):
    settings = maps_settings(tmp_path)
    with Database(settings.db_path()) as db:
        first = run_finder(db, settings, "pars_default", NoneProvider(),
                           crawler=fake_crawler(), verifier=fake_verifier())
        second = run_finder(db, settings, "pars_default", NoneProvider(),
                            crawler=fake_crawler(), verifier=fake_verifier())
        assert {r.status for r in second} == {states.REJECTED_DUPLICATE}
        assert all(r.reason.startswith("duplicate of lead") for r in second)
        emails = [r["email"] for r in db.conn.execute(
            "SELECT email FROM leads WHERE status=?", (states.CANDIDATE,))]
        assert sorted(emails) == sorted({r.email for r in first})     # one candidate row per address


def test_an_existing_ledger_gains_the_new_columns_without_losing_rows(tmp_path):
    """The ledger stays the source of truth: the extra columns are an ALTER, not a new store."""
    settings = make_settings(tmp_path)
    with Database(settings.db_path()) as db:
        old_id = db.insert_lead("pars_default", "Legacy", "seg", "Fitzroy", "legacy@old.test",
                                "https://old.test", "2026-01-01T00:00:00")
    with Database(settings.db_path()) as db:                          # reopen: migration runs again
        row = dict(db.lead(old_id))
        assert row["email"] == "legacy@old.test"
        assert row["quality_score"] is None and row["provenance"] is None
        new_id = db.insert_lead("pars_default", "New", "seg", "Carlton", "new@x.test", "u", "2026-01-02T00:00:00",
                                extra={"provenance": "google_maps_scraper", "quality_score": 42})
        assert dict(db.lead(new_id))["quality_score"] == 42
        assert db.counts("pars_default")[states.CANDIDATE] == 2


def test_insert_lead_refuses_columns_it_does_not_know(tmp_path):
    settings = make_settings(tmp_path)
    with Database(settings.db_path()) as db:
        with pytest.raises(KeyError):
            db.insert_lead("pars_default", "X", "seg", "Fitzroy", "x@y.test", "u", "2026-01-01T00:00:00",
                           extra={"drop_table": "1"})
