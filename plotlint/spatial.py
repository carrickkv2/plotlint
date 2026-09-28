"""PostGIS area, validity, duplicate, and overlap checks."""

from __future__ import annotations

import json
from typing import Any, Sequence

from psycopg import Connection

Reason = tuple[str, str]


def apply_spatial_rules(
    conn: Connection,
    features: Sequence[dict[str, Any]],
    feature_results: Sequence[Sequence[Reason]],
) -> dict[int, list[Reason]]:
    """Run PostGIS checks on features already checked by `apply_feature_rules`.

    Uses the caller's transaction when one exists and never commits it. Structurally
    unusable polygons are skipped based on their Python reason codes.
    """
    if len(features) != len(feature_results):
        raise ValueError("features and feature_results must have the same length")

    spatial_results: dict[int, list[Reason]] = {}
    unusable_codes = {
        "INVALID_GEOMETRY_TYPE",
        "LIKELY_SWAPPED_COORDS",
        "POLYGON_NOT_CLOSED",
        "POLYGON_TOO_FEW_VERTICES",
    }

    with conn.transaction(), conn.cursor() as cursor:
        # A temporary table isolates each list and drops automatically at transaction end.
        cursor.execute("""
            CREATE TEMP TABLE farm_list_spatial_features (
                feature_index integer PRIMARY KEY,
                geometry_type text NOT NULL,
                declared_area_ha double precision,
                geom geometry(Geometry, 4326) NOT NULL,
                is_valid boolean NOT NULL DEFAULT true
            ) ON COMMIT DROP
        """)

        for index, (feature, results) in enumerate(zip(features, feature_results)):
            if any(code in unusable_codes for code, _ in results):
                continue
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict) or geometry.get("type") not in ("Point", "Polygon"):
                continue
            geometry_type = geometry["type"]
            properties = feature.get("properties") or {}
            declared_area = properties.get("area_ha")

            # ST_GeomFromGeoJSON parses the WGS-84 geometry; ST_Force2D drops optional altitude because rules compare XY footprints.
            cursor.execute(
                """
                INSERT INTO farm_list_spatial_features
                    (feature_index, geometry_type, declared_area_ha, geom)
                VALUES (%s, %s, %s, ST_Force2D(ST_GeomFromGeoJSON(%s)))
                """,
                (index, geometry_type, declared_area, json.dumps(geometry, allow_nan=False)),
            )

        # ST_IsValid checks polygon topology; ST_IsValidReason gives support a readable cause.
        cursor.execute("""
            UPDATE farm_list_spatial_features
            SET is_valid = ST_IsValid(geom)
            WHERE geometry_type = 'Polygon'
        """)
        cursor.execute("""
            SELECT feature_index, ST_IsValidReason(geom)
            FROM farm_list_spatial_features
            WHERE geometry_type = 'Polygon' AND NOT is_valid
        """)
        for index, reason in cursor.fetchall():
            spatial_results.setdefault(index, []).append(
                ("POLYGON_SELF_INTERSECTS", f"Polygon is invalid: {reason}")
            )

        # Remove invalid polygons before geography math rather than silently repairing them.
        cursor.execute("""
            DELETE FROM farm_list_spatial_features
            WHERE geometry_type = 'Polygon' AND NOT is_valid
        """)

        # Geography area is in square metres; divide by 10,000 to compare hectares.
        cursor.execute("""
            SELECT feature_index, declared_area_ha,
                   ST_Area(geom::geography) / 10000.0 AS computed_area_ha
            FROM farm_list_spatial_features
            WHERE geometry_type = 'Polygon' AND declared_area_ha IS NOT NULL
        """)
        for index, declared_area, computed_area in cursor.fetchall():
            # A zero declared area matches only zero computed area, without dividing by zero.
            mismatch = computed_area > 0 if declared_area == 0 else abs(computed_area - declared_area) / declared_area > 0.20
            if mismatch:
                spatial_results.setdefault(index, []).append(
                    (
                        "AREA_MISMATCH",
                        f"computed area is {computed_area:.3f} ha; declared area is {declared_area:.3f} ha",
                    )
                )

        # ST_Equals compares topological shape, ignoring vertex order and ring winding.
        # Note: pairwise comparisons are O(n²); batch or add a spatial index for large lists.
        cursor.execute("""
            SELECT a.feature_index, b.feature_index
            FROM farm_list_spatial_features AS a
            JOIN farm_list_spatial_features AS b
              ON a.feature_index < b.feature_index
            WHERE ST_Equals(a.geom, b.geom)
        """)
        for first, second in cursor.fetchall():
            for index, other in ((first, second), (second, first)):
                spatial_results.setdefault(index, []).append(
                    ("DUPLICATE_PLOT", f"geometry is equal to feature index {other}")
                )

        # ST_Intersects removes disjoint pairs; geography intersection and area measure their earth-surface overlap.
        cursor.execute("""
            WITH polygon_pairs AS (
                SELECT a.feature_index AS first_index,
                       b.feature_index AS second_index,
                       a.geom AS first_geom,
                       b.geom AS second_geom
                FROM farm_list_spatial_features AS a
                JOIN farm_list_spatial_features AS b
                  ON a.feature_index < b.feature_index
                WHERE a.geometry_type = 'Polygon'
                  AND b.geometry_type = 'Polygon'
                  AND ST_Intersects(a.geom::geography, b.geom::geography)
            ), measured AS (
                SELECT first_index, second_index,
                       ST_Area(first_geom::geography) AS first_area_m2,
                       ST_Area(second_geom::geography) AS second_area_m2,
                       ST_Area(ST_Intersection(first_geom::geography, second_geom::geography)) AS overlap_m2
                FROM polygon_pairs
            )
            SELECT first_index, second_index,
                   100.0 * overlap_m2 / LEAST(first_area_m2, second_area_m2) AS overlap_percent
            FROM measured
            WHERE LEAST(first_area_m2, second_area_m2) > 0
              AND overlap_m2 / NULLIF(LEAST(first_area_m2, second_area_m2), 0) > 0.01
        """)
        for first, second, overlap_percent in cursor.fetchall():
            for index, other in ((first, second), (second, first)):
                spatial_results.setdefault(index, []).append(
                    (
                        "OVERLAPPING_PLOTS",
                        f"overlaps feature index {other} by {overlap_percent:.2f}% of the smaller polygon",
                    )
                )

        cursor.execute("DROP TABLE pg_temp.farm_list_spatial_features")

    return spatial_results
