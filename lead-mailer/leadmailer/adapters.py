"""Optional external components, all disabled by default and all behind an HTTP/CLI boundary.

Three upstream projects can be plugged in without vendoring any of their code:

  * gosom/google-maps-scraper  -> discovery. We read its CSV/JSON export (`maps_export`),
    or run a locally installed binary ourselves (`maps_command`).
  * unclecode/crawl4ai         -> public website crawling, through its HTTP server.
  * reacherhq/check-if-email-exists -> no-send email verification, through its HTTP API.
    Reacher is AGPL-3.0 / commercial dual licensed: it stays a separate process we talk to
    over HTTP, we never import or copy its source. See README "External components".

Every adapter answers `available()` honestly and raises `AdapterUnavailable` instead of
propagating network/Docker errors, so the finder can always fall back to what it had before.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

VALID = "valid"
INVALID = "invalid"
RISKY = "risky"
CATCH_ALL = "catch_all"
UNKNOWN = "unknown"
UNVERIFIED = "unverified"          # no verifier ran at all
SEND_READY_STATES = (VALID, UNVERIFIED)

DEFAULT_CRAWL_PATHS = ("", "contact", "contact-us", "about")


class AdapterUnavailable(Exception):
    """The external component is off, missing, unreachable or answered something unusable."""


@dataclass
class Business:
    """One public business record as discovered by the maps scraper."""
    name: str
    website: str = ""
    phone: str = ""
    suburb: str = ""
    source_url: str = ""
    query: str = ""
    emails: list[str] = field(default_factory=list)


@dataclass
class PageEvidence:
    url: str
    text: str


def _cfg(section: dict | None, key: str, default):
    if not isinstance(section, dict):
        return default
    value = section.get(key, default)
    return default if value is None else value


def _http_json(url: str, payload: dict | None, headers: dict, timeout: float, method: str = "POST") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AdapterUnavailable(f"{url}: {e}") from e


# ---- discovery: gosom/google-maps-scraper ----------------------------------------------

_MAPS_NAME_KEYS = ("title", "name", "business_name", "company")
_MAPS_WEBSITE_KEYS = ("website", "web_site", "site", "url")
_MAPS_PHONE_KEYS = ("phone", "phone_number", "international_phone_number")
_MAPS_LINK_KEYS = ("link", "google_maps_link", "cid_url", "place_url")
_MAPS_ADDRESS_KEYS = ("address", "complete_address", "full_address")
_MAPS_QUERY_KEYS = ("query", "input_id", "keyword", "search_term")


def _first(row: dict, keys) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):                      # complete_address is a dict in JSON exports
            value = value.get("borough") or value.get("city") or value.get("street") or ""
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _row_emails(row: dict) -> list[str]:
    out: list[str] = []
    for key in ("emails", "email", "email_address"):
        value = row.get(key)
        if isinstance(value, str):
            out.extend(p.strip() for p in re.split(r"[,;\s]+", value) if p.strip())
        elif isinstance(value, list):
            out.extend(str(p).strip() for p in value if str(p).strip())
    seen, uniq = set(), []
    for email in out:
        if email.lower() not in seen:
            seen.add(email.lower())
            uniq.append(email)
    return uniq


def _suburb_from(row: dict, known_suburbs) -> str:
    for key in ("suburb", "city", "locality"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    address = _first(row, _MAPS_ADDRESS_KEYS)
    for suburb in known_suburbs or ():
        if suburb and re.search(rf"\b{re.escape(suburb)}\b", address, re.I):
            return suburb
    return ""


def business_from_row(row: dict, known_suburbs=()) -> Business | None:
    name = _first(row, _MAPS_NAME_KEYS)
    if not name:
        return None
    return Business(
        name=name[:120],
        website=_first(row, _MAPS_WEBSITE_KEYS),
        phone=_first(row, _MAPS_PHONE_KEYS),
        suburb=_suburb_from(row, known_suburbs),
        source_url=_first(row, _MAPS_LINK_KEYS),
        query=_first(row, _MAPS_QUERY_KEYS),
        emails=_row_emails(row),
    )


def parse_maps_text(text: str, known_suburbs=(), fallback_source: str = "") -> list[Business]:
    """Accepts the scraper's JSON array, JSON-lines or CSV output — whichever the user exported."""
    rows: list[dict] = []
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        try:
            loaded = json.loads(stripped)
            rows = loaded if isinstance(loaded, list) else [loaded]
        except ValueError:                                 # JSON lines
            rows = []
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError as e:
                    raise AdapterUnavailable(f"unparseable maps JSON line: {e}") from e
    else:
        rows = list(csv.DictReader(stripped.splitlines()))
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        business = business_from_row(row, known_suburbs)
        if business is None:
            continue
        if not business.source_url:
            business.source_url = business.website or fallback_source
        out.append(business)
    return out


def load_maps_export(path: Path, known_suburbs=()) -> list[Business]:
    if not Path(path).exists():
        raise AdapterUnavailable(f"maps export not found: {path}")
    return parse_maps_text(Path(path).read_text(encoding="utf-8"), known_suburbs, fallback_source=str(path))


def run_maps_command(command: list[str], timeout: float, known_suburbs=(), runner=subprocess.run) -> list[Business]:
    """Run a locally installed google-maps-scraper and read its stdout. Never installs anything."""
    if not command:
        raise AdapterUnavailable("maps_command: empty command")
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        raise AdapterUnavailable(f"maps_command: {command[0]} is not installed")
    try:
        proc = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        raise AdapterUnavailable(f"maps_command failed: {e}") from e
    if proc.returncode != 0:
        raise AdapterUnavailable(f"maps_command exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return parse_maps_text(proc.stdout or "", known_suburbs, fallback_source=" ".join(command))


# ---- crawling: unclecode/crawl4ai ------------------------------------------------------

class Crawl4AIAdapter:
    """Talks to a crawl4ai server (`docker run ... unclecode/crawl4ai`) over HTTP.

    Public pages only: we request the site root and a few conventional public contact paths.
    No login, no cookies, no paths that require authentication.
    """

    name = "crawl4ai"

    def __init__(self, cfg: dict | None = None, http=_http_json):
        cfg = cfg or {}
        self.enabled = bool(_cfg(cfg, "enabled", False))
        self.base_url = str(_cfg(cfg, "base_url", "http://localhost:11235")).rstrip("/")
        self.timeout = float(_cfg(cfg, "timeout_seconds", 30))
        self.max_pages = int(_cfg(cfg, "max_pages_per_site", 3))
        self.paths = list(_cfg(cfg, "paths", list(DEFAULT_CRAWL_PATHS)))
        self.api_key = os.environ.get(str(_cfg(cfg, "api_key_env", "")), "")
        self._http = http

    def available(self) -> bool:
        return self.enabled and bool(self.base_url)

    def page_urls(self, website: str) -> list[str]:
        parsed = urlparse(website.strip() if "://" in website else f"https://{website.strip()}")
        host = parsed.netloc
        if not host or " " in host or "." not in host:     # scraper rows carry junk like "no website"
            return []
        root = f"{parsed.scheme}://{parsed.netloc}/"
        urls = []
        for path in self.paths:
            url = urljoin(root, str(path).lstrip("/"))
            if url not in urls:
                urls.append(url)
        return urls[: max(1, self.max_pages)]

    def crawl(self, website: str) -> list[PageEvidence]:
        if not self.available():
            raise AdapterUnavailable("crawl4ai is disabled")
        urls = self.page_urls(website)
        if not urls:
            raise AdapterUnavailable(f"crawl4ai: unusable website {website!r}")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        out = self._http(f"{self.base_url}/crawl", {"urls": urls}, headers, self.timeout)
        results = out.get("results") if isinstance(out, dict) else None
        if not isinstance(results, list):
            raise AdapterUnavailable("crawl4ai: response had no results list")
        pages = []
        for item, requested in zip(results, urls):
            if not isinstance(item, dict) or item.get("success") is False:
                continue
            pages.append(PageEvidence(url=str(item.get("url") or requested), text=_page_text(item)))
        if not pages:
            raise AdapterUnavailable(f"crawl4ai: no page succeeded for {website}")
        return pages


def _page_text(item: dict) -> str:
    markdown = item.get("markdown")
    if isinstance(markdown, dict):
        markdown = markdown.get("raw_markdown") or markdown.get("fit_markdown") or ""
    for value in (markdown, item.get("extracted_content"), item.get("cleaned_html"), item.get("html")):
        if isinstance(value, str) and value.strip():
            return value
    return ""


# ---- verification: reacherhq/check-if-email-exists -------------------------------------

class ReacherAdapter:
    """Calls a self-hosted Reacher backend's POST /v0/check_email. Verification only: no mail is sent.

    Reacher needs outbound port 25 to do SMTP checks; when it cannot, it answers `unknown`,
    which this project treats as not send-ready rather than as a pass.
    """

    name = "reacher"

    def __init__(self, cfg: dict | None = None, http=_http_json):
        cfg = cfg or {}
        self.enabled = bool(_cfg(cfg, "enabled", False))
        self.base_url = str(_cfg(cfg, "base_url", "http://localhost:8080")).rstrip("/")
        self.timeout = float(_cfg(cfg, "timeout_seconds", 30))
        self.from_email = str(_cfg(cfg, "from_email", "verify@example.com"))
        self.api_key = os.environ.get(str(_cfg(cfg, "api_key_env", "")), "")
        self._http = http
        self._cache: dict[str, tuple[str, str]] = {}

    def available(self) -> bool:
        return self.enabled and bool(self.base_url)

    def verify(self, email: str) -> tuple[str, str]:
        """Returns (state, detail). Raises AdapterUnavailable when Reacher cannot be reached."""
        if not self.available():
            raise AdapterUnavailable("reacher is disabled")
        key = email.lower()
        if key in self._cache:
            return self._cache[key]
        headers = {"Authorization": self.api_key} if self.api_key else {}
        payload = {"to_email": email, "from_email": self.from_email}
        out = self._http(f"{self.base_url}/v0/check_email", payload, headers, self.timeout)
        result = interpret_reacher(out)
        self._cache[key] = result
        return result


def interpret_reacher(out: dict) -> tuple[str, str]:
    if not isinstance(out, dict):
        raise AdapterUnavailable("reacher: unexpected response")
    reachable = str(out.get("is_reachable") or "").lower()
    smtp = out.get("smtp") if isinstance(out.get("smtp"), dict) else {}
    detail_bits = []
    if smtp.get("is_catch_all"):
        detail_bits.append("catch_all")
    if smtp.get("is_disabled"):
        detail_bits.append("disabled")
    if isinstance(out.get("misc"), dict) and out["misc"].get("is_disposable"):
        detail_bits.append("disposable")
    if isinstance(out.get("mx"), dict) and out["mx"].get("accepts_mail") is False:
        detail_bits.append("no_mx")
    detail = ",".join(detail_bits) or reachable or "no_state"

    if reachable == "safe":
        return VALID, detail
    if reachable == "invalid":
        return INVALID, detail
    if reachable == "risky":
        return (CATCH_ALL, detail) if smtp.get("is_catch_all") else (RISKY, detail)
    if reachable == "unknown":
        return UNKNOWN, detail
    raise AdapterUnavailable(f"reacher: unknown is_reachable {reachable!r}")


def build_adapters(acquisition: dict | None) -> tuple[Crawl4AIAdapter, ReacherAdapter]:
    acquisition = acquisition or {}
    return Crawl4AIAdapter(acquisition.get("crawl4ai")), ReacherAdapter(acquisition.get("reacher"))
