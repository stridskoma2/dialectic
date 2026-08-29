"""Offline construction contract for the future native Codex adapter."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    filesystem_template = {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
        "<isolated_worktree>": "write",
        "<isolated_worktree:.git>": "read",
        "<isolated_worktree:.codex>": "read",
        "<git_common_dir>": "read",
        "<original_worktree>": "deny",
        "<state_root>": "deny",
        "<turn_scratch_root>": "read",
        "<turn_scratch_control>": "read",
        "<turn_scratch_tmp>": "write",
        **{str(path.resolve(strict=False)): "deny" for path in fixture.saved_auth_paths},
        str(Path(tempfile.gettempdir()).resolve(strict=False)): "deny",
    }
    template = {
        "approval_policy": "never",
        "apps": {"_default": {"enabled": False}},
        "default_permissions": "dialectic-driver",
        "features": {"multi_agent": False},
        "mcp_servers": {},
        "permissions": {
            "dialectic-driver": {
                "filesystem": filesystem_template,
                "network": {"enabled": False},
            }
        },
        "projects": {"<isolated_worktree>": {"trust_level": "untrusted"}},
        "shell_environment_policy": child_policy,
        "web_search": "disabled",
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
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
        str(paths["isolated_worktree"]): "write",
        str(paths["isolated_worktree"] / ".git"): "read",
        str(paths["isolated_worktree"] / ".codex"): "read",
        str(paths["git_common_dir"]): "read",
        str(paths["original_worktree"]): "deny",
        str(paths["state_root"]): "deny",
        str(paths["turn_scratch_root"]): "read",
        str(paths["turn_scratch_control"]): "read",
        str(paths["turn_scratch_tmp"]): "write",
        **{str(path.resolve(strict=False)): "deny" for path in fixture.saved_auth_paths},
        str(Path(tempfile.gettempdir()).resolve(strict=False)): "deny",
    }
    concrete = {
        **template,
        "permissions": {
            "dialectic-driver": {
                "filesystem": filesystem,
                "network": {"enabled": False},
            }
        },
        "projects": {
            str(paths["isolated_worktree"]): {"trust_level": "untrusted"}
        },
    }
    canonical_overrides = tuple(
        item
        for key, value in sorted(concrete.items())
        for item in ("-c", f"{key}={_toml_literal(value)}")
    )
    arguments = (
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
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
    default = policy.get("default_permissions")
    allowed = policy.get("allowed_permission_profiles")
    profile_denied = isinstance(allowed, Mapping) and allowed.get("dialectic-driver") is False
    if displaced or default not in {None, "dialectic-driver"} or profile_denied:
        raise CodexPolicyError("managed policy displaces the dialectic-driver profile")


def _environment_sort_key(name: str) -> str:
    return name.casefold() if os.name == "nt" else name


def _toml_literal(value: object) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in {int, float}:
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        entries = (
            f"{json.dumps(str(key), ensure_ascii=False)}={_toml_literal(child)}"
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        )
        return "{" + ",".join(entries) + "}"
    raise CodexPolicyError("Codex override contains an unsupported value")
