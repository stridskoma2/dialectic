"""Strict UTF-8 and JSON-compatible YAML configuration loading."""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent
from yaml.tokens import AliasToken, AnchorToken, TagToken

from .contracts import MAX_NAMED_INPUT_BYTES, RunMode
from .schemas import DialecticConfig

_ENV_REFERENCE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_ESCAPED_ENV_REFERENCE = re.compile(r"\$\$\{([A-Z_][A-Z0-9_]*)\}")
_EXPANDABLE_FIELDS = frozenset({"model", "effort", "runtime", "lens", "id"})


class ConfigError(ValueError):
    """A credential-free invalid configuration diagnostic."""


class StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key and alias rejection."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ConfigError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConfigError("YAML mapping keys must be strings") from exc
            if duplicate:
                raise ConfigError(f"duplicate YAML mapping key: {key!r}")
            if key == "<<":
                raise ConfigError("YAML merge keys are not supported")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: DialecticConfig
    source_sha256: str


class ConfigLoader:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def load(self, raw: bytes, *, mode: RunMode | None = None) -> LoadedConfig:
        if len(raw) > MAX_NAMED_INPUT_BYTES:
            raise ConfigError("configuration exceeds the 262144-byte product ceiling")
        text = decode_scalar_utf8(raw, "configuration")
        parsed = self._parse_yaml(text)
        expanded = self._expand_environment(parsed)
        try:
            config = DialecticConfig.model_validate(expanded)
        except ValidationError as exc:
            raise ConfigError(_bounded_validation_message(exc)) from exc
        if len(raw) > config.limits.max_config_bytes:
            raise ConfigError(
                f"configuration byte count exceeds limits.max_config_bytes "
                f"({len(raw)} > {config.limits.max_config_bytes})"
            )
        if mode is not None:
            validate_mode(config, mode)
        return LoadedConfig(config, hashlib.sha256(raw).hexdigest())

    def _parse_yaml(self, text: str) -> Any:
        try:
            for token in yaml.scan(text):
                if isinstance(token, (AnchorToken, AliasToken, TagToken)):
                    raise ConfigError("YAML tags, anchors, and aliases are not supported")
            value = yaml.load(text, Loader=StrictSafeLoader)
        except ConfigError:
            raise
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid JSON-compatible YAML: {exc}") from exc
        _validate_json_compatible(value, path="$", root=True)
        return value

    def _expand_environment(self, value: Any, path: tuple[str | int, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {
                key: self._expand_environment(child, (*path, key))
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self._expand_environment(child, (*path, index))
                for index, child in enumerate(value)
            ]
        if not isinstance(value, str) or not path or path[-1] not in _EXPANDABLE_FIELDS:
            return value

        escaped: dict[str, str] = {}

        def protect(match: re.Match[str]) -> str:
            token = f"\x00dialectic-env-{len(escaped)}\x00"
            escaped[token] = "${" + match.group(1) + "}"
            return token

        protected = _ESCAPED_ENV_REFERENCE.sub(protect, value)
        match = _ENV_REFERENCE.fullmatch(protected)
        if match:
            name = match.group(1)
            resolved = self._environment.get(name)
            if resolved is None or resolved == "":
                raise ConfigError(f"environment variable {name} is missing or empty")
            _reject_surrogates(resolved, f"environment variable {name}")
            return resolved
        if "${" in protected:
            raise ConfigError(
                f"partial environment interpolation is not supported at {_json_path(path)}"
            )
        for token, literal in escaped.items():
            protected = protected.replace(token, literal)
        return protected


def validate_mode(config: DialecticConfig, mode: RunMode) -> None:
    if mode == "code":
        if config.driver is None:
            raise ConfigError("driver is required for code mode")
        if config.reviewers is None:
            raise ConfigError("reviewers are required for code mode")
    elif config.council is None:
        raise ConfigError("council is required for council mode")


def decode_scalar_utf8(raw: bytes, label: str) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ConfigError(f"{label} must be UTF-8 without a byte-order mark")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ConfigError(f"{label} must not be UTF-16")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{label} must decode as strict UTF-8") from exc
    _reject_surrogates(text, label)
    return text


def validate_model_bounds(value: Any, *, max_chars: int, max_items: int) -> None:
    """Apply the configured generic bounds before payload artifact construction."""

    stack: list[tuple[str, Any]] = [("$", value)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, str):
            _reject_surrogates(current, path)
            if len(current) > max_chars:
                raise ConfigError(f"{path} exceeds limits.max_model_field_chars")
        elif isinstance(current, list):
            if len(current) > max_items:
                raise ConfigError(f"{path} exceeds limits.max_model_list_items")
            stack.extend((f"{path}[{index}]", child) for index, child in enumerate(current))
        elif isinstance(current, dict):
            if len(current) > max_items:
                raise ConfigError(f"{path} exceeds limits.max_model_list_items")
            stack.extend((f"{path}/{key}", child) for key, child in current.items())


def _validate_json_compatible(value: Any, *, path: str, root: bool = False) -> None:
    if root and not isinstance(value, dict):
        raise ConfigError("configuration root must be a mapping")
    if value is None or type(value) in {str, int, bool}:  # bool is intentionally exact
        if isinstance(value, str):
            _reject_surrogates(value, path)
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ConfigError(f"non-finite number at {path}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_compatible(child, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ConfigError(f"mapping key at {path} must be a string")
            _reject_surrogates(key, f"{path} key")
            _validate_json_compatible(child, path=f"{path}/{key}")
        return
    raise ConfigError(f"non-JSON YAML value at {path}: {type(value).__name__}")


def _reject_surrogates(value: str, label: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ConfigError(f"{label} contains a Unicode surrogate code point")


def _json_path(path: tuple[str | int, ...]) -> str:
    return "/" + "/".join(str(component).replace("~", "~0").replace("/", "~1") for component in path)


def _bounded_validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False)
    first = errors[0] if errors else {"loc": (), "msg": "configuration validation failed"}
    location = ".".join(str(part) for part in first.get("loc", ())) or "configuration"
    return f"{location}: {first.get('msg', 'configuration validation failed')}"
