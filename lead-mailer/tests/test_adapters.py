"""Optional external components. Nothing here touches the network or spawns a process:
every HTTP call and every subprocess run is injected as a fake.
"""
import json
import subprocess

import pytest

from leadmailer import adapters
from leadmailer.adapters import (AdapterUnavailable, Crawl4AIAdapter, ReacherAdapter,
                                 interpret_reacher, load_maps_export, parse_maps_text, run_maps_command)

SUBURBS = ("Fitzroy", "Carlton")

MAPS_JSON = json.dumps([
    {"title": "Alpha Realty", "website": "https://alpha.test", "phone": "+61 3 9000 0000",
     "complete_address": {"city": "Fitzroy"}, "link": "https://maps.google.com/?cid=1",
     "emails": ["Hello@Alpha.test", "hello@alpha.test"], "query": "real estate Fitzroy"},
    {"title": "", "website": "https://nameless.test"},                      # no name -> dropped
])

MAPS_CSV = ("title,website,phone,address,link,email\n"
            "Beta Strata,https://beta.test,03 9111 1111,\"12 Smith St, Carlton VIC\",https://maps.google.com/?cid=2,office@beta.test\n")


def test_parse_maps_json_array_drops_nameless_rows_and_dedupes_emails():
    businesses = parse_maps_text(MAPS_JSON, SUBURBS)
    assert len(businesses) == 1
    b = businesses[0]
    assert (b.name, b.website, b.suburb) == ("Alpha Realty", "https://alpha.test", "Fitzroy")
    assert b.emails == ["Hello@Alpha.test"]              # case-insensitive de-duplication
    assert b.source_url == "https://maps.google.com/?cid=1"


def test_parse_maps_json_lines_and_csv_agree_on_shape():
    lines = "\n".join(json.dumps(r) for r in json.loads(MAPS_JSON) if r["title"])
    assert parse_maps_text(lines, SUBURBS)[0].name == "Alpha Realty"
    csv_rows = parse_maps_text(MAPS_CSV, SUBURBS)
    assert [(b.name, b.suburb, b.emails) for b in csv_rows] == [("Beta Strata", "Carlton", ["office@beta.test"])]


def test_parse_maps_text_rejects_broken_json_lines():
    with pytest.raises(AdapterUnavailable):
        parse_maps_text('{"title": "Alpha"}\n{"title": ', SUBURBS)


def test_parse_maps_text_empty_input_is_not_an_error():
    assert parse_maps_text("   ", SUBURBS) == []


def test_load_maps_export_missing_file_is_unavailable_not_a_crash(tmp_path):
    with pytest.raises(AdapterUnavailable):
        load_maps_export(tmp_path / "nope.json", SUBURBS)


def test_load_maps_export_reads_a_real_export(tmp_path):
    path = tmp_path / "maps.json"
    path.write_text(MAPS_JSON, encoding="utf-8")
    assert load_maps_export(path, SUBURBS)[0].name == "Alpha Realty"


def test_run_maps_command_requires_an_installed_binary():
    with pytest.raises(AdapterUnavailable) as e:
        run_maps_command(["definitely-not-installed-scraper", "-q", "x"], 5, SUBURBS)
    assert "not installed" in str(e.value)


def test_run_maps_command_empty_command_is_unavailable():
    with pytest.raises(AdapterUnavailable):
        run_maps_command([], 5, SUBURBS)


def _runner(returncode=0, stdout="", stderr=""):
    def run(command, **kwargs):
        run.called = (command, kwargs)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def test_run_maps_command_parses_stdout(tmp_path):
    binary = tmp_path / "scraper"
    binary.write_text("#!/bin/sh\n")
    runner = _runner(stdout=MAPS_CSV)
    businesses = run_maps_command([str(binary)], 5, SUBURBS, runner=runner)
    assert [b.name for b in businesses] == ["Beta Strata"]
    assert runner.called[1]["timeout"] == 5


def test_run_maps_command_nonzero_exit_is_unavailable(tmp_path):
    binary = tmp_path / "scraper"
    binary.write_text("#!/bin/sh\n")
    with pytest.raises(AdapterUnavailable) as e:
        run_maps_command([str(binary)], 5, SUBURBS, runner=_runner(returncode=2, stderr="boom"))
    assert "exited 2" in str(e.value)


def test_run_maps_command_subprocess_failure_is_unavailable(tmp_path):
    binary = tmp_path / "scraper"
    binary.write_text("#!/bin/sh\n")

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    with pytest.raises(AdapterUnavailable):
        run_maps_command([str(binary)], 5, SUBURBS, runner=runner)


# ---- crawl4ai --------------------------------------------------------------------------

def _crawl_http(response, spy=None):
    def http(url, payload, headers, timeout, method="POST"):
        if spy is not None:
            spy.append((url, payload, headers, timeout))
        if isinstance(response, Exception):
            raise response
        return response
    return http


def test_crawl4ai_is_off_by_default_and_never_calls_out():
    crawler = Crawl4AIAdapter()
    assert crawler.available() is False
    with pytest.raises(AdapterUnavailable):
        crawler.crawl("https://alpha.test")


def test_crawl4ai_requests_only_public_paths_within_the_page_budget():
    crawler = Crawl4AIAdapter({"enabled": True, "max_pages_per_site": 2})
    assert crawler.page_urls("alpha.test") == ["https://alpha.test/", "https://alpha.test/contact"]
    assert crawler.page_urls("not a url") == []


def test_crawl4ai_returns_page_evidence_with_urls():
    spy = []
    crawler = Crawl4AIAdapter(
        {"enabled": True, "base_url": "http://localhost:11235/", "max_pages_per_site": 1},
        http=_crawl_http({"results": [{"url": "https://alpha.test/", "success": True,
                                       "markdown": {"raw_markdown": "Director Jane. hello@alpha.test"}}]}, spy))
    pages = crawler.crawl("https://alpha.test")
    assert [(p.url, "hello@alpha.test" in p.text) for p in pages] == [("https://alpha.test/", True)]
    url, payload, _, _ = spy[0]
    assert url == "http://localhost:11235/crawl" and payload == {"urls": ["https://alpha.test/"]}


def test_crawl4ai_skips_failed_pages_and_reports_unavailable_when_none_succeed():
    crawler = Crawl4AIAdapter({"enabled": True},
                              http=_crawl_http({"results": [{"url": "https://alpha.test/", "success": False}]}))
    with pytest.raises(AdapterUnavailable):
        crawler.crawl("https://alpha.test")


def test_crawl4ai_unexpected_payload_is_unavailable():
    crawler = Crawl4AIAdapter({"enabled": True}, http=_crawl_http({"detail": "no auth"}))
    with pytest.raises(AdapterUnavailable):
        crawler.crawl("https://alpha.test")


def test_crawl4ai_transport_error_is_unavailable():
    crawler = Crawl4AIAdapter({"enabled": True}, http=_crawl_http(AdapterUnavailable("connection refused")))
    with pytest.raises(AdapterUnavailable):
        crawler.crawl("https://alpha.test")


# ---- reacher ---------------------------------------------------------------------------

def _reacher_response(is_reachable, **extra):
    return {"input": "x@y.test", "is_reachable": is_reachable, **extra}


@pytest.mark.parametrize("payload,expected", [
    (_reacher_response("safe"), adapters.VALID),
    (_reacher_response("invalid", mx={"accepts_mail": False}), adapters.INVALID),
    (_reacher_response("risky", smtp={"is_catch_all": True}), adapters.CATCH_ALL),
    (_reacher_response("risky", smtp={"is_disabled": True}), adapters.RISKY),
    (_reacher_response("unknown"), adapters.UNKNOWN),
])
def test_interpret_reacher_maps_every_documented_state(payload, expected):
    state, detail = interpret_reacher(payload)
    assert state == expected and detail


def test_interpret_reacher_rejects_anything_it_does_not_understand():
    for payload in ({"is_reachable": "banana"}, {}, "not a dict"):
        with pytest.raises(AdapterUnavailable):
            interpret_reacher(payload)


def test_reacher_is_off_by_default():
    verifier = ReacherAdapter()
    assert verifier.available() is False
    with pytest.raises(AdapterUnavailable):
        verifier.verify("hello@alpha.test")


def test_reacher_verifies_once_per_address_and_never_sends():
    calls = []

    def http(url, payload, headers, timeout, method="POST"):
        calls.append((url, payload))
        return _reacher_response("safe")

    verifier = ReacherAdapter({"enabled": True, "from_email": "verify@pars.test"}, http=http)
    assert verifier.verify("Hello@Alpha.test")[0] == adapters.VALID
    assert verifier.verify("hello@alpha.test")[0] == adapters.VALID       # served from cache
    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/v0/check_email")
    assert payload == {"to_email": "Hello@Alpha.test", "from_email": "verify@pars.test"}


def test_reacher_transport_error_is_unavailable():
    def http(*a, **kw):
        raise AdapterUnavailable("port 25 blocked")

    with pytest.raises(AdapterUnavailable):
        ReacherAdapter({"enabled": True}, http=http).verify("hello@alpha.test")


def test_build_adapters_from_an_empty_acquisition_block_yields_two_disabled_adapters():
    crawler, verifier = adapters.build_adapters({})
    assert crawler.available() is False and verifier.available() is False
