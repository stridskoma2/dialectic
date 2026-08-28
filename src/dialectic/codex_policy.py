"""Offline construction contract for the future native Codex adapter."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class CodexPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CodexConstructionFixture:
    credential_environment_names: tuple[str, ...]
    non_secret_environment_names: tuple[str, ...]
    saved_auth_paths: tuple[Path, ...]
    fixture_version: str = "codex-offline-v1"


@dataclass(frozen=True, slots=True)
class CodexDriverConstruction:
    arguments: tuple[str, ...]
    trusted_environment: dict[str, str]
    child_environment_policy: dict[str, object]
    profile_template: dict[str, object]
    concrete_profile: dict[str, object]
    profile_sha256: str


def build_codex_driver_construction(
    *,
    fixture: CodexConstructionFixture,
    source_environment: Mapping[str, str],
    worktree: Path,
    git_common_dir: Path,
    original_worktree: Path,
    state_root: Path,
    scratch_root: Path,
    scratch_control: Path,
    scratch_tmp: Path,
    managed_policy: Mapping[str, object] | None = None,
) -> CodexDriverConstruction:
    _reject_displacing_policy(managed_policy or {})
    allowed_names = {
        *fixture.credential_environment_names,
        *fixture.non_secret_environment_names,
    }
    trusted_environment = {
        name: source_environment[name]
        for name in sorted(allowed_names, key=_environment_sort_key)
        if name in source_environment
    }
    missing = [
        name for name in fixture.non_secret_environment_names if name not in trusted_environment
    ]
    if missing:
        raise CodexPolicyError(f"required non-secret environment name is missing: {missing[0]}")
    for name in fixture.credential_environment_names:
        value = trusted_environment.get(name)
        if value is not None and len(value) < 8:
            raise CodexPolicyError(f"credential environment value is too short: {name}")
    child_policy = {
        "inherit": "core",
        "ignore_default_excludes": False,
        "experimental_use_profile": False,
        "exclude": sorted(fixture.credential_environment_names, key=_environment_sort_key),
        "set": {"GIT_OPTIONAL_LOCKS": "0"},
    }
    template = {
        "profile": "dialectic-driver",
        "approval_policy": "never",
        "network": "deny",
        "project_codex_config": "ignore-untrusted",
        "agents_md_discovery": "preserve",
        "subagents": "deny",
        "web_search": "deny",
        "mcp": "deny",
        "filesystem": [
            {"role": "isolated_worktree", "access": "read-write"},
            {"role": "git_common_dir", "access": "read-only-minimum"},
            {"role": "original_worktree", "access": "deny"},
            {"role": "state_root", "access": "deny"},
            {"role": "turn_scratch_root", "access": "protected"},
            {"role": "turn_scratch_control", "access": "read-only"},
            {"role": "turn_scratch_tmp", "access": "read-write-bounded"},
        ],
        "saved_auth_path_hashes": sorted(
            hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
            for path in fixture.saved_auth_paths
        ),
        "child_environment_policy": child_policy,
    }
    paths = {
        "isolated_worktree": worktree.resolve(strict=True),
        "git_common_dir": git_common_dir.resolve(strict=True),
        "original_worktree": original_worktree.resolve(strict=True),
        "state_root": state_root.resolve(strict=True),
        "turn_scratch_root": scratch_root.resolve(strict=True),
        "turn_scratch_control": scratch_control.resolve(strict=True),
        "turn_scratch_tmp": scratch_tmp.resolve(strict=True),
    }
    concrete = {
        **template,
        "filesystem": [
            {**rule, "path": str(paths[rule["role"]])}
            for rule in template["filesystem"]  # type: ignore[index]
        ],
    }
    canonical_overrides = tuple(
        item
        for key, value in sorted(concrete.items())
        for item in ("-c", f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}")
    )
    arguments = (
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        *canonical_overrides,
        "-",
    )
    profile_bytes = (
        json.dumps(concrete, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    return CodexDriverConstruction(
        arguments=arguments,
        trusted_environment=trusted_environment,
        child_environment_policy=child_policy,
        profile_template=template,
        concrete_profile=concrete,
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
    )


def _reject_displacing_policy(policy: Mapping[str, object]) -> None:
    forbidden = {"sandbox_mode", "sandbox_workspace_write", "danger-full-access"}
    displaced = forbidden.intersection(policy)
    if displaced or policy.get("profile") not in {None, "dialectic-driver"}:
        raise CodexPolicyError("managed policy displaces the dialectic-driver profile")


def _environment_sort_key(name: str) -> str:
    return name.casefold() if os.name == "nt" else name
