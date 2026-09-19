from leadmailer import states
from leadmailer.finder import iter_source, plausible_email, run_finder
from leadmailer.llm import NoneProvider


def test_csv_finder_dedupes_and_filters(db, settings):
    results = run_finder(db, settings, "pars_default", NoneProvider())
    by_email = {r.email: r for r in results}
    assert by_email["hello@alpha.test"].status == states.CANDIDATE   # normalised to lower case
    assert by_email["office@beta.test"].status == states.CANDIDATE
    assert "noreply@junk.test" not in by_email                        # junk filtered before storage
    assert len(results) == 2                                           # in-run duplicate not stored twice

    # second run: everything is a duplicate, nothing deleted, records appended
    again = run_finder(db, settings, "pars_default", NoneProvider())
    assert {r.status for r in again} == {states.REJECTED_DUPLICATE}
    assert db.counts("pars_default")[states.CANDIDATE] == 2
    assert db.counts("pars_default")[states.REJECTED_DUPLICATE] == 2


def test_suppressed_email_is_quarantined_not_candidate(db, settings):
    db.suppress("office@beta.test", "unsubscribe")
    results = run_finder(db, settings, "pars_default", NoneProvider())
    assert {r.email: r.status for r in results}["office@beta.test"] == states.QUARANTINED


def test_daily_cap_respected(tmp_path):
    from conftest import make_settings
    from leadmailer.db import Database
    s = make_settings(tmp_path, **{"profiles.pars_default.daily_cap": 1})
    with Database(s.db_path()) as db:
        assert len(run_finder(db, s, "pars_default", NoneProvider())) == 1


def test_html_source_expands_suburbs(settings):
    profile = dict(settings.profile("pars_default"), suburbs=["Fitzroy", "Carlton"], keywords=["painter"])
    calls = []

    def fetch(url):
        calls.append(url)
        return f"<html><title>Acme {url[-7:]} Realty</title><a href='mailto:info@acme.test'>x</a> logo@2x.png</html>"

    leads = iter_source({"type": "html_page", "url": "https://d.test/?q={keyword}+{suburb}"}, profile, settings, NoneProvider(), fetch)
    assert calls == ["https://d.test/?q=painter+Fitzroy", "https://d.test/?q=painter+Carlton"]
    assert [(l.suburb, l.email) for l in leads] == [("Fitzroy", "info@acme.test"), ("Carlton", "info@acme.test")]
    assert leads[0].company.startswith("Acme")


def test_plausible_email():
    assert plausible_email("Hello@Alpha.test")
    assert not plausible_email("noreply@x.test")
    assert not plausible_email("img@2x.png")
    assert not plausible_email("a@example.com")
