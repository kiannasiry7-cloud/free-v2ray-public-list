"""Finder: read a profile's sources, extract leads, de-duplicate, store as candidate."""
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
from . import states

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


@dataclass
class FinderResult:
    lead_id: int
    company: str
    suburb: str
    email: str
    status: str
    reason: str | None


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


def iter_source(source: dict, profile: dict, settings: Settings, provider: Provider, fetch=default_fetch) -> list[RawLead]:
    kind = source.get("type")
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
                ))
        return leads
    if kind == "html_page":
        leads = []
        for url, suburb in _expand_urls(source["url"], profile):
            page = fetch(url)
            company = _llm_company(provider, page, url, _title_or_domain(page, url))
            for email in sorted({e for e in EMAIL_RE.findall(page) if plausible_email(e)}):
                leads.append(RawLead(company=company, segment=profile["segment"], suburb=suburb, email=email, source_url=url))
        return leads
    raise ConfigError(f"unknown source type: {kind!r} (known: csv_file, html_page)")


def run_finder(db: Database, settings: Settings, profile_name: str, provider: Provider, fetch=default_fetch,
               now: datetime | None = None) -> list[FinderResult]:
    profile = settings.profile(profile_name)
    now = now or datetime.now(timezone.utc)
    observed_at = now.isoformat(timespec="seconds")
    day = observed_at[:10]
    cap = int(profile["daily_cap"])
    already = db.leads_observed_on(profile_name, day)
    results: list[FinderResult] = []
    seen_this_run: set[str] = set()

    for source in profile["sources"]:
        for raw in iter_source(source, profile, settings, provider, fetch):
            if already + len(results) >= cap:
                return results
            email = normalize_email(raw.email)
            if not plausible_email(email) or email in seen_this_run:
                continue
            seen_this_run.add(email)
            existing = db.lead_by_email(email)
            if existing is not None:
                status, reason, dup = states.REJECTED_DUPLICATE, f"duplicate of lead {existing['id']}", existing["id"]
            elif db.is_suppressed(email):
                status, reason, dup = states.QUARANTINED, "suppressed", None
            else:
                status, reason, dup = states.CANDIDATE, None, None
            lead_id = db.insert_lead(profile_name, raw.company, raw.segment, raw.suburb, email, raw.source_url,
                                     observed_at, status=status, reason=reason, duplicate_of=dup)
            results.append(FinderResult(lead_id, raw.company, raw.suburb, email, status, reason))
    return results
