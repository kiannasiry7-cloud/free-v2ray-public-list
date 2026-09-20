import pytest
import yaml

from leadmailer.cli import main
from leadmailer.config import ConfigError, Settings


def test_cli_end_to_end_dry_run(settings, capsys):
    s = str(settings.base_dir / "settings.yaml")
    assert main(["--settings", s, "finder"]) == 0
    assert main(["--settings", s, "writer"]) == 0
    assert main(["--settings", s, "approve", "--all"]) == 0
    assert main(["--settings", s, "sender"]) == 0
    assert main(["--settings", s, "status"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: 2 sent" in out and "approved=2" in out


def test_missing_key_is_an_error(tmp_path, settings):
    raw = yaml.safe_load((settings.base_dir / "settings.yaml").read_text())
    del raw["sender"]["hourly_cap"]
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="sender.hourly_cap"):
        Settings.load(tmp_path / "bad.yaml")


def test_unknown_provider_reference(tmp_path, settings):
    raw = yaml.safe_load((settings.base_dir / "settings.yaml").read_text())
    raw["llm"]["use"]["writer"] = "ghost"
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="ghost"):
        Settings.load(tmp_path / "bad.yaml")


def test_finder_output_shows_the_grade_and_verification_state(settings, capsys):
    s = str(settings.base_dir / "settings.yaml")
    assert main(["--settings", s, "finder"]) == 0
    out = capsys.readouterr().out
    assert "q=" in out and "unverified" in out          # no verifier configured: honest, not a pass


def test_acquisition_block_is_optional_but_must_be_a_mapping(tmp_path, settings):
    raw = yaml.safe_load((settings.base_dir / "settings.yaml").read_text())
    assert "acquisition" not in raw                     # the fixture strips it; see conftest.py
    raw["acquisition"] = ["crawl4ai"]
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="acquisition must be a mapping"):
        Settings.load(tmp_path / "bad.yaml").acquisition()


def test_an_unknown_source_type_names_the_ones_that_exist(tmp_path, settings):
    from leadmailer.db import Database
    from leadmailer.finder import run_finder
    from leadmailer.llm import NoneProvider
    raw = yaml.safe_load((settings.base_dir / "settings.yaml").read_text())
    raw["profiles"]["pars_default"]["sources"] = [{"type": "linkedin_scrape"}]
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(raw))
    bad = Settings.load(tmp_path / "s.yaml")
    with Database(bad.db_path()) as db:
        with pytest.raises(ConfigError, match="maps_export"):
            run_finder(db, bad, "pars_default", NoneProvider())
