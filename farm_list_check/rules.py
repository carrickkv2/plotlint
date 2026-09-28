"""Per-feature checks; PostGIS handles validity and cross-feature rules."""

from __future__ import annotations

import math
import re
from typing import Any, Callable

RuleResult = tuple[str, str] | None


def _properties(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties")
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ValueError("Feature 'properties' must be an object or null")
    return properties


def _finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positions(feature: dict[str, Any]) -> tuple[str | None, list[list[float | int]]]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None, []
    geometry_type = geometry.get("type")
    if geometry_type not in ("Point", "Polygon"):
        return geometry_type if isinstance(geometry_type, str) else None, []

    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        if not isinstance(coordinates, list):
            raise ValueError("Point coordinates must be an array position")
        positions = [coordinates]
    else:
        if not isinstance(coordinates, list):
            raise ValueError("Polygon coordinates must be an array of rings")
        if not coordinates:
            raise ValueError("Polygon coordinates must contain at least one ring")
        if any(not isinstance(ring, list) for ring in coordinates):
            raise ValueError("Each Polygon ring must be an array of positions")
        positions = [position for ring in coordinates for position in ring]

    for position in positions:
        if not isinstance(position, list) or len(position) < 2:
            raise ValueError("Every coordinate position must contain at least longitude and latitude")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _finite(value)
            for value in position
        ):
            raise ValueError("Coordinate positions must contain only finite numbers")
        longitude, latitude = position[:2]
        in_wgs84_range = abs(longitude) <= 180 and abs(latitude) <= 90
        could_be_swapped = abs(latitude) <= 180 and abs(longitude) <= 90
        if not in_wgs84_range and not could_be_swapped:
            raise ValueError("Coordinate pair is outside WGS-84 ranges and cannot be explained by swapping longitude and latitude")
    return geometry_type, positions


def _rings(feature: dict[str, Any]) -> list[list[list[float | int]]]:
    return feature["geometry"]["coordinates"]


def _declared_area(feature: dict[str, Any]) -> float | int | None:
    value = _properties(feature).get("area_ha")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value) or value < 0:
        raise ValueError("Feature 'area_ha' must be a finite, non-negative number or null")
    return value


def _decimal_places(token: str) -> int:
    # Exponents shift the literal's represented decimal resolution; trailing
    # zeros still count because this check is about supplied text, not accuracy.
    match = re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?", token)
    if match is None:
        raise ValueError(f"Invalid raw JSON number token: {token!r}")
    fraction_digits = len(match.group(1) or "")
    try:
        exponent = int(match.group(2) or "0")
    except ValueError as exc:
        raise ValueError("Raw coordinate exponent is too large to evaluate") from exc
    return max(0, fraction_digits - exponent)


def check_missing_farm_id(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    farm_id = _properties(feature).get("farm_id")
    if not isinstance(farm_id, str) or not farm_id.strip():
        return "MISSING_FARM_ID", "farm_id must be a non-empty string"
    return None


def check_invalid_geometry_type(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Point", "Polygon"):
        return "INVALID_GEOMETRY_TYPE", "geometry must be a GeoJSON Point or Polygon"
    return None


def check_low_precision(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    if any(_decimal_places(token) < 6 for token in coordinate_tokens):
        return "LOW_PRECISION", "every coordinate number must represent at least 6 decimal places"
    return None


def check_likely_swapped_coords(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Point", "Polygon"):
        return None
    _, positions = _positions(feature)
    if any(
        abs(position[1]) > 90
        and abs(position[1]) <= 180
        and abs(position[0]) <= 90
        for position in positions
    ):
        return "LIKELY_SWAPPED_COORDS", "latitude is outside ±90; swapping the coordinates would be in range"
    return None


def check_point_over_4ha(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    area = _declared_area(feature)
    if area is not None and area > 4:
        return "POINT_OVER_4HA", "plots larger than 4 ha must use a Polygon geometry"
    return None


def check_polygon_not_closed(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None
    if any(not ring or ring[0] != ring[-1] for ring in _rings(feature)):
        return "POLYGON_NOT_CLOSED", "every Polygon ring must end at its first position"
    return None


def check_polygon_too_few_vertices(feature: dict[str, Any], coordinate_tokens: list[str]) -> RuleResult:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None
    if any(len({tuple(position[:2]) for position in ring}) < 4 for ring in _rings(feature)):
        return "POLYGON_TOO_FEW_VERTICES", "every Polygon ring must have at least 4 distinct vertices"
    return None


RULES: list[Callable[[dict[str, Any], list[str]], RuleResult]] = [
    check_missing_farm_id,
    check_invalid_geometry_type,
    check_low_precision,
    check_likely_swapped_coords,
    check_point_over_4ha,
    check_polygon_not_closed,
    check_polygon_too_few_vertices,
]


def apply_feature_rules(feature: dict[str, Any], coordinate_tokens: list[str]) -> list[tuple[str, str]]:
    """Run the ordered Python rules and return each reason code with its message."""
    if not isinstance(feature, dict):
        raise ValueError("Feature must be an object")
    if feature.get("type") != "Feature":
        raise ValueError("Feature 'type' must be 'Feature'")
    if not isinstance(coordinate_tokens, list) or any(not isinstance(token, str) for token in coordinate_tokens):
        raise ValueError("coordinate_tokens must be a list of raw JSON number strings")

    _properties(feature)
    _declared_area(feature)
    geometry_type, positions = _positions(feature)
    if geometry_type in ("Point", "Polygon") and sum(map(len, positions)) != len(coordinate_tokens):
        raise ValueError("Raw coordinate tokens do not match the feature's coordinate positions")

    results = []
    for rule in RULES:
        result = rule(feature, coordinate_tokens)
        if result is not None:
            results.append(result)
    return results
