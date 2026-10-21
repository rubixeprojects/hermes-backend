"""Behavior contracts for the standalone backend source copier."""
from pathlib import Path

import pytest

from scripts.copy_backend import generate


def _source(root: Path) -> Path:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1"\nreadme = "README.md"\n'
        '[tool.setuptools.packages.find]\ninclude = ["agent", "agent.*"]\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("backend fixture\n", encoding="utf-8")
    (root / "run_agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "agent").mkdir()
    (root / "agent/__init__.py").write_text("", encoding="utf-8")
    (root / "agent/assets").mkdir()
    (root / "agent/assets/prompt.txt").write_text("asset\n", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills/example.md").write_text("skill\n", encoding="utf-8")
    (root / "ui-tui").mkdir()
    (root / "ui-tui/package.json").write_text("{}", encoding="utf-8")
    (root / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    return root


def test_copies_backend_packages_resources_and_root_modules_but_not_ui_or_secrets(tmp_path):
    source = _source(tmp_path / "source")
    output = tmp_path / "hermes-backend"

    generate(source, output)

    assert (output / "agent/__init__.py").is_file()
    assert (output / "agent/assets/prompt.txt").read_text(encoding="utf-8") == "asset\n"
    assert (output / "run_agent.py").is_file()
    assert (output / "skills/example.md").is_file()
    assert (output / "README.md").is_file()
    assert (output / "HERMES_BACKEND_README.md").is_file()
    assert not (output / "ui-tui").exists()
    assert not (output / ".env").exists()


def test_refuses_to_overwrite_unowned_destination(tmp_path):
    source = _source(tmp_path / "source")
    output = tmp_path / "hermes-backend"
    output.mkdir()
    (output / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate(source, output)

    assert (output / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_can_replace_its_own_generated_output_on_rerun(tmp_path):
    source = _source(tmp_path / "source")
    output = tmp_path / "hermes-backend"

    generate(source, output)
    (output / "stale.txt").write_text("remove on regeneration", encoding="utf-8")
    generate(source, output, replace=True)

    assert (output / "agent/__init__.py").is_file()
    assert not (output / "stale.txt").exists()


def test_rejects_destination_inside_source_unless_it_is_the_backend_output(tmp_path):
    source = _source(tmp_path / "source")

    with pytest.raises(ValueError, match="inside the source"):
        generate(source, source / "agent" / "generated")

    output = source / "hermes-backend"
    generate(source, output)
    assert (output / "agent/__init__.py").is_file()
