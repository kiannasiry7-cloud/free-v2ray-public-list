"""Scoring is pure and deterministic; the verification gate is deliberately conservative."""
import pytest

from leadmailer import adapters
from leadmailer.quality import LeadSignals, score_lead, verification_block

PROFILE = dict(profile_suburbs=("Fitzroy", "Carlton"), profile_keywords=("property manager", "strata"))


def signals(email, **kw):
    return LeadSignals(email=email, **{**PROFILE, **kw})


def test_score_is_deterministic():
    sig = signals("director@alpha.test", company="Alpha Realty", website="https://alpha.test",
                  suburb="Fitzroy", phone="03", evidence_url="https://alpha.test/contact",
                  page_text="Jane is our property manager")
    first = score_lead(sig)
    assert (first.score, first.reason) == (score_lead(sig).score, score_lead(sig).reason)


def test_a_decision_maker_on_the_company_domain_outscores_a_role_and_a_low_value_mailbox():
    common = dict(company="Alpha Realty", website="https://alpha.test", suburb="Fitzroy")
    decider = score_lead(signals("director@alpha.test", **common)).score
    role = score_lead(signals("info@alpha.test", **common)).score
    low = score_lead(signals("careers@alpha.test", **common)).score
    assert decider > role > low


def test_reasons_explain_the_score():
    q = score_lead(signals("owner@alpha.test", company="Alpha Realty", website="https://alpha.test",
                           suburb="Fitzroy", phone="03 9000 0000", evidence_url="https://alpha.test/contact",
                           page_text="our managing director", verification=adapters.VALID))
    for reason in ("email_on_company_domain", "decision_maker_mailbox", "in_target_suburb",
                   "crawled_evidence", "has_website", "has_phone", "verified_valid"):
        assert reason in q.reason


def test_an_off_domain_free_mailbox_with_no_evidence_scores_low():
    weak = score_lead(signals("info@gmail.com", company="Someone", website="https://alpha.test"))
    strong = score_lead(signals("info@alpha.test", company="Someone", website="https://alpha.test"))
    assert weak.score < strong.score
    assert "free_mailbox_domain" in weak.reason


def test_score_stays_inside_0_and_100():
    best = score_lead(signals("director.jane@alpha.test", company="Alpha Realty strata property manager",
                              website="https://alpha.test", suburb="Fitzroy", phone="03",
                              evidence_url="https://alpha.test/contact",
                              page_text="managing director, property manager, strata",
                              verification=adapters.VALID))
    worst = score_lead(signals("abuse@elsewhere.test", website="https://alpha.test",
                               verification=adapters.INVALID))
    assert 0 <= worst.score <= best.score <= 100
    assert worst.score == 0


def test_unverified_is_not_rewarded_but_not_punished():
    unverified = score_lead(signals("info@alpha.test", website="https://alpha.test"))
    valid = score_lead(signals("info@alpha.test", website="https://alpha.test", verification=adapters.VALID))
    assert valid.score > unverified.score
    assert "unverified" in unverified.reason


@pytest.mark.parametrize("state", [adapters.INVALID, adapters.RISKY, adapters.CATCH_ALL, adapters.UNKNOWN])
def test_only_valid_and_unverified_may_pass_the_gate(state):
    assert verification_block(state, require_verification=False) == f"verification:{state}"
    assert verification_block(state, require_verification=True) == f"verification:{state}"


def test_valid_always_passes_and_unverified_passes_only_when_not_required():
    assert verification_block(adapters.VALID, False) is None
    assert verification_block(adapters.VALID, True) is None
    assert verification_block(adapters.UNVERIFIED, False) is None          # pre-existing behaviour
    assert verification_block(adapters.UNVERIFIED, True) == "verification:unverified"
