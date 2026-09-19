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
