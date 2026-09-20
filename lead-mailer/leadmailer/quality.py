"""Deterministic lead quality scoring and the verification gate.

Nothing here calls the network or an LLM: the same inputs always produce the same score and
the same reason string, so a stored score can be re-derived and argued with later.

The gate is deliberately conservative. Only `valid` (Reacher said safe) and `unverified`
(no verifier ran, which is the pre-existing behaviour of this project) may become a
send-ready candidate. `invalid`, `risky`, `catch_all` and `unknown` are quarantined, and the
only way out of quarantine is a human running `leadmailer release --id N --reason ...`.
"""
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import adapters

# local-parts that usually reach a decision maker at a small business
DECISION_MAKER_PARTS = ("director", "owner", "principal", "founder", "manager", "gm", "ceo",
                        "head", "partner", "proprietor")
# local-parts that reach the business, but not a named person
ROLE_PARTS = ("info", "contact", "hello", "enquiries", "enquiry", "inquiries", "office",
              "admin", "reception", "sales", "team", "mail")
# local-parts that are not worth a cold email even though they parse
LOW_VALUE_PARTS = ("careers", "jobs", "recruitment", "support", "help", "billing", "accounts",
                   "invoice", "privacy", "legal", "abuse", "webmaster", "marketing", "press", "media")
DECISION_MAKER_TITLES = ("director", "owner", "principal", "founder", "general manager",
                         "managing director", "operations manager", "property manager",
                         "portfolio manager", "facilities manager", "building manager")
FREE_MAILBOX_DOMAINS = ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.com.au",
                        "bigpond.com", "icloud.com", "live.com", "optusnet.com.au")

MAX_SCORE = 100


@dataclass
class Quality:
    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class LeadSignals:
    """Everything the score is allowed to look at."""
    email: str
    company: str = ""
    suburb: str = ""
    website: str = ""
    phone: str = ""
    evidence_url: str = ""
    page_text: str = ""
    provenance: str = ""
    verification: str = adapters.UNVERIFIED
    profile_suburbs: tuple = ()
    profile_keywords: tuple = ()


def domain_of(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "@" in value:
        return value.rsplit("@", 1)[1].lower()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.netloc or "").lower().removeprefix("www.")


def _local_part(email: str) -> str:
    return (email or "").split("@", 1)[0].lower()


def _matches(parts, local: str) -> bool:
    return any(re.search(rf"(^|[._-]){re.escape(p)}([._-]|\d|$)", local) for p in parts)


def score_lead(sig: LeadSignals) -> Quality:
    """0-100. Higher means: a real, locally relevant business whose address likely reaches a decider."""
    score, reasons = 0, []
    local = _local_part(sig.email)
    email_domain = domain_of(sig.email)
    site_domain = domain_of(sig.website or sig.evidence_url)

    if site_domain and email_domain == site_domain:
        score += 25
        reasons.append("email_on_company_domain")
    elif email_domain in FREE_MAILBOX_DOMAINS:
        score += 5
        reasons.append("free_mailbox_domain")
    elif site_domain:
        reasons.append("email_domain_differs_from_website")

    if _matches(LOW_VALUE_PARTS, local):
        score -= 20
        reasons.append("low_value_mailbox")
    elif _matches(DECISION_MAKER_PARTS, local):
        score += 20
        reasons.append("decision_maker_mailbox")
    elif _matches(ROLE_PARTS, local):
        score += 8
        reasons.append("role_mailbox")
    elif "." in local or len(local) > 3:
        score += 12
        reasons.append("personal_mailbox")

    text = f"{sig.company} {sig.page_text}".lower()
    if any(t in text for t in DECISION_MAKER_TITLES):
        score += 10
        reasons.append("decision_maker_named_on_site")

    hits = sorted({k for k in sig.profile_keywords if k and k.lower() in text})
    if hits:
        score += min(15, 5 * len(hits))
        reasons.append("segment_match:" + ",".join(hits[:3]))

    if sig.suburb and any(sig.suburb.lower() == s.lower() for s in sig.profile_suburbs):
        score += 10
        reasons.append("in_target_suburb")

    if sig.evidence_url:
        score += 10
        reasons.append("crawled_evidence")
    if sig.website:
        score += 5
        reasons.append("has_website")
    if sig.phone:
        score += 5
        reasons.append("has_phone")

    if sig.verification == adapters.VALID:
        score += 15
        reasons.append("verified_valid")
    elif sig.verification == adapters.UNVERIFIED:
        reasons.append("unverified")
    else:
        reasons.append(f"verification_{sig.verification}")

    score = max(0, min(MAX_SCORE, score))
    return Quality(score=score, reasons=reasons or ["no_signals"])


def verification_block(verification: str, require_verification: bool) -> str | None:
    """Reason this lead must not become send-ready, or None."""
    if verification == adapters.VALID:
        return None
    if verification == adapters.UNVERIFIED:
        return "verification:unverified" if require_verification else None
    return f"verification:{verification}"
