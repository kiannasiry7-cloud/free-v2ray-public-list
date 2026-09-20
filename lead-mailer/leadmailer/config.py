"""settings.yaml loader. Values are read from the file only; missing keys are errors, not defaults."""
import os
from pathlib import Path

import yaml

REQUIRED = [
    "app.db_path",
    "app.timezone",
    "llm.providers",
    "llm.use.finder",
    "llm.use.writer",
    "llm.use.sender",
    "mailboxes",
    "sender.dry_run",
    "sender.hourly_cap",
    "sender.daily_cap",
    "sender.spacing_seconds",
    "sender.warmup.enabled",
    "sender.warmup.steps",
    "sender.unsubscribe_footer",
    "profiles",
]

PROFILE_REQUIRED = ["business", "brief", "templates_dir", "segment", "suburbs", "keywords", "daily_cap", "sources", "mailboxes"]
MAILBOX_REQUIRED = ["from_name", "from_email", "reply_to", "method"]
SMTP_REQUIRED = ["host", "port", "username", "password_env", "starttls"]


# Optional block. Absent settings.yaml keys keep the pre-existing behaviour exactly:
# both external adapters off, nothing gated on verification, no score threshold.
ACQUISITION_DEFAULTS = {
    "max_candidates_per_run": 20,      # low-volume policy, matches sender.daily_cap per mailbox
    "max_contacts_per_business": 1,    # one decision-maker address per company, never a list dump
    "min_quality_score": 0,            # raise this to make the finder pickier
    "require_verification": False,     # true = an unverified address cannot become a candidate
    "crawl4ai": {"enabled": False},
    "reacher": {"enabled": False},
}


class ConfigError(Exception):
    pass


class Settings:
    def __init__(self, raw: dict, base_dir: Path):
        self.raw = raw
        self.base_dir = Path(base_dir)
        self._validate()

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Settings":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"settings file not found: {p}")
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw, p.parent)

    def get(self, dotted: str, default=None, required: bool = True):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if required and default is None:
                    raise ConfigError(f"missing setting: {dotted}")
                return default
            node = node[part]
        return node

    def path(self, value: str) -> Path:
        """Resolve a path value (as written in the YAML) relative to the settings file directory."""
        p = Path(str(value))
        return p if p.is_absolute() else self.base_dir / p

    def db_path(self) -> Path:
        return self.path(self.get("app.db_path"))

    def acquisition(self) -> dict:
        """The optional `acquisition:` block, merged over defaults. Missing block == all defaults."""
        raw = self.get("acquisition", default={}, required=False) or {}
        if not isinstance(raw, dict):
            raise ConfigError("acquisition must be a mapping")
        merged = dict(ACQUISITION_DEFAULTS)
        merged.update(raw)
        return merged

    def profile(self, name: str) -> dict:
        profiles = self.get("profiles")
        if name not in profiles:
            raise ConfigError(f"unknown profile: {name} (known: {', '.join(profiles)})")
        return profiles[name]

    def profile_names(self) -> list[str]:
        return list(self.get("profiles").keys())

    def mailbox(self, name: str) -> dict:
        boxes = self.get("mailboxes")
        if name not in boxes:
            raise ConfigError(f"unknown mailbox: {name}")
        return dict(boxes[name], name=name)

    def _validate(self) -> None:
        for key in REQUIRED:
            self.get(key)
        profiles = self.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise ConfigError("profiles must be a non-empty mapping")
        for name, prof in profiles.items():
            for key in PROFILE_REQUIRED:
                if key not in prof:
                    raise ConfigError(f"profile {name}: missing {key}")
        providers = self.get("llm.providers") or {}
        for part in ("finder", "writer", "sender"):
            use = self.get(f"llm.use.{part}")
            if use not in (None, "none") and use not in providers:
                raise ConfigError(f"llm.use.{part} = {use!r} is not defined under llm.providers")
        mailboxes = self.get("mailboxes")
        if not isinstance(mailboxes, dict) or not mailboxes:
            raise ConfigError("mailboxes must be a non-empty mapping")
        for name, box in mailboxes.items():
            for key in MAILBOX_REQUIRED:
                if key not in box:
                    raise ConfigError(f"mailbox {name}: missing {key}")
            if box["method"] not in ("smtp", "apple_mail"):
                raise ConfigError(f"mailbox {name}: method must be smtp or apple_mail")
            if box["method"] == "smtp":
                for key in SMTP_REQUIRED:
                    if key not in (box.get("smtp") or {}):
                        raise ConfigError(f"mailbox {name}: missing smtp.{key}")
        for name, prof in profiles.items():
            for box in prof["mailboxes"]:
                if box not in mailboxes:
                    raise ConfigError(f"profile {name}: mailbox {box!r} is not defined under mailboxes")
