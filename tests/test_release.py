from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from dialectic import TOOL_VERSION, __version__
from dialectic.config import ConfigLoader
from dialectic.contracts import ARTIFACT_SCHEMA_VERSION


def test_release_metadata_and_entry_points_are_versioned() -> None:
    root = Path(__file__).parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["version"] == TOOL_VERSION == __version__ == "0.1.0"
    assert importlib.metadata.version("dialectic") == "0.1.0"
    assert ARTIFACT_SCHEMA_VERSION == 1
    assert project["scripts"] == {
        "dial": "dialectic.cli:main",
        "dialectic": "dialectic.cli:main",
        "dialectic-desktop": "dialectic.desktop:main",
        "dialectic-ui": "dialectic.ui:main",
    }


def test_windows_desktop_launcher_prefers_the_checkout_and_has_installed_fallback() -> None:
    launcher = (
        Path(__file__).parents[1] / "Launch Dialectic Desktop.cmd"
    ).read_text(encoding="utf-8")
    assert ".venv\\Scripts\\pythonw.exe" in launcher
    assert "-m dialectic.desktop" in launcher
    assert "where.exe dialectic-desktop.exe" in launcher
    assert "dialectic.ui" not in launcher


def test_release_examples_validate_for_both_modes() -> None:
    root = Path(__file__).parents[1]
    environment = {
        "CODEX_DRIVER_MODEL": "codex-model",
        "CLAUDE_REVIEW_MODEL": "claude-model",
        "GROK_REVIEW_MODEL": "grok-model",
        "CODEX_COUNCIL_MODEL": "codex-council-model",
        "CLAUDE_COUNCIL_MODEL": "claude-council-model",
        "GROK_COUNCIL_MODEL": "grok-council-model",
    }
    config = (root / "examples" / "dialectic.yaml").read_bytes()
    for mode in ("code", "council"):
        assert ConfigLoader(environment).load(config, mode=mode).config.version == 1
    for name in ("task.md", "prompt.md"):
        payload = (root / "examples" / name).read_bytes()
        assert payload and payload.decode("utf-8")


def test_release_readme_pins_operational_boundaries() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    required = (
        "trusted local use",
        "setsid()",
        "DIALECTIC_LIVE",
        "git worktree remove",
        "git branch -D dialectic/<run-id>",
        "git worktree prune",
        "Failed and cancelled runs are deliberately retained",
        "ignored local artifacts",
        "controller does not inject a repository/worktree path",
        "paid quota",
        "%LOCALAPPDATA%\\dialectic\\runs\\<run-id>\\",
        "${XDG_STATE_HOME:-~/.local/state}/dialectic/runs/<run-id>/",
    )
    assert all(item in readme for item in required)
