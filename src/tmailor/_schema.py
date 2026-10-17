"""Strict readers for 2xx response bodies (Review 17d SDK-06).

Every reader checks that a field is present and has exactly the JSON type of the OpenAPI schema. ``null`` is
accepted only where the schema allows it. Nothing is coerced (no ``bool("false")``, no ``int("12")``).

A violation raises :class:`SchemaError`. Its message names the field and the expected type, never the value
(response bodies carry address tokens); the client turns it into ``InvalidResponseError`` without the body.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

__all__ = ["SchemaError"]

_MISSING = object()


class SchemaError(ValueError):
    """Internal: a response body does not match the schema. The message never contains field values."""


def obj(v: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise SchemaError(f"{path} must be an object")
    return v


def _get(d: Mapping[str, Any], key: str, path: str) -> Any:
    v = d.get(key, _MISSING)
    if v is _MISSING:
        raise SchemaError(f"{path}.{key} is missing")
    return v


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def req_obj(d: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    return obj(_get(d, key, path), f"{path}.{key}")


def req_obj_or_none(d: Mapping[str, Any], key: str, path: str) -> Optional[Mapping[str, Any]]:
    v = _get(d, key, path)
    return None if v is None else obj(v, f"{path}.{key}")


def req_str(d: Mapping[str, Any], key: str, path: str) -> str:
    v = _get(d, key, path)
    if not isinstance(v, str):
        raise SchemaError(f"{path}.{key} must be a string")
    return v


def req_str_or_none(d: Mapping[str, Any], key: str, path: str) -> Optional[str]:
    v = _get(d, key, path)
    if v is not None and not isinstance(v, str):
        raise SchemaError(f"{path}.{key} must be a string or null")
    return v


def req_int(d: Mapping[str, Any], key: str, path: str) -> int:
    v = _get(d, key, path)
    if not _is_int(v):
        raise SchemaError(f"{path}.{key} must be an integer")
    return int(v)


def opt_int(d: Mapping[str, Any], key: str, path: str) -> Optional[int]:
    """Field that may be absent (``None``); when present it must be an integer (not null)."""
    if key not in d:
        return None
    return req_int(d, key, path)


def req_bool(d: Mapping[str, Any], key: str, path: str) -> bool:
    v = _get(d, key, path)
    if not isinstance(v, bool):
        raise SchemaError(f"{path}.{key} must be a boolean")
    return v


def req_bool_or_none(d: Mapping[str, Any], key: str, path: str) -> Optional[bool]:
    v = _get(d, key, path)
    if v is not None and not isinstance(v, bool):
        raise SchemaError(f"{path}.{key} must be a boolean or null")
    return v


def req_list(d: Mapping[str, Any], key: str, path: str) -> List[Any]:
    v = _get(d, key, path)
    if not isinstance(v, list):
        raise SchemaError(f"{path}.{key} must be an array")
    return v


def req_str_list(d: Mapping[str, Any], key: str, path: str) -> List[str]:
    out = req_list(d, key, path)
    for i, x in enumerate(out):
        if not isinstance(x, str):
            raise SchemaError(f"{path}.{key}[{i}] must be a string")
    return list(out)


def cursor(d: Mapping[str, Any], path: str) -> Optional[str]:
    """``next_cursor``: string or null (an empty string is treated as the end)."""
    v = req_str_or_none(d, "next_cursor", path)
    return v or None
