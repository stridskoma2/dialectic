"""Deterministic, fail-closed extraction of bounded model JSON."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .config import ConfigError, validate_model_bounds
from .contracts import MAX_JSON_DEPTH

T = TypeVar("T", bound=BaseModel)
_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*?)```", re.DOTALL)


class OutputError(ValueError):
    pass


def strict_json_loads(text: str) -> Any:
    _validate_json_nesting(text)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OutputError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise OutputError(f"non-finite JSON number is not permitted: {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except OutputError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OutputError("invalid strict JSON") from exc
    _reject_surrogates(value)
    return value


def extract_model_payload(
    assistant_text: str,
    schema: type[T],
    *,
    max_chars: int,
    max_items: int,
    context: dict[str, Any] | None = None,
) -> T:
    payload = extract_json_payload(assistant_text)
    try:
        validate_model_bounds(payload, max_chars=max_chars, max_items=max_items)
        return schema.model_validate(payload, context=context)
    except (ValidationError, ConfigError) as exc:
        raise OutputError(f"model payload failed {schema.__name__} validation") from exc


def extract_json_payload(assistant_text: str) -> Any:
    """Apply the complete-text-or-one-json-fence extraction rule."""

    try:
        return strict_json_loads(assistant_text)
    except OutputError as whole_error:
        fences = list(_JSON_FENCE.finditer(assistant_text))
        if len(fences) != 1 or len(re.findall(r"```json\b", assistant_text)) != 1:
            raise OutputError("assistant output is neither whole JSON nor exactly one json fence") from whole_error
        return strict_json_loads(fences[0].group("body"))


def _validate_json_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise OutputError(f"JSON nesting exceeds {MAX_JSON_DEPTH}")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise OutputError("invalid strict JSON nesting")


def _reject_surrogates(value: Any) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise OutputError("JSON string contains a lone surrogate code point")
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
