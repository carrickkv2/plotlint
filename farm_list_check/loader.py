"""Read a GeoJSON FeatureCollection while retaining its original bytes."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any
from dataclasses import dataclass


@dataclass
class LoadedGeoJSON:
    original_bytes: bytes
    file_sha256: str
    features: list[dict[str, Any]]
    coordinate_tokens: list[list[str]]


def _coordinate_tokens(value: Any) -> list[str]:
    if isinstance(value, _NumberToken):
        return [str(value)]
    if isinstance(value, list):
        return [token for child in value for token in _coordinate_tokens(child)]
    return []


class _NumberToken(str):
    """Temporary JSON number representation, retaining its exact source text."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"number is outside the supported range: {value}")
    return number


def load_geojson_bytes(raw: bytes) -> LoadedGeoJSON:
    try:
        token_doc = json.loads(raw, parse_int=_NumberToken, parse_float=_NumberToken, parse_constant=_reject_constant)
        document = json.loads(raw, parse_float=_finite_float, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid GeoJSON JSON: {exc}") from exc

    if not isinstance(token_doc, dict) or token_doc.get("type") != "FeatureCollection":
        raise ValueError("Expected a GeoJSON FeatureCollection")
    raw_features = token_doc.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("FeatureCollection 'features' must be an array")
    if any(not isinstance(feature, dict) or feature.get("type") != "Feature" for feature in raw_features):
        raise ValueError("Every FeatureCollection item must be a GeoJSON Feature")

    features = document["features"]
    tokens = []
    for feature in raw_features:
        geometry = feature.get("geometry")
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        tokens.append(_coordinate_tokens(coordinates))
    return LoadedGeoJSON(
        original_bytes=raw,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        features=features,
        coordinate_tokens=tokens,
    )


def load_geojson(path: str | Path) -> LoadedGeoJSON:
    return load_geojson_bytes(Path(path).read_bytes())
