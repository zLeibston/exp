"""Strict bare-JSON protocol shared by task and tool responses."""
import json


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON field: {key}")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_json(text):
    value = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected one JSON object")
    return value
