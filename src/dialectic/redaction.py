"""Deliberately narrow known-value redaction and bounded stream capture."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Iterable, Mapping

from pydantic import ValidationError

from .contracts import ARTIFACT_SCHEMA_VERSION, REDACTION_MARKER, TOOL_VERSION, TRUNCATION_MARKER
from .schemas import DialecticConfig, RedactedConfigArtifact, StreamCaptureResult

CONFIG_REDACTION_MARKER = "redacted"


class CredentialBoundaryError(ValueError):
    pass


class RedactionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KnownCredential:
    name: str
    value: str


class KnownCredentials:
    def __init__(self, values: Iterable[KnownCredential] = ()) -> None:
        normalized: dict[str, KnownCredential] = {}
        for credential in values:
            if credential.value == "" or len(credential.value) < 8:
                raise CredentialBoundaryError(
                    f"credential environment value {credential.name} must contain at least "
                    "eight Unicode scalar values"
                )
            if any(0xD800 <= ord(character) <= 0xDFFF for character in credential.value):
                raise CredentialBoundaryError(
                    f"credential environment value {credential.name} contains a surrogate"
                )
            key = credential.name.casefold() if os.name == "nt" else credential.name
            previous = normalized.get(key)
            if previous is not None and previous.value != credential.value:
                raise CredentialBoundaryError(
                    f"credential environment name {credential.name} is supplied more than once"
                )
            normalized[key] = credential
        self._credentials = tuple(
            sorted(normalized.values(), key=lambda item: (-len(item.value.encode("utf-8")), item.name))
        )

    @classmethod
    def from_environment(
        cls, allowlisted_names: Iterable[str], environment: Mapping[str, str]
    ) -> "KnownCredentials":
        by_name = {name.casefold() if os.name == "nt" else name: name for name in allowlisted_names}
        credentials: list[KnownCredential] = []
        for supplied_name, value in environment.items():
            key = supplied_name.casefold() if os.name == "nt" else supplied_name
            if key in by_name:
                credentials.append(KnownCredential(by_name[key], value))
        return cls(credentials)

    @property
    def values(self) -> tuple[KnownCredential, ...]:
        return self._credentials

    @property
    def longest_utf8_bytes(self) -> int:
        return max((len(item.value.encode("utf-8")) for item in self._credentials), default=0)

    @property
    def longest_name(self) -> str | None:
        if not self._credentials:
            return None
        maximum = self.longest_utf8_bytes
        return min(
            item.name
            for item in self._credentials
            if len(item.value.encode("utf-8")) == maximum
        )

    def redact_bytes(self, value: bytes) -> bytes:
        redacted = value
        for credential in self._credentials:
            redacted = redacted.replace(credential.value.encode("utf-8"), REDACTION_MARKER)
        return redacted

    def redact_text(self, value: str) -> str:
        redacted = value
        marker = REDACTION_MARKER.decode("ascii")
        for credential in self._credentials:
            redacted = redacted.replace(credential.value, marker)
        return redacted

    def validate_stream_limits(self, *, stdout_bytes: int, stderr_bytes: int) -> None:
        required = 64 + len(TRUNCATION_MARKER) + max(0, self.longest_utf8_bytes - 1)
        for field, value in (
            ("max_agent_stdout_bytes", stdout_bytes),
            ("max_agent_stderr_bytes", stderr_bytes),
        ):
            if value < required:
                name = self.longest_name
                suffix = f" for credential environment {name}" if name is not None else ""
                raise CredentialBoundaryError(
                    f"{field} must be at least {required} bytes{suffix}"
                )


def redact_config(
    config: DialecticConfig,
    *,
    source_sha256: str,
    credentials: KnownCredentials,
) -> RedactedConfigArtifact:
    data = config.model_dump(mode="python")
    paths: list[str] = []

    def visit(value: object, path: tuple[str | int, ...]) -> object:
        if isinstance(value, str):
            redacted = value
            for credential in credentials.values:
                redacted = redacted.replace(credential.value, CONFIG_REDACTION_MARKER)
            if redacted != value:
                paths.append(_json_pointer(path))
            return redacted
        if isinstance(value, list):
            return [visit(child, (*path, index)) for index, child in enumerate(value)]
        if isinstance(value, dict):
            return {key: visit(child, (*path, key)) for key, child in value.items()}
        return value

    substituted = visit(data, ())
    try:
        validated = DialecticConfig.model_validate(substituted)
    except ValidationError as exc:
        raise RedactionError(
            "known-value substitution did not preserve the complete DialecticConfig schema"
        ) from exc
    return RedactedConfigArtifact(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        source_sha256=source_sha256,
        normalized_config=validated,
        redacted_field_paths=sorted(set(paths)),
    )


@dataclass(frozen=True, slots=True)
class CapturedStream:
    persisted: bytes
    result: StreamCaptureResult


class BoundedStreamCapture:
    """Collect no more than a stream cap plus the caller's current read chunk."""

    def __init__(self, limit: int, credentials: KnownCredentials) -> None:
        if limit <= len(TRUNCATION_MARKER):
            raise ValueError("stream limit cannot retain the truncation marker")
        self._limit = limit
        self._credentials = credentials
        self._accepted = bytearray()
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def accepted_bytes(self) -> int:
        return len(self._accepted)

    def feed(self, chunk: bytes) -> bool:
        """Return true only for the atomic first overflow transition."""

        if self._overflowed:
            return False
        remaining = self._limit - len(self._accepted)
        self._accepted.extend(chunk[:remaining])
        if len(chunk) > remaining:
            return self.mark_overflow()
        return False

    def mark_overflow(self) -> bool:
        """Record an externally detected one-shot overflow, such as Windows handoff."""

        if self._overflowed:
            return False
        self._overflowed = True
        return True

    def finish(self) -> CapturedStream:
        accepted = bytes(self._accepted)
        discarded_guard = 0
        if self._overflowed:
            guard = max(0, self._credentials.longest_utf8_bytes - 1)
            discarded_guard = min(guard, len(accepted))
            safe_prefix = accepted[: len(accepted) - discarded_guard]
            redacted = self._credentials.redact_bytes(safe_prefix)
            available = self._limit - len(TRUNCATION_MARKER)
            persisted = redacted[:available] + TRUNCATION_MARKER
        else:
            persisted = self._credentials.redact_bytes(accepted)
        result = StreamCaptureResult(
            configured_limit_bytes=self._limit,
            accepted_pre_redaction_bytes=len(accepted),
            accepted_pre_redaction_sha256=hashlib.sha256(accepted).hexdigest(),
            discarded_guard_bytes=discarded_guard,
            truncated=self._overflowed,
            persisted_bytes=len(persisted),
            persisted_sha256=hashlib.sha256(persisted).hexdigest(),
            triggered_termination=self._overflowed,
        )
        return CapturedStream(persisted, result)


def _json_pointer(path: tuple[str | int, ...]) -> str:
    return "/" + "/".join(
        str(component).replace("~", "~0").replace("/", "~1") for component in path
    )
