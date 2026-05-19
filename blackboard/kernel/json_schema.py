from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


def _schema_kind(schema: Dict[str, Any]) -> str:
    explicit = str(schema.get("type") or "").strip().lower()
    if explicit:
        return explicit
    if any(key in schema for key in ("properties", "required", "additionalProperties")):
        return "object"
    if any(key in schema for key in ("items", "minItems", "maxItems")):
        return "array"
    return ""


def normalize_schema_node(schema: Optional[Dict[str, Any]], *, close_objects: bool = False) -> Dict[str, Any]:
    node = dict(schema or {})
    kind = _schema_kind(node)
    any_of = node.get("anyOf") if isinstance(node.get("anyOf"), list) else None
    if any_of is not None:
        node["anyOf"] = [normalize_schema_node(item if isinstance(item, dict) else {}, close_objects=close_objects) for item in any_of]
    one_of = node.get("oneOf") if isinstance(node.get("oneOf"), list) else None
    if one_of is not None:
        node["oneOf"] = [normalize_schema_node(item if isinstance(item, dict) else {}, close_objects=close_objects) for item in one_of]
    if kind == "object":
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        node["type"] = "object"
        node["properties"] = {
            str(key): normalize_schema_node(value if isinstance(value, dict) else {}, close_objects=close_objects)
            for key, value in properties.items()
        }
        required = [str(item) for item in (node.get("required") or []) if str(item)]
        has_combiners = bool(node.get("anyOf") or node.get("oneOf"))
        should_close = bool(properties or required or has_combiners)
        if close_objects and should_close and "additionalProperties" not in node:
            node["additionalProperties"] = False
        return node
    if kind == "array" and isinstance(node.get("items"), dict):
        node["type"] = "array"
        node["items"] = normalize_schema_node(node.get("items") or {}, close_objects=close_objects)
    return node


def _validate_schema_core(value: Any, schema: Dict[str, Any], *, path: str) -> Tuple[Any, str]:
    kind = _schema_kind(schema)
    if kind == "object":
        if not isinstance(value, dict):
            return None, f"{path} must be an object"
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = [str(item) for item in (schema.get("required") or []) if str(item)]
        missing = [key for key in required if key not in value or value.get(key) is None]
        if missing:
            return None, f"{path} is missing required field(s): {', '.join(missing)}"
        additional_raw = schema.get("additionalProperties")
        additional_allowed = True if additional_raw is None else bool(additional_raw)
        unexpected = sorted(str(key) for key in value.keys() if key not in properties)
        if unexpected and not additional_allowed:
            return None, f"{path} has unexpected field(s): {', '.join(unexpected)}"
        normalized: Dict[str, Any] = {}
        for key, item in value.items():
            if key in properties:
                child, error = validate_schema_value(item, properties[key], path=f"{path}.{key}")
                if error:
                    return None, error
                normalized[key] = child
            elif additional_allowed:
                normalized[key] = item
        return normalized, ""
    if kind == "array":
        if not isinstance(value, list):
            return None, f"{path} must be an array"
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return None, f"{path} must contain at least {min_items} item(s)"
        if isinstance(max_items, int) and len(value) > max_items:
            return None, f"{path} must contain at most {max_items} item(s)"
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else None
        if not item_schema:
            return value, ""
        normalized_items: List[Any] = []
        for index, item in enumerate(value):
            child, error = validate_schema_value(item, item_schema, path=f"{path}[{index}]")
            if error:
                return None, error
            normalized_items.append(child)
        return normalized_items, ""
    if kind == "integer":
        if isinstance(value, bool):
            return None, f"{path} must be an integer"
        if isinstance(value, int):
            coerced = value
        elif isinstance(value, str):
            try:
                coerced = int(value.strip())
            except Exception:
                return None, f"{path} must be an integer"
        else:
            return None, f"{path} must be an integer"
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and coerced < minimum:
            return None, f"{path} must be >= {minimum}"
        if isinstance(maximum, (int, float)) and coerced > maximum:
            return None, f"{path} must be <= {maximum}"
        return coerced, ""
    if kind == "number":
        if isinstance(value, bool):
            return None, f"{path} must be a number"
        if isinstance(value, (int, float)):
            coerced = float(value)
        elif isinstance(value, str):
            try:
                coerced = float(value.strip())
            except Exception:
                return None, f"{path} must be a number"
        else:
            return None, f"{path} must be a number"
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and coerced < minimum:
            return None, f"{path} must be >= {minimum}"
        if isinstance(maximum, (int, float)) and coerced > maximum:
            return None, f"{path} must be <= {maximum}"
        return coerced, ""
    if kind == "boolean":
        if isinstance(value, bool):
            coerced = value
        elif isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                coerced = True
            elif lowered in {"false", "0", "no", "off"}:
                coerced = False
            else:
                return None, f"{path} must be a boolean"
        else:
            return None, f"{path} must be a boolean"
        return coerced, ""
    if kind == "string":
        if isinstance(value, (dict, list)):
            return None, f"{path} must be a string"
        coerced = value if isinstance(value, str) else str(value)
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(coerced) < min_length:
            return None, f"{path} must be at least {min_length} character(s)"
        if isinstance(max_length, int) and len(coerced) > max_length:
            return None, f"{path} must be at most {max_length} character(s)"
        enum_values = schema.get("enum") if isinstance(schema.get("enum"), list) else None
        if enum_values is not None and coerced not in enum_values:
            return None, f"{path} must be one of: {', '.join(str(item) for item in enum_values)}"
        return coerced, ""
    enum_values = schema.get("enum") if isinstance(schema.get("enum"), list) else None
    if enum_values is not None and value not in enum_values:
        return None, f"{path} must be one of: {', '.join(str(item) for item in enum_values)}"
    return value, ""


def validate_schema_value(value: Any, schema: Dict[str, Any], *, path: str) -> Tuple[Any, str]:
    base_schema = {key: item for key, item in schema.items() if key not in {"anyOf", "oneOf"}}
    current, error = _validate_schema_core(value, base_schema, path=path)
    if error:
        return None, error
    any_of = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else None
    if any_of:
        errors: List[str] = []
        for option in any_of:
            child, error = validate_schema_value(current, option if isinstance(option, dict) else {}, path=path)
            if not error:
                return child, ""
            errors.append(error)
        return None, errors[0] if errors else f"{path} did not match any allowed schema"
    one_of = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else None
    if one_of:
        matches: List[Any] = []
        errors: List[str] = []
        for option in one_of:
            child, error = validate_schema_value(current, option if isinstance(option, dict) else {}, path=path)
            if not error:
                matches.append(child)
            else:
                errors.append(error)
        if len(matches) == 1:
            return matches[0], ""
        if len(matches) > 1:
            return None, f"{path} matched more than one allowed schema"
        return None, errors[0] if errors else f"{path} did not match any allowed schema"
    return current, ""


def validate_payload(payload: Any, schema: Dict[str, Any], *, path: str = "payload", close_objects: bool = False) -> Tuple[Any, str]:
    normalized_schema = normalize_schema_node(schema, close_objects=close_objects)
    return validate_schema_value(payload, normalized_schema, path=path)


def parse_json_payload(text: str) -> Tuple[Any, str]:
    value = str(text or "").strip()
    if not value:
        return None, "empty response"
    fence_match = re.match(r"^```[a-zA-Z0-9_-]*\s*\n([\s\S]*?)\n?```\s*$", value)
    if fence_match:
        value = fence_match.group(1).strip()
    starts = [(value.find("{"), "{", "}"), (value.find("["), "[", "]")]
    starts = [item for item in starts if item[0] >= 0]
    if not starts:
        return None, "no JSON payload found"
    start, opener, closer = min(starts, key=lambda item: item[0])
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                raw = value[start:index + 1]
                try:
                    return json.loads(raw), ""
                except Exception as exc:
                    return None, f"invalid JSON: {exc}"
    return None, "unterminated JSON payload"


def build_response_format(schema: Dict[str, Any], name: str) -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": str(name or "structured_response"),
            "strict": True,
            "schema": normalize_schema_node(schema),
        },
    }
