"""Small fail-closed validator for the JSON-Schema subset emitted by Pydantic."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


class JsonSchemaError(ValueError):
    pass


_SUPPORTED_KEYWORDS = {
    "$anchor", "$defs", "$id", "$ref", "$schema",
    "additionalProperties", "allOf", "anyOf", "const", "default",
    "deprecated", "description", "enum", "examples", "exclusiveMaximum",
    "exclusiveMinimum", "items", "maxItems", "maxLength", "maxProperties",
    "maximum", "minItems", "minLength", "minProperties", "minimum",
    "multipleOf", "not", "oneOf", "pattern", "patternProperties",
    "prefixItems", "properties", "readOnly", "required", "title", "type",
    "uniqueItems", "writeOnly",
}


def validate_json_schema(value: Any, schema: Mapping[str, Any]) -> None:
    _validate(value, schema, root=schema, path="$")


def _validate(
    value: Any,
    schema: Mapping[str, Any] | bool,
    *,
    root: Mapping[str, Any],
    path: str,
) -> None:
    if schema is True:
        return
    if schema is False:
        raise JsonSchemaError(f"{path} is forbidden by schema")
    if not isinstance(schema, Mapping):
        raise JsonSchemaError(f"{path} has an invalid schema node")
    unsupported = set(schema).difference(_SUPPORTED_KEYWORDS)
    if unsupported:
        raise JsonSchemaError(
            f"{path} uses unsupported schema keyword {sorted(unsupported)[0]}"
        )
    if "$ref" in schema:
        target = _resolve_reference(root, schema["$ref"])
        _validate(value, target, root=root, path=path)
    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list):
        raise JsonSchemaError(f"{path} has invalid allOf branches")
    for branch in all_of:
        _validate(value, branch, root=root, path=path)
    if "anyOf" in schema:
        if not _matches_any(value, schema["anyOf"], root=root, path=path):
            raise JsonSchemaError(f"{path} matches no anyOf branch")
    if "oneOf" in schema:
        matches = sum(
            _matches(value, branch, root=root, path=path)
            for branch in schema["oneOf"]
        )
        if matches != 1:
            raise JsonSchemaError(f"{path} must match exactly one oneOf branch")
    if "not" in schema and _matches(value, schema["not"], root=root, path=path):
        raise JsonSchemaError(f"{path} matches a forbidden schema")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise JsonSchemaError(f"{path} violates const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not any(
            _json_equal(value, choice) for choice in choices
        ):
            raise JsonSchemaError(f"{path} is outside enum")

    expected = schema.get("type")
    if expected is not None:
        choices = [expected] if isinstance(expected, str) else expected
        if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
            raise JsonSchemaError(f"{path} has an invalid type declaration")
        if not any(_has_type(value, item) for item in choices):
            raise JsonSchemaError(f"{path} has the wrong JSON type")

    if isinstance(value, dict):
        _validate_object(value, schema, root=root, path=path)
    elif isinstance(value, list):
        _validate_array(value, schema, root=root, path=path)
    elif isinstance(value, str):
        _validate_string(value, schema, path=path)
    elif type(value) in {int, float}:
        _validate_number(value, schema, path=path)


def _validate_object(
    value: dict[str, Any], schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str
) -> None:
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise JsonSchemaError(f"{path} has invalid required fields")
    missing = [name for name in required if name not in value]
    if missing:
        raise JsonSchemaError(f"{path} lacks required field {missing[0]}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise JsonSchemaError(f"{path} has invalid properties")
    patterns = schema.get("patternProperties", {})
    if not isinstance(patterns, Mapping):
        raise JsonSchemaError(f"{path} has invalid patternProperties")
    additional = schema.get("additionalProperties", True)
    for key, child in value.items():
        if not isinstance(key, str):
            raise JsonSchemaError(f"{path} has a non-string object key")
        matched = False
        if key in properties:
            _validate(child, properties[key], root=root, path=f"{path}/{key}")
            matched = True
        for pattern, branch in patterns.items():
            if not isinstance(pattern, str):
                raise JsonSchemaError(f"{path} has invalid patternProperties")
            try:
                matches = re.search(pattern, key) is not None
            except re.error as exc:
                raise JsonSchemaError(f"{path} has invalid patternProperties") from exc
            if matches:
                _validate(child, branch, root=root, path=f"{path}/{key}")
                matched = True
        if not matched:
            if additional is False:
                raise JsonSchemaError(f"{path} contains unexpected field {key}")
            if isinstance(additional, (dict, bool)):
                _validate(child, additional, root=root, path=f"{path}/{key}")
            else:
                raise JsonSchemaError(f"{path} has invalid additionalProperties")
    _bounded_length(value, schema, "Properties", path)


def _validate_array(
    value: list[Any], schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str
) -> None:
    prefix = schema.get("prefixItems", [])
    if not isinstance(prefix, list):
        raise JsonSchemaError(f"{path} has invalid prefixItems")
    for index, branch in enumerate(prefix):
        if index < len(value):
            _validate(value[index], branch, root=root, path=f"{path}[{index}]")
    items = schema.get("items", True)
    start = len(prefix)
    for index in range(start, len(value)):
        _validate(value[index], items, root=root, path=f"{path}[{index}]")
    _bounded_length(value, schema, "Items", path)
    if schema.get("uniqueItems") is True:
        for index, item in enumerate(value):
            if any(_json_equal(item, prior) for prior in value[:index]):
                raise JsonSchemaError(f"{path} has duplicate array items")
    elif "uniqueItems" in schema and schema["uniqueItems"] is not False:
        raise JsonSchemaError(f"{path} has invalid uniqueItems")


def _validate_string(value: str, schema: Mapping[str, Any], *, path: str) -> None:
    _bounded_length(value, schema, "Length", path)
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise JsonSchemaError(f"{path} has invalid pattern")
        try:
            matches = re.search(pattern, value) is not None
        except re.error as exc:
            raise JsonSchemaError(f"{path} has invalid pattern") from exc
        if not matches:
            raise JsonSchemaError(f"{path} violates pattern")


def _validate_number(
    value: int | float, schema: Mapping[str, Any], *, path: str
) -> None:
    if not math.isfinite(value):
        raise JsonSchemaError(f"{path} is non-finite")
    comparisons = (
        ("minimum", lambda left, right: left >= right),
        ("maximum", lambda left, right: left <= right),
        ("exclusiveMinimum", lambda left, right: left > right),
        ("exclusiveMaximum", lambda left, right: left < right),
    )
    for name, predicate in comparisons:
        if name in schema:
            bound = schema[name]
            if type(bound) not in {int, float} or not math.isfinite(bound):
                raise JsonSchemaError(f"{path} has invalid {name}")
            if not predicate(value, bound):
                raise JsonSchemaError(f"{path} violates {name}")
    if "multipleOf" in schema:
        divisor = schema["multipleOf"]
        if type(divisor) not in {int, float} or divisor <= 0:
            raise JsonSchemaError(f"{path} has invalid multipleOf")
        quotient = value / divisor
        if not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-12):
            raise JsonSchemaError(f"{path} violates multipleOf")


def _bounded_length(value: Any, schema: Mapping[str, Any], suffix: str, path: str) -> None:
    minimum = schema.get(f"min{suffix}")
    maximum = schema.get(f"max{suffix}")
    if minimum is not None and (type(minimum) is not int or minimum < 0):
        raise JsonSchemaError(f"{path} has invalid min{suffix}")
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise JsonSchemaError(f"{path} has invalid max{suffix}")
    if minimum is not None and len(value) < minimum:
        raise JsonSchemaError(f"{path} violates min{suffix}")
    if maximum is not None and len(value) > maximum:
        raise JsonSchemaError(f"{path} violates max{suffix}")


def _has_type(value: Any, name: str) -> bool:
    return {
        "null": value is None,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in {int, float},
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(name, False)


def _matches_any(
    value: Any, branches: Any, *, root: Mapping[str, Any], path: str
) -> bool:
    if not isinstance(branches, list):
        raise JsonSchemaError(f"{path} has invalid schema branches")
    return any(_matches(value, branch, root=root, path=path) for branch in branches)


def _matches(
    value: Any, schema: Any, *, root: Mapping[str, Any], path: str
) -> bool:
    try:
        _validate(value, schema, root=root, path=path)
    except JsonSchemaError:
        return False
    return True


def _resolve_reference(root: Mapping[str, Any], reference: Any) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise JsonSchemaError("only local JSON-Schema references are supported")
    current: Any = root
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            raise JsonSchemaError("JSON-Schema reference is unresolved")
        current = current[token]
    return current


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right
