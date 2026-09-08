"""Loads schemas/messages.v1.json and exposes one validator per message type."""

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .config import Settings

_DEFS = ("paper", "chunk", "chunked", "failed")


def schema_path(settings: Settings | None = None) -> Path:
    settings = settings or Settings()
    if settings.schema_path:
        return Path(settings.schema_path)
    # <repo>/python/arxiv_common/src/arxiv_common/schema.py -> <repo>/schemas/messages.v1.json
    return Path(__file__).resolve().parents[4] / "schemas" / "messages.v1.json"


@lru_cache(maxsize=1)
def _root() -> dict[str, Any]:
    with schema_path().open() as f:
        return json.load(f)


@cache
def validator(name: str) -> Draft202012Validator:
    if name not in _DEFS:
        raise KeyError(f"unknown message type {name!r}; expected one of {_DEFS}")
    root = _root()
    schema = {"$schema": root["$schema"], "$ref": f"#/$defs/{name}", "$defs": root["$defs"]}
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


class SchemaError(ValueError):
    pass


def validate(name: str, instance: Any) -> None:
    """Raise SchemaError describing the first violation, or return None."""
    err = next(iter(validator(name).iter_errors(instance)), None)
    if err is not None:
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        raise SchemaError(f"{name} message violates schema at {path}: {err.message}")


def dumps(name: str, model: Any) -> bytes:
    """Serialize a pydantic model and validate the result against the shared schema."""
    instance = model.model_dump(mode="json")
    validate(name, instance)
    return json.dumps(instance, separators=(",", ":")).encode()


def loads(name: str, raw: bytes | str) -> dict[str, Any]:
    instance = json.loads(raw)
    validate(name, instance)
    return instance
