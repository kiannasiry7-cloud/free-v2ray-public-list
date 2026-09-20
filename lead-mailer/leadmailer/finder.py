"""Finder: read a profile's sources, extract leads, de-duplicate, grade them, store as candidate.

This is the one discovery flow. Optional external components (google-maps-scraper exports,
crawl4ai, reacher) plug into it as extra source types and adapters; when they are off or
unreachable the finder behaves exactly as it did before them. See leadmailer/adapters.py.
"""
import csv
import html
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .config import ConfigError, Settings
from .db import Database
from .guards import normalize_email
from .llm import LLMError, Provider
from .quality import LeadSignals, score_lead, verification_block
from . import adapters, states

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
JUNK_DOMAIN_PARTS = ("example.", "sentry.", "wixpress.", "w3.org", "schema.org")
JUNK_LOCAL_PARTS = ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


@dataclass
class RawLead:
    company: str
    segment: str
    suburb: str
    email: str
    source_url: str
    website: str = ""
    phone: str = ""
    evidence_url: str = ""          # exact public page the address was read from
    provenance: str = "csv_file"
    page_text: str = ""             # public page text, kept only to grade the lead


@dataclass
class FinderResult:
    lead_id: int
    company: str
    suburb: str
    email: str
    status: str
    reason: str | None
    quality_score: int = 0
    verification: str = adapters.UNVERIFIED


def plausible_email(email: str) -> bool:
    email = normalize_email(email)
    if not EMAIL_RE.fullmatch(email):
        return False
    local, _, domain = email.partition("@")
    if any(p in local for p in JUNK_LOCAL_PARTS):
        return False
    if any(p in domain for p in JUNK_DOMAIN_PARTS):
        return False
    if domain.endswith(IMAGE_SUFFIXES):
        return False
    return True


def default_fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "leadmailer/0.1 (+public directory lookup)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise ConfigError(f"fetch failed for {url}: {e}") from e


def _title_or_domain(page: str, url: str) -> str:
    m = TITLE_RE.search(page)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
        if title:
            return title[:120]
    return urlparse(url).netloc or url


def _llm_company(provider: Provider, page: str, url: str, fallback: str) -> str:
    """Optional: let the finder's provider pick a clean company name out of the page. Falls back silently."""
    if not provider.available():
        return fallback
    snippet = re.sub(r"<[^>]+>", " ", page)
    snippet = re.sub(r"\s+", " ", snippet)[:1500]
    try:
        out = provider.complete(
            f"Page URL: {url}\nPage text: {snippet}\n\nReply with only the business name on this page, nothing else."
        )
        out = out.strip().splitlines()[0].strip(" \"'.") if out.strip() else ""
        return out[:120] if 2 <= len(out) <= 120 else fallback
    except LLMError:
        return fallback


def _expand_urls(template: str, profile: dict) -> list[tuple[str, str]]:
    """Expand {suburb} / {keyword} placeholders. Returns (url, suburb) pairs."""
    suburbs = profile["suburbs"] if "{suburb}" in template else [""]
    keywords = profile["keywords"] if "{keyword}" in template else [""]
    out = []
    for suburb in suburbs:
        for keyword in keywords:
            out.append((template.format(suburb=suburb, keyword=keyword), suburb))
    return out


PAGE_TEXT_LIMIT = 4000


def _clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:PAGE_TEXT_LIMIT]


def emails_in(text: str) -> list[str]:
    return sorted({normalize_email(e) for e in EMAIL_RE.findall(text) if plausible_email(e)})


def business_leads(business: adapters.Business, profile: dict, crawler: adapters.Crawl4AIAdapter,
                   acquisition: dict, log) -> list[RawLead]:
    """One discovered business -> at most `max_contacts_per_business` graded contacts.

    Emails come from the scraper row and, when crawl4ai is enabled, from the business's own
    public website. If the crawl fails for any reason we keep whatever the scraper gave us.
    """
    found: dict[str, tuple[str, str]] = {}                 # email -> (crawled page url, page text)
    for email in business.emails:
        email = normalize_email(email)
        if plausible_email(email):
            found.setdefault(email, ("", ""))              # scraper row: no crawled page behind it
    pages: list[adapters.PageEvidence] = []
    if business.website and crawler.available():
        try:
            pages = crawler.crawl(business.website)
        except adapters.AdapterUnavailable as e:
            log(f"    crawl4ai unavailable for {business.website}: {e}")
    for page in pages:
        text = _clean_text(page.text)
        for email in emails_in(page.text):
            if not found.get(email, ("", ""))[0]:          # first crawled page wins; it is the evidence
                found[email] = (page.url, text)

    site_text = " ".join(_clean_text(p.text) for p in pages)[:PAGE_TEXT_LIMIT]
    keep = int(acquisition.get("max_contacts_per_business", 1) or 1)
    leads = []
    for email, (evidence_url, text) in found.items():
        leads.append(RawLead(
            company=business.name,
            segment=profile["segment"],
            suburb=business.suburb,
            email=email,
            source_url=business.source_url or business.website,
            website=business.website,
            phone=business.phone,
            evidence_url=evidence_url,
            # provenance describes where *this address* came from, not what else was crawled
            provenance="google_maps_scraper" + ("+crawl4ai" if evidence_url else ""),
            page_text=text or site_text,
        ))
    # pick the best addresses for this business by the same deterministic score used later
    leads.sort(key=lambda l: (-_pre_score(l, profile), l.email))
    return leads[:max(1, keep)]


def _pre_score(lead: RawLead, profile: dict) -> int:
    return score_lead(signals_for(lead, profile, adapters.UNVERIFIED)).score


def signals_for(lead: RawLead, profile: dict, verification: str) -> LeadSignals:
    return LeadSignals(
        email=lead.email, company=lead.company, suburb=lead.suburb, website=lead.website,
        phone=lead.phone, evidence_url=lead.evidence_url, page_text=lead.page_text,
        provenance=lead.provenance, verification=verification,
        profile_suburbs=tuple(profile.get("suburbs") or ()),
        profile_keywords=tuple(profile.get("keywords") or ()),
    )


def iter_source(source: dict, profile: dict, settings: Settings, provider: Provider, fetch=default_fetch,
                crawler: adapters.Crawl4AIAdapter | None = None, acquisition: dict | None = None,
                log=lambda msg: None) -> list[RawLead]:
    kind = source.get("type")
    crawler = crawler or adapters.Crawl4AIAdapter()
    acquisition = acquisition or {}
    if kind == "csv_file":
        path = settings.path(source["path"])
        if not path.exists():
            raise ConfigError(f"csv source not found: {path}")
        leads = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get("email"):
                    continue
                leads.append(RawLead(
                    company=(row.get("company") or "").strip() or row["email"].split("@")[1],
                    segment=(row.get("segment") or profile["segment"]).strip(),
                    suburb=(row.get("suburb") or "").strip(),
                    email=row["email"].strip(),
                    source_url=(row.get("source_url") or str(path)).strip(),
                    website=(row.get("website") or "").strip(),
                    phone=(row.get("phone") or "").strip(),
                    provenance="csv_file",
                ))
        return leads
    if kind == "html_page":
        leads = []
        for url, suburb in _expand_urls(source["url"], profile):
            page = fetch(url)
            company = _llm_company(provider, page, url, _title_or_domain(page, url))
            text = _clean_text(page)
            for email in emails_in(page):
                leads.append(RawLead(company=company, segment=profile["segment"], suburb=suburb, email=email,
                                     source_url=url, evidence_url=url, provenance="html_page", page_text=text))
        return leads
    if kind in ("maps_export", "maps_command"):
        suburbs = profile.get("suburbs") or ()
        try:
            if kind == "maps_export":
                businesses = adapters.load_maps_export(settings.path(source["path"]), suburbs)
            else:
                businesses = adapters.run_maps_command(
                    list(source.get("command") or []), float(source.get("timeout_seconds", 600)), suburbs)
        except adapters.AdapterUnavailable as e:
            log(f"  source {kind} skipped: {e}")      # graceful fallback: other sources still run
            return []
        leads = []
        for business in businesses:
            leads.extend(business_leads(business, profile, crawler, acquisition, log))
        return leads
    raise ConfigError(f"unknown source type: {kind!r} "
                      f"(known: csv_file, html_page, maps_export, maps_command)")


def run_finder(db: Database, settings: Settings, profile_name: str, provider: Provider, fetch=default_fetch,
               now: datetime | None = None, crawler: adapters.Crawl4AIAdapter | None = None,
               verifier: adapters.ReacherAdapter | None = None, log=lambda msg: None) -> list[FinderResult]:
    profile = settings.profile(profile_name)
    acquisition = settings.acquisition()
    if crawler is None or verifier is None:
        built_crawler, built_verifier = adapters.build_adapters(acquisition)
        crawler = crawler or built_crawler
        verifier = verifier or built_verifier
    now = now or datetime.now(timezone.utc)
    observed_at = now.isoformat(timespec="seconds")
    day = observed_at[:10]
    cap = int(profile["daily_cap"])
    run_cap = int(acquisition.get("max_candidates_per_run", 20) or 20)
    min_score = int(acquisition.get("min_quality_score", 0) or 0)
    require_verification = bool(acquisition.get("require_verification", False))
    already = db.leads_observed_on(profile_name, day)
    results: list[FinderResult] = []
    admitted = 0
    seen_this_run: set[str] = set()

    for source in profile["sources"]:
        for raw in iter_source(source, profile, settings, provider, fetch, crawler, acquisition, log):
            if already + len(results) >= cap or admitted >= run_cap:
                return results
            email = normalize_email(raw.email)
            if not plausible_email(email) or email in seen_this_run:
                continue
            seen_this_run.add(email)

            verification, detail = adapters.UNVERIFIED, None
            existing = db.lead_by_email(email)
            if existing is not None:
                status, reason, dup = states.REJECTED_DUPLICATE, f"duplicate of lead {existing['id']}", existing["id"]
            elif db.is_suppressed(email):
                status, reason, dup = states.QUARANTINED, "suppressed", None
            else:
                dup = None
                verification, detail = _verify(verifier, email, log)
                blocked = verification_block(verification, require_verification)
                score = score_lead(signals_for(raw, profile, verification)).score
                if blocked:  # invalid / risky / catch-all / unknown never reaches the writer
                    status, reason = states.QUARANTINED, blocked
                elif score < min_score:
                    status, reason = states.QUARANTINED, f"quality_below_threshold:{score}<{min_score}"
                else:
                    status, reason = states.CANDIDATE, None
                    admitted += 1
            quality = score_lead(signals_for(raw, profile, verification))  # graded for every stored row
            lead_id = db.insert_lead(
                profile_name, raw.company, raw.segment, raw.suburb, email, raw.source_url, observed_at,
                status=status, reason=reason, duplicate_of=dup,
                extra={"website": raw.website, "phone": raw.phone, "evidence_url": raw.evidence_url,
                       "provenance": raw.provenance, "verification": verification,
                       "verification_detail": detail, "quality_score": quality.score,
                       "quality_reason": quality.reason},
            )
            results.append(FinderResult(lead_id, raw.company, raw.suburb, email, status, reason,
                                        quality_score=quality.score, verification=verification))
    return results


def _verify(verifier: adapters.ReacherAdapter | None, email: str, log) -> tuple[str, str | None]:
    """No-send verification. A verifier that is off or unreachable leaves the lead `unverified`."""
    if verifier is None or not verifier.available():
        return adapters.UNVERIFIED, None
    try:
        return verifier.verify(email)
    except adapters.AdapterUnavailable as e:
        log(f"    reacher unavailable for {email}: {e}")
        return adapters.UNVERIFIED, "verifier_unavailable"
