import shutil
from pathlib import Path

import pytest
import yaml

from leadmailer.config import Settings
from leadmailer.db import Database

ROOT = Path(__file__).resolve().parent.parent


def make_settings(tmp_path: Path, **overrides) -> Settings:
    raw = yaml.safe_load((ROOT / "settings.yaml").read_text())
    raw["app"]["db_path"] = "db.sqlite"
    raw["sender"]["spacing_seconds"] = 0
    raw["llm"]["use"]["writer"] = "none"
    for box in raw["mailboxes"].values():
        box["smtp"]["host"] = "smtp.test"
        box["smtp"]["password_source"] = "env"
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    shutil.copytree(ROOT / "briefs", tmp_path / "briefs")
    shutil.copytree(ROOT / "templates", tmp_path / "templates")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "pars_manual.csv").write_text(
        "company,segment,suburb,email,source_url\n"
        "Alpha Realty,property_manager,Fitzroy,Hello@Alpha.test,https://alpha.test\n"
        "Beta Strata,strata,Carlton,office@beta.test,https://beta.test\n"
        "Dup Realty,property_manager,Richmond,hello@alpha.test,https://dup.test\n"
        "Junk,x,Y,noreply@junk.test,https://junk.test\n"
    )
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    return Settings.load(tmp_path / "settings.yaml")


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(settings):
    with Database(settings.db_path()) as d:
        yield d
