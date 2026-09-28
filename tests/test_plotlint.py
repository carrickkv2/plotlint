"""The behaviour that matters, end to end. Needs the PostGIS database from docker-compose."""

import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from plotlint import jobs
from plotlint.api import MAX_UPLOAD_BYTES, app
from plotlint.cli import app as cli_app
from plotlint.db import connect
from plotlint.loader import load_geojson_bytes
from plotlint.report import build_report
from plotlint.rules import apply_feature_rules
from plotlint.spatial import apply_spatial_rules

# Reason codes the demo sample should produce, by feature index (see samples/README.md).
DEMO_EXPECTED_CODES = [
    ["DUPLICATE_PLOT"],
    ["MISSING_FARM_ID"],
    ["INVALID_GEOMETRY_TYPE"],
    ["LOW_PRECISION"],
    ["LIKELY_SWAPPED_COORDS"],
    ["POINT_OVER_4HA"],
    ["POLYGON_NOT_CLOSED"],
    ["POLYGON_TOO_FEW_VERTICES"],
    ["POLYGON_SELF_INTERSECTS"],
    ["AREA_MISMATCH"],
    ["DUPLICATE_PLOT"],
    ["OVERLAPPING_PLOTS"],
    ["OVERLAPPING_PLOTS"],
    [],
    [],
    [],  # KMB-016 to KMB-023: valid farms
    [],
    [],
    [],
    [],
    [],
    [],
    [],
]


def unique_copy(path: str) -> bytes:
    """Return the file's bytes with a unique extra field, so each test gets its own job
    (jobs are deduplicated by file hash).

    The field is appended to the raw text instead of re-serialising with json.dumps, which
    would rewrite 36.821900 as 36.8219 and trip the precision rule.
    """
    raw = Path(path).read_bytes().rstrip()
    return raw[:-1] + f',"test_run":"{uuid4().hex}"}}'.encode()


@pytest.fixture
def job_ids():
    """Collect the jobs a test creates and delete them afterwards."""
    ids: list[int] = []
    yield ids
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))


def test_demo_file_produces_the_expected_reason_codes(job_ids):
    job = jobs.submit_job(unique_copy("samples/demo.geojson"))
    job_ids.append(job["id"])

    assert jobs.run_job(job["id"])["status"] == "done"

    report = build_report(job["id"])
    assert [result["codes"] for result in report["results"]] == DEMO_EXPECTED_CODES
    assert report["summary"]["valid"] == 10
    assert report["summary"]["invalid"] == 13


def test_valid_file_has_no_errors(job_ids):
    job = jobs.submit_job(unique_copy("samples/valid.geojson"))
    job_ids.append(job["id"])
    jobs.run_job(job["id"])

    assert build_report(job["id"])["summary"]["invalid"] == 0


def test_precision_is_read_from_the_raw_text_not_the_parsed_float():
    feature = {"type": "Feature", "properties": {"farm_id": "F-1"},
               "geometry": {"type": "Point", "coordinates": [36.821900, -1.102300]}}

    # As floats, both spellings are identical. Only the file's text shows the precision.
    assert apply_feature_rules(feature, ["36.821900", "-1.102300"]) == []
    assert [code for code, _ in apply_feature_rules(feature, ["36.8219", "-1.1023"])] == ["LOW_PRECISION"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "Invalid GeoJSON JSON"),
        (b'{"type":"Feature","features":[]}', "Expected a GeoJSON FeatureCollection"),
        (b'{"type":"FeatureCollection","features":{}}', "features' must be an array"),
        (b'{"type":"FeatureCollection","features":[],"extra":NaN}', "Invalid GeoJSON JSON"),
        (b'{"type":"FeatureCollection","features":[],"extra":1e999}', "Invalid GeoJSON JSON"),
    ],
)
def test_malformed_files_are_rejected_with_a_clear_message(raw, message):
    with pytest.raises(ValueError, match=message):
        load_geojson_bytes(raw)


def square(farm_id: str, west: float, south: float = -1.202, size: float = 0.002, z: float | None = None) -> dict:
    """A square polygon feature; pass z to give every corner a third (height) value."""
    corners = [[west, south + size], [west + size, south + size], [west + size, south], [west, south], [west, south + size]]
    if z is not None:
        corners = [[x, y, z] for x, y in corners]
    return {"type": "Feature", "properties": {"farm_id": farm_id},
            "geometry": {"type": "Polygon", "coordinates": [corners]}}


def test_overlapping_polygons_are_flagged_but_touching_ones_are_not():
    features = [
        square("A", 36.900),  # overlaps B by half
        square("B", 36.901),
        square("C", 36.910),  # shares only an edge with D
        square("D", 36.912),
    ]
    with connect() as conn:
        spatial = apply_spatial_rules(conn, features, [[] for _ in features])

    assert set(spatial) == {0, 1}
    assert spatial[0][0][0] == "OVERLAPPING_PLOTS"
    assert "50.00%" in spatial[0][0][1]


def test_spatial_edge_cases_containment_and_3d_coordinates():
    features = [
        square("LARGE", 36.920, south=-1.214, size=0.004),
        square("SMALL-INSIDE", 36.921, south=-1.213, size=0.001),  # entirely inside LARGE
        square("WITH-HEIGHT", 36.930, z=10.0),                      # [lon, lat, height] corners
    ]
    with connect() as conn:
        spatial = apply_spatial_rules(conn, features, [[] for _ in features])

    # A plot inside another overlaps 100% of the smaller one.
    assert [code for code, _ in spatial[1]] == ["OVERLAPPING_PLOTS"]
    assert "100.00%" in spatial[1][0][1]
    # The third (height) value is ignored: the plot is checked on longitude/latitude only.
    assert 2 not in spatial


def test_a_polygon_hole_that_is_not_closed_is_flagged():
    outer = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
    open_hole = [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]]  # last point doesn't repeat the first
    feature = {"type": "Feature", "properties": {"farm_id": "HOLE"},
               "geometry": {"type": "Polygon", "coordinates": [outer, open_hole]}}
    tokens = [f"{value:.6f}" for ring in (outer, open_hole) for point in ring for value in point]

    assert [code for code, _ in apply_feature_rules(feature, tokens)] == ["POLYGON_NOT_CLOSED"]


def test_submitting_the_same_file_twice_returns_the_same_job(job_ids):
    raw = unique_copy("samples/valid.geojson")
    first = jobs.submit_job(raw)
    job_ids.append(first["id"])

    assert jobs.submit_job(raw)["id"] == first["id"]


def test_a_failing_job_is_retried_at_most_three_times(job_ids):
    broken_point = {"type": "FeatureCollection", "test_run": uuid4().hex, "features": [
        {"type": "Feature", "properties": {"farm_id": "BAD"}, "geometry": {"type": "Point", "coordinates": [1]}},
    ]}
    job = jobs.submit_job(json.dumps(broken_point).encode())
    job_ids.append(job["id"])

    for attempt in (1, 2, 3):
        with pytest.raises(ValueError):
            jobs.run_job(job["id"])
        assert jobs.get_job(job["id"])["attempts"] == attempt

    # A fourth run is refused: the job stays failed at 3 attempts.
    after = jobs.run_job(job["id"])
    assert (after["status"], after["attempts"]) == ("failed", 3)


def test_two_workers_running_the_same_job_only_claim_it_once(job_ids, monkeypatch):
    job = jobs.submit_job(unique_copy("samples/valid.geojson"))
    job_ids.append(job["id"])

    # Hold the first worker inside the job until the second worker has tried to run it.
    first_worker_started = Event()
    let_first_worker_finish = Event()
    real_rule = jobs.apply_feature_rules

    def paused_rule(feature, tokens):
        first_worker_started.set()
        let_first_worker_finish.wait(timeout=15)
        return real_rule(feature, tokens)

    monkeypatch.setattr(jobs, "apply_feature_rules", paused_rule)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(jobs.run_job, job["id"])
        assert first_worker_started.wait(timeout=15)

        second = jobs.run_job(job["id"])  # returns immediately: the job is locked
        assert (second["status"], second["attempts"]) == ("validating", 1)

        let_first_worker_finish.set()
        assert first.result(timeout=30)["status"] == "done"

    assert jobs.get_job(job["id"])["attempts"] == 1


def test_a_job_whose_worker_crashed_is_marked_failed_and_can_be_retried(job_ids):
    job = jobs.submit_job(unique_copy("samples/valid.geojson"))
    job_ids.append(job["id"])

    # Simulate a worker process dying mid-job. os._exit skips all Python cleanup, so only
    # PostgreSQL releasing the session lock tells us the worker is gone.
    crash = ("import os, sys; from plotlint import jobs; "
             "jobs.apply_feature_rules = lambda *args: os._exit(37); jobs.run_job(int(sys.argv[1]))")
    child = subprocess.run([sys.executable, "-c", crash, str(job["id"])], env=os.environ.copy(), timeout=30)
    assert child.returncode == 37

    recovered = jobs.get_job(job["id"])
    assert recovered["status"] == "failed"
    assert "Worker exited" in recovered["error"]

    retried = jobs.run_job(job["id"])
    assert (retried["status"], retried["attempts"]) == ("done", 2)


def test_cli_submit_run_and_report(job_ids, tmp_path):
    runner = CliRunner()
    source = tmp_path / "demo.geojson"
    source.write_bytes(unique_copy("samples/demo.geojson"))

    submitted = runner.invoke(cli_app, ["submit", str(source)])
    assert submitted.exit_code == 0, submitted.output
    job_id = int(submitted.output)
    job_ids.append(job_id)

    assert runner.invoke(cli_app, ["run", str(job_id)]).exit_code == 0

    as_json = runner.invoke(cli_app, ["report", str(job_id), "--format", "json"])
    assert json.loads(as_json.output)["summary"]["total"] == 23

    as_csv = runner.invoke(cli_app, ["report", str(job_id), "--format", "csv"])
    rows = list(csv.DictReader(StringIO(as_csv.output)))
    assert len([row for row in rows if row["row_type"] == "feature"]) == 23
    assert rows[-1]["row_type"] == "summary"

    # Errors: bad option → exit 2, missing job → exit 1, malformed file → exit 2.
    assert runner.invoke(cli_app, ["report", str(job_id), "--format", "xml"]).exit_code == 2
    assert runner.invoke(cli_app, ["report", "999999999"]).exit_code == 1
    malformed = tmp_path / "bad.geojson"
    malformed.write_text("{}")
    assert runner.invoke(cli_app, ["submit", str(malformed)]).exit_code == 2


def test_api_upload_to_report(job_ids):
    client = TestClient(app)  # runs background tasks before returning, so the job is processed
    raw = unique_copy("samples/demo.geojson")

    uploaded = client.post("/farm-lists", content=raw)
    assert uploaded.status_code == 202
    job_id = uploaded.json()["job_id"]
    job_ids.append(job_id)

    assert client.get(f"/jobs/{job_id}").json()["status"] == "done"
    assert client.get(f"/jobs/{job_id}/report").json()["summary"]["total"] == 23
    assert client.get(f"/jobs/{job_id}/report?format=csv").headers["content-type"].startswith("text/csv")
    assert client.get(f"/jobs/{job_id}/geojson").json()["features"][11]["properties"]["codes"] == ["OVERLAPPING_PLOTS"]

    assert client.post("/farm-lists", content=b"{}").status_code == 422
    assert client.post("/farm-lists", content=b" " * (MAX_UPLOAD_BYTES + 1)).status_code == 413
    assert client.get("/jobs/999999999").status_code == 404
