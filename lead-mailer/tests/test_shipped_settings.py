"""The shipped settings.yaml is itself a safety contract, so it is tested like code.

conftest.py deliberately strips the `acquisition:` block and the `acquisition_smoke` profile out
of the unit fixtures, so nothing else in this suite reads the file that actually runs. These tests
read the real one. Every assertion here is a thing that must not drift silently: a live sender, a
verification gate that has been loosened, a ceiling that has been raised, an external service
pointed off this machine, or a secret pasted where a variable name belongs.
"""
import re
from pathlib import Path

import yaml

from leadmailer import adapters
from leadmailer.config import Settings

ROOT = Path(__file__).resolve().parent.parent
RAW = yaml.safe_load((ROOT / "settings.yaml").read_text(encoding="utf-8"))
ACQ = Settings.load(ROOT / "settings.yaml").acquisition()


def test_the_sender_ships_in_dry_run():
    assert RAW["sender"]["dry_run"] is True, "a real send needs an owner decision, not a default"


def test_the_verification_gate_is_closed_and_the_ceilings_are_low():
    assert ACQ["require_verification"] is True      # unverified or unknown cannot become send-ready
    assert ACQ["max_candidates_per_run"] <= 20      # low volume, quality first
    assert ACQ["max_contacts_per_business"] == 1    # one address per company, never a list dump
    assert ACQ["min_quality_score"] > 0             # a bare address with no corroboration cannot pass


def test_no_verification_state_other_than_valid_can_reach_the_writer():
    """The gate the two settings above rely on, asserted directly rather than assumed."""
    from leadmailer.quality import verification_block
    require = ACQ["require_verification"]
    assert verification_block(adapters.VALID, require) is None
    for state in (adapters.UNVERIFIED, adapters.INVALID, adapters.RISKY,
                  adapters.CATCH_ALL, adapters.UNKNOWN):
        assert verification_block(state, require) is not None, f"{state} must be quarantined"


def test_external_services_stay_on_this_machine():
    for name in ("crawl4ai", "reacher"):
        url = str(ACQ[name].get("base_url", ""))
        assert re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", url), f"{name}: {url!r}"
        assert float(ACQ[name].get("timeout_seconds", 0)) > 0, f"{name} needs a timeout"


def test_credentials_are_referenced_by_variable_name_never_pasted():
    for name, box in RAW["mailboxes"].items():
        smtp = box.get("smtp") or {}
        assert "password" not in smtp, f"mailbox {name} must not carry an inline password"
        assert smtp["password_source"] in ("keychain", "env")
    for name in ("crawl4ai", "reacher"):
        assert "api_key" not in ACQ[name], f"{name}: name the env var, never the key"


def test_the_smoke_profile_cannot_reach_a_real_domain():
    """Its sample file is the only source, and every domain in it is under a reserved TLD."""
    profile = RAW["profiles"]["acquisition_smoke"]
    sources = profile["sources"]
    assert len(sources) == 1 and sources[0]["type"] == "maps_export"
    text = (ROOT / sources[0]["path"]).read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([^/\"]+)", text)) | {
        e.split("@", 1)[1] for e in re.findall(r"[\w.+-]+@[\w.-]+", text)}
    assert hosts, "the sample file should contain hosts to check"
    for host in hosts:
        assert host.endswith((".test", ".example", ".invalid", ".localhost")) or host == "example.com", host
