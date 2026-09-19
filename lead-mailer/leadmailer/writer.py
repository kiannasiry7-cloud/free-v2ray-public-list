"""Writer: turns candidate leads into drafts. Uses the profile's brief + templates and (optionally) an LLM.
It only writes drafts. It never sends."""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ConfigError, Settings
from .db import Database
from .llm import LLMError, Provider
from . import states

# Default rules, always on: no pricing, no timeline promises, no unverified numbers.
PRICING_RE = re.compile(r"(\$\s?\d|\bAUD\b|\bdollars?\b|\bper (hour|sqm|square|room)\b|\bquote of\b|\bdiscount\b|\d+\s?%)", re.I)
TIMELINE_RE = re.compile(
    r"(\bwithin\s+\d+\s*(hours?|days?|weeks?|months?)\b|\bin\s+\d+\s*(hours?|days?|weeks?)\b|"
    r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week|the end of)\b|"
    r"\bguarantee[ds]?\b|\bfinish(ed)?\s+(it\s+)?in\b|\bturnaround\b|\bsame[- ]day\b)",
    re.I,
)
NUMBER_CLAIM_RE = re.compile(r"\b(\d+\+?)\s*(years?|clients?|customers?|projects?|homes?|jobs?|reviews?|stars?)\b", re.I)


@dataclass
class WriterResult:
    lead_id: int
    email: str
    status: str
    generator: str
    reason: str | None


def load_brief(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"brief not found: {path}")
    with open(path, encoding="utf-8") as f:
        brief = yaml.safe_load(f) or {}
    for key in ("tone", "offer", "forbidden", "sender_name", "facts"):
        if key not in brief:
            raise ConfigError(f"brief {path}: missing {key}")
    return brief


def load_template(templates_dir: Path, name: str) -> tuple[str, str]:
    subject = templates_dir / f"{name}.subject.txt"
    body = templates_dir / f"{name}.body.md"
    if not subject.exists() or not body.exists():
        raise ConfigError(f"template {name} needs {subject.name} and {body.name} in {templates_dir}")
    return subject.read_text(encoding="utf-8").strip(), body.read_text(encoding="utf-8").strip()


def render(text: str, ctx: dict) -> str:
    try:
        return text.format(**ctx)
    except KeyError as e:
        raise ConfigError(f"template uses unknown placeholder {e}")


def check_rules(text: str, brief: dict) -> list[str]:
    """Return a list of violated rules (empty = ok)."""
    violations = []
    if PRICING_RE.search(text):
        violations.append("pricing")
    if TIMELINE_RE.search(text):
        violations.append("timeline_promise")
    facts_blob = " ".join(str(v) for v in brief.get("facts") or []).lower()
    for m in NUMBER_CLAIM_RE.finditer(text):
        if m.group(0).lower() not in facts_blob:
            violations.append(f"unverified_number:{m.group(0)}")
    for word in brief.get("forbidden") or []:
        if re.search(rf"\b{re.escape(str(word))}\b", text, re.I):
            violations.append(f"forbidden:{word}")
    return violations


def build_prompt(brief: dict, business: str, lead, template_body: str) -> tuple[str, str]:
    facts = "\n".join(f"- {f}" for f in brief.get("facts") or []) or "- (none given)"
    forbidden = ", ".join(str(w) for w in brief.get("forbidden") or []) or "(none)"
    system = (
        f"You write short, plain-text outreach emails for {business}. Tone: {brief['tone']}. "
        "Rules: use ONLY the facts listed; never invent claims, numbers, awards or clients; "
        "never mention prices, discounts or costs; never promise timelines or completion dates; "
        f"never use these words: {forbidden}. Output the email body only, no subject line, no markdown."
    )
    prompt = (
        f"Recipient company: {lead['company']}\nSuburb: {lead['suburb']}\nSegment: {lead['segment']}\n"
        f"Offer to describe: {brief['offer']}\nFacts you may use:\n{facts}\n"
        f"Sign off as: {brief['sender_name']}\n\n"
        f"Here is a template for length and structure; rewrite it naturally for this recipient:\n\n{template_body}"
    )
    return system, prompt


def run_writer(db: Database, settings: Settings, profile_name: str, provider: Provider,
               limit: int | None = None, template_name: str = "default") -> list[WriterResult]:
    profile = settings.profile(profile_name)
    brief = load_brief(settings.path(profile["brief"]))
    subject_tpl, body_tpl = load_template(settings.path(profile["templates_dir"]), template_name)
    fallback = bool(settings.get("llm.fallback_to_template", default=True, required=False))
    results: list[WriterResult] = []

    for lead in db.leads_by_status(states.CANDIDATE, profile_name, limit):
        ctx = {
            "company": lead["company"], "suburb": lead["suburb"], "segment": lead["segment"],
            "business": profile["business"], "offer": str(brief["offer"]).strip(), "sender_name": str(brief["sender_name"]).strip(),
        }
        subject = render(subject_tpl, ctx)
        template_body = render(body_tpl, ctx)
        generator, body = "template", template_body
        if provider.available():
            system, prompt = build_prompt(brief, profile["business"], lead, template_body)
            try:
                body, generator = provider.complete(prompt, system), provider.name
            except LLMError as e:
                if not fallback:
                    results.append(WriterResult(lead["id"], lead["email"], lead["status"], "none", f"llm_error:{e}"))
                    continue
        violations = check_rules(subject + "\n" + body, brief)
        if violations:
            reason = "rule_violation:" + ",".join(violations)
            db.insert_draft(lead["id"], subject, body, generator + ":rejected")
            db.transition(lead["id"], states.QUARANTINED, reason)
            results.append(WriterResult(lead["id"], lead["email"], states.QUARANTINED, generator, reason))
            continue
        db.insert_draft(lead["id"], subject, body, generator)
        db.transition(lead["id"], states.DRAFTED)
        results.append(WriterResult(lead["id"], lead["email"], states.DRAFTED, generator, None))
    return results
