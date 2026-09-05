"""Supplemental native executable selection extension contract tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dialectic.adapters import AgentRegistry
from dialectic.config import ConfigError, ConfigLoader
from dialectic.native_runtime import build_native_adapter
from dialectic.redaction import KnownCredentials
from dialectic.schemas import DialecticConfig
from dialectic.store import RunStore, canonical_json_bytes
from dialectic.ui import _prepare_run


def test_native_selections_preserve_old_config_serialization(config_bytes: bytes) -> None:
    config = ConfigLoader({}).load(config_bytes).config
    assert config.native_executables == {}
    assert "native_executables" not in config.model_dump()
    assert b"native_executables" not in canonical_json_bytes(config)
    assert canonical_json_bytes(DialecticConfig.model_validate_json(canonical_json_bytes(config))) == canonical_json_bytes(config)


def test_native_selections_expand_environment_and_stay_out_of_targets(
    tmp_path: Path, config_data: dict
) -> None:
    driver = str(tmp_path / "older CLI" / "codex.exe")
    reviewer = str(tmp_path / "newer CLI" / "codex.exe")
    config_data["native_executables"] = {"codex": {"driver": "${DRIVER_BIN}", "reviewer": reviewer}}
    raw = yaml.safe_dump(config_data).encode()
    config = ConfigLoader({"DRIVER_BIN": driver}).load(raw, mode="code").config
    assert config.model_dump()["native_executables"] == {"codex": {"driver": driver, "reviewer": reviewer}}
    targets = AgentRegistry.code_targets(config)
    assert targets[0] == targets[1][0][1]  # @driver keeps its model identity.
    assert "executable" not in json.dumps(targets[0].model_dump())
    assert config.model_dump(include={"driver"}) == {"driver": config.driver.model_dump()}
    assert "native_executables" not in config.model_dump(exclude={"native_executables"})
    with pytest.raises(ConfigError, match="missing or empty"):
        ConfigLoader({}).load(raw)


@pytest.mark.parametrize("bad", [
    [], None, {"unknown": {}}, {"codex": {"unknown": "/cli"}},
    {"codex": {"driver": None}}, {"codex": {"driver": ""}},
    {"codex": {"driver": "codex"}}, {"codex": {"driver": "./bin/codex"}},
    {"codex": {"driver": " /cli"}}, {"codex": {"driver": "/cli\n--flag"}},
    {"codex": {"driver": "/cli\0"}}, {"codex": {"driver": 1}},
])
def test_native_selections_reject_malformed_configuration(config_data: dict, bad: object) -> None:
    config_data["native_executables"] = bad
    with pytest.raises(ConfigError):
        ConfigLoader({}).load(yaml.safe_dump(config_data).encode())


def test_native_selections_forbid_non_codex_driver(tmp_path: Path, config_data: dict) -> None:
    config_data["native_executables"] = {"claude-code": {"driver": str(tmp_path / "claude.exe")}}
    with pytest.raises(ConfigError, match="only Codex"):
        ConfigLoader({}).load(yaml.safe_dump(config_data).encode())


@pytest.mark.parametrize("runtime,role", [
    ("codex", "driver"), ("codex", "reviewer"), ("codex", "participant"), ("codex", "moderator"),
    ("claude-code", "reviewer"), ("claude-code", "participant"), ("claude-code", "moderator"),
    ("grok-build", "reviewer"), ("grok-build", "participant"), ("grok-build", "moderator"),
])
def test_native_selection_is_by_runtime_and_execution_role(
    tmp_path: Path, config_data: dict, runtime: str, role: str
) -> None:
    from dialectic.schemas import AgentTarget
    selected = str(tmp_path / "selected CLI" / (runtime + ".exe"))
    config_data["native_executables"] = {runtime: {role: selected}}
    config = DialecticConfig.model_validate(config_data)
    environment = {"PATH": "original path"}
    adapter = build_native_adapter(
        AgentTarget(runtime=runtime, model="model"), role=role,
        access_mode="driver-write" if role == "driver" else "packet-only",
        config=config, store=RunStore(tmp_path / "state"), credentials=KnownCredentials(),
        source_environment=environment, probe_provider=None, turn_deadlines=None,
    )
    executable_name = {"codex": "codex", "claude-code": "claude", "grok-build": "grok"}[runtime]
    assert adapter.which(executable_name) == selected
    assert environment == adapter.source_environment == {"PATH": "original path"}


def test_browser_submission_carries_role_paths(tmp_path: Path) -> None:
    paths = {"codex": {"driver": str(tmp_path / "Sol CLI.exe"), "reviewer": str(tmp_path / "Astra CLI.exe")}}
    payload = {
        "mode": "code", "prompt": "Implement the task", "repository": str(tmp_path),
        "main": {"runtime": "codex", "model": "gpt-5.6-sol"},
        "agents": [{"runtime": "codex", "model": "gpt-6-astra"}],
        "nativeExecutables": paths,
    }
    prepared = _prepare_run(payload)
    config = ConfigLoader({}).load(prepared.config_bytes).config
    assert config.model_dump()["native_executables"] == paths
    assert prepared.prompt_bytes == b"Implement the task"
    for bad in ([], None, "", {"codex": {"reviewer": "relative"}}):
        with pytest.raises(ValueError):
            _prepare_run({**payload, "nativeExecutables": bad})
