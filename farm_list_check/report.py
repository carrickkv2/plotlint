"""Build one report shape shared by the CLI, the API and the map."""

from __future__ import annotations

import csv
import json
from collections import Counter
from io import StringIO
from typing import Any

from farm_list_check.db import connect
from farm_list_check.jobs import get_job


class JobNotDoneError(RuntimeError):
    """The job exists but has no results yet."""


def build_report(job_id: int) -> dict[str, Any]:
    """Return per-feature results and summary for a completed job."""
    job = get_job(job_id)
    if job["status"] != "done":
        raise JobNotDoneError(f"Job {job_id} is not done (status: {job['status']})")

    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """SELECT feature_index, farm_id, valid, codes, messages
               FROM results WHERE job_id = %s ORDER BY feature_index""",
            (job_id,),
        )
        rows = cursor.fetchall()

    results = [
        {
            "feature_index": index,
            "farm_id": farm_id,
            "valid": valid,
            "codes": codes,
            "messages": messages,
        }
        for index, farm_id, valid, codes, messages in rows
    ]
    code_counts = Counter(code for result in results for code in set(result["codes"]))
    valid_count = sum(result["valid"] for result in results)
    return {
        "job_id": job_id,
        "results": results,
        "summary": {
            "total": len(results),
            "valid": valid_count,
            "invalid": len(results) - valid_count,
            "counts_by_code": dict(sorted(code_counts.items())),
        },
    }


def build_result_geojson(job_id: int) -> dict[str, Any]:
    """Return the uploaded features with each one's result added to its properties (used by the map)."""
    report = build_report(job_id)  # also checks the job exists and is done

    with connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT original_geojson FROM jobs WHERE id = %s", (job_id,))
        original = json.loads(bytes(cursor.fetchone()[0]))

    features = original["features"]
    for result in report["results"]:
        feature = features[result["feature_index"]]
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        feature["properties"] = {
            **properties,
            "valid": result["valid"],
            "codes": result["codes"],
            "messages": result["messages"],
        }
    return {"type": "FeatureCollection", "features": features, "summary": report["summary"]}


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2) + "\n"


def render_csv(report: dict[str, Any]) -> str:
    """Use feature and summary records under one stable CSV header."""
    fields = [
        "row_type", "job_id", "feature_index", "farm_id", "valid", "codes",
        "messages", "total", "valid_count", "invalid_count", "counts_by_code",
    ]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for result in report["results"]:
        writer.writerow(
            {
                "row_type": "feature",
                "job_id": report["job_id"],
                "feature_index": result["feature_index"],
                "farm_id": result["farm_id"] or "",
                "valid": str(result["valid"]).lower(),
                "codes": json.dumps(result["codes"], separators=(",", ":")),
                "messages": json.dumps(result["messages"], separators=(",", ":")),
            }
        )
    summary = report["summary"]
    writer.writerow(
        {
            "row_type": "summary",
            "job_id": report["job_id"],
            "total": summary["total"],
            "valid_count": summary["valid"],
            "invalid_count": summary["invalid"],
            "counts_by_code": json.dumps(summary["counts_by_code"], separators=(",", ":")),
        }
    )
    return output.getvalue()
