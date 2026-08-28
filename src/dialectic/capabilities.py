"""Fail-closed generic attestation and concrete per-run binding checks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from pydantic import ValidationError

from .filesystem import stable_filesystem_identity
from .schemas import (
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    DynamicFilesystemIdentity,
    TargetPreflightArtifact,
)
from .contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION
from .store import canonical_json_bytes


class CapabilityEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityFixture:
    probe_ids: tuple[str, ...]
    dynamic_roles: tuple[str, ...]
    template: dict[str, Any]

    @property
    def template_sha256(self) -> str:
        return _canonical_hash(self.template)


def validate_target_preflight(raw: bytes) -> TargetPreflightArtifact:
    try:
        return TargetPreflightArtifact.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise CapabilityEvidenceError("target preflight evidence is absent or invalid") from exc


def validate_cached_attestation(
    raw: bytes,
    *,
    fixture: CapabilityFixture,
    expected_fields: Mapping[str, str],
) -> CapabilityAttestationArtifact:
    try:
        artifact = CapabilityAttestationArtifact.model_validate_json(
            raw,
            strict=True,
            context={"probe_ids": fixture.probe_ids},
        )
    except ValidationError as exc:
        raise CapabilityEvidenceError("capability attestation is corrupt") from exc
    for field, expected in expected_fields.items():
        if getattr(artifact, field, object()) != expected:
            raise CapabilityEvidenceError(f"capability attestation is stale at {field}")
    if artifact.profile_template_sha256 != fixture.template_sha256:
        raise CapabilityEvidenceError("capability attestation template is stale")
    if not all(result.passed for result in artifact.probe_results):
        raise CapabilityEvidenceError("capability attestation contains a failed probe")
    results_hash = _canonical_hash(
        [result.model_dump(mode="json") for result in artifact.probe_results]
    )
    if artifact.probe_results_sha256 != results_hash:
        raise CapabilityEvidenceError("capability attestation probe-results hash mismatches")
    return artifact


def validate_or_probe_attestation(
    cached: bytes | None,
    *,
    fixture: CapabilityFixture,
    expected_fields: Mapping[str, str],
    probe: Callable[[], CapabilityAttestationArtifact],
) -> CapabilityAttestationArtifact:
    if cached is not None:
        try:
            return validate_cached_attestation(
                cached,
                fixture=fixture,
                expected_fields=expected_fields,
            )
        except CapabilityEvidenceError:
            pass
    fresh = probe()
    return validate_cached_attestation(
        canonical_json_bytes(fresh),
        fixture=fixture,
        expected_fields=expected_fields,
    )


def build_capability_binding(
    *,
    binding_id: str,
    role: str,
    target_id: str,
    access_mode: str,
    target_preflight_bytes: bytes,
    attestation_bytes: bytes,
    attestation: CapabilityAttestationArtifact,
    fixture: CapabilityFixture,
    dynamic_paths: Mapping[str, Path],
    supplied_concrete_profile: dict[str, Any],
) -> CapabilityBindingArtifact:
    preflight = validate_target_preflight(target_preflight_bytes)
    if preflight.role != role or preflight.target_id != target_id:
        raise CapabilityEvidenceError("target preflight role or target id mismatches the binding")
    attestation_sha256 = hashlib.sha256(attestation_bytes).hexdigest()
    if preflight.capability_attestation_sha256 != attestation_sha256:
        raise CapabilityEvidenceError("target preflight references different capability evidence")
    if preflight.target.runtime != attestation.runtime:
        raise CapabilityEvidenceError("target runtime mismatches the capability attestation")
    if hashlib.sha256(attestation_bytes).hexdigest() != hashlib.sha256(
        canonical_json_bytes(attestation)
    ).hexdigest():
        raise CapabilityEvidenceError("attestation bytes are not the validated artifact")
    if set(dynamic_paths) != set(fixture.dynamic_roles):
        raise CapabilityEvidenceError("dynamic role set does not exactly match the fixture")

    substitutions: dict[str, str] = {}
    for dynamic_role, path in dynamic_paths.items():
        substitutions[dynamic_role] = str(_resolve_dynamic_path(dynamic_role, path))
    identities = _capture_dynamic_identities(
        dynamic_paths,
        platform_backend=attestation.platform_backend,
    )
    expected_profile = _substitute_template(fixture.template, substitutions)
    if _canonical_bytes(expected_profile) != _canonical_bytes(supplied_concrete_profile):
        raise CapabilityEvidenceError("concrete profile is not the canonical template instantiation")
    identities.sort(key=lambda item: (item.role, item.path_sha256))
    return CapabilityBindingArtifact(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        binding_id=binding_id,
        role=role,
        target_id=target_id,
        access_mode=access_mode,
        target_preflight_artifact_sha256=hashlib.sha256(target_preflight_bytes).hexdigest(),
        capability_attestation_sha256=attestation_sha256,
        profile_template_sha256=fixture.template_sha256,
        concrete_profile_sha256=_canonical_hash(supplied_concrete_profile),
        dynamic_filesystem_identities=identities,
        canonical_instantiation_verified=True,
    )


def validate_binding_identities(
    binding: CapabilityBindingArtifact,
    *,
    dynamic_paths: Mapping[str, Path],
    platform_backend: str,
) -> None:
    """Revalidate every authoritative identity before a bound start or resume."""

    expected = _capture_dynamic_identities(
        dynamic_paths,
        platform_backend=platform_backend,
    )
    if binding.dynamic_filesystem_identities != expected:
        raise CapabilityEvidenceError(
            "capability binding dynamic path or filesystem identity changed"
        )


def _capture_dynamic_identities(
    dynamic_paths: Mapping[str, Path],
    *,
    platform_backend: str,
) -> list[DynamicFilesystemIdentity]:
    try:
        backend = platform_backend.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CapabilityEvidenceError("platform backend must be ASCII") from exc
    identities: list[DynamicFilesystemIdentity] = []
    for dynamic_role, path in dynamic_paths.items():
        resolved = _resolve_dynamic_path(dynamic_role, path)
        info = resolved.stat()
        canonical_key = os.path.normcase(str(resolved)) if os.name == "nt" else str(resolved)
        identities.append(
            DynamicFilesystemIdentity(
                role=dynamic_role,
                path_sha256=hashlib.sha256(
                    backend + b"\0" + canonical_key.encode("utf-8")
                ).hexdigest(),
                filesystem_identity=stable_filesystem_identity(resolved),
            )
        )
    identities.sort(key=lambda item: (item.role, item.path_sha256))
    return identities


def _resolve_dynamic_path(dynamic_role: str, path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise CapabilityEvidenceError(
            f"dynamic object {dynamic_role} does not exist before binding"
        ) from exc
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise CapabilityEvidenceError(f"dynamic object {dynamic_role} has unsupported type")
    directory_roles = {
        "turn_scratch_root",
        "turn_scratch_control",
        "turn_scratch_tmp",
        "neutral_role_dir",
    }
    if dynamic_role in directory_roles and not stat.S_ISDIR(info.st_mode):
        raise CapabilityEvidenceError(f"dynamic object {dynamic_role} must be a directory")
    return resolved


class BindingBarrier:
    def __init__(self, required_aliases: Iterable[str]) -> None:
        aliases = tuple(required_aliases)
        if not aliases or len(aliases) != len(set(aliases)):
            raise ValueError("binding barrier requires a non-empty unique cohort")
        self._required = frozenset(aliases)
        self._bindings: dict[str, CapabilityBindingArtifact] = {}

    def add(self, alias: str, binding: CapabilityBindingArtifact) -> None:
        if alias not in self._required or alias in self._bindings:
            raise CapabilityEvidenceError("unexpected or duplicate cohort binding")
        self._bindings[alias] = binding

    @property
    def ready(self) -> bool:
        return self._bindings.keys() == self._required

    def authorize_launch(self) -> tuple[CapabilityBindingArtifact, ...]:
        if not self.ready:
            raise CapabilityEvidenceError("launch barrier is closed until all bindings validate")
        return tuple(self._bindings[alias] for alias in sorted(self._bindings))


def _substitute_template(value: Any, substitutions: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"dynamic_path"}:
            role = value["dynamic_path"]
            if role not in substitutions:
                raise CapabilityEvidenceError(f"unknown dynamic template slot {role}")
            return substitutions[role]
        return {key: _substitute_template(child, substitutions) for key, child in value.items()}
    if isinstance(value, list):
        return [_substitute_template(child, substitutions) for child in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()
