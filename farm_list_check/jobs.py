"""Hash-idempotent job service with crash recovery and at most three attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from farm_list_check.db import connect
from farm_list_check.loader import load_geojson_bytes
from farm_list_check.rules import apply_feature_rules
from farm_list_check.spatial import apply_spatial_rules

MAX_ATTEMPTS = 3


def _job_from_cursor(cursor: Any, job_id: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT id, file_sha256, status, attempts, error,
               created_at, updated_at, started_at, finished_at
        FROM jobs WHERE id = %s
        """,
        (job_id,),
    )
    job = cursor.fetchone()
    if job is None:
        raise LookupError(f"Job {job_id} was not found")
    return job


def _mark_abandoned(cursor: Any, job_id: int) -> None:
    cursor.execute(
        """
        UPDATE jobs
        SET status = 'failed', error = 'Worker exited while job was validating',
            updated_at = now(), finished_at = now()
        WHERE id = %s AND status = 'validating'
        """,
        (job_id,),
    )


def _recover_if_unlocked(conn: Any, job_id: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"Job {job_id} was not found")
        if row[0] != "validating":
            return

        # Session locks vanish on disconnect; job IDs are reserved as this service's advisory-lock keys.
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (job_id,))
        if cursor.fetchone()[0]:
            _mark_abandoned(cursor, job_id)


def submit_job(source: bytes | str | Path) -> dict[str, Any]:
    """Validate and store exact source bytes; a matching SHA-256 reuses its job."""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    loaded = load_geojson_bytes(raw)

    with connect() as conn, conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            INSERT INTO jobs (file_sha256, original_geojson)
            VALUES (%s, %s)
            ON CONFLICT (file_sha256) DO NOTHING
            RETURNING id, file_sha256, status, attempts, error,
                      created_at, updated_at, started_at, finished_at
            """,
            (loaded.file_sha256, raw),
        )
        job = cursor.fetchone()
        if job is not None:
            return job
        cursor.execute("SELECT id FROM jobs WHERE file_sha256 = %s", (loaded.file_sha256,))
        existing = cursor.fetchone()
        if existing is None:
            raise RuntimeError("Job insert conflicted but no matching job exists")
        _recover_if_unlocked(conn, existing["id"])
        return _job_from_cursor(cursor, existing["id"])


def get_job(job_id: int) -> dict[str, Any]:
    with connect() as conn:
        with conn.transaction():
            _recover_if_unlocked(conn, job_id)
            with conn.cursor(row_factory=dict_row) as cursor:
                return _job_from_cursor(cursor, job_id)


def delete_jobs_older_than(days: int) -> None:
    """Delete old jobs (their results go with them via ON DELETE CASCADE)."""
    with connect() as conn:
        conn.execute("DELETE FROM jobs WHERE created_at < now() - make_interval(days => %s)", (days,))


def run_job(job_id: int) -> dict[str, Any]:
    """Run one validation attempt for a queued or failed job.

    Done jobs, jobs another worker is running, and jobs out of attempts are returned unchanged.
    """
    with connect() as conn:
        if not _lock_job(conn, job_id):
            return _read_job(conn, job_id)  # another worker is running this job

        raw_geojson = _claim_attempt(conn, job_id)
        if raw_geojson is None:
            return _read_job(conn, job_id)  # done, or no attempts left

        try:
            rows = _validate(conn, raw_geojson)
            _save_results_and_mark_done(conn, job_id, rows)
        except Exception as exc:
            _mark_failed(conn, job_id, str(exc))
            raise

    return get_job(job_id)


def _read_job(conn: Any, job_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cursor:
        return _job_from_cursor(cursor, job_id)


def _lock_job(conn: Any, job_id: int) -> bool:
    """Take this job's session lock; the lock is how other callers know a worker is alive.

    If we get the lock while the job says 'validating', the worker that set it has died.
    """
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (job_id,))
        locked = cursor.fetchone()[0]
        if locked:
            _mark_abandoned(cursor, job_id)
    return locked


def _claim_attempt(conn: Any, job_id: int) -> bytes | None:
    """Start the next attempt and return the stored GeoJSON, or None if the job can't run."""
    with conn.transaction(), conn.cursor() as cursor:
        # The WHERE clause is the claim: a job can move out of queued/failed only once per attempt.
        cursor.execute(
            """
            UPDATE jobs
            SET status = 'validating', attempts = attempts + 1,
                error = NULL, started_at = now(), finished_at = NULL, updated_at = now()
            WHERE id = %s AND status IN ('queued', 'failed') AND attempts < %s
            RETURNING original_geojson
            """,
            (job_id, MAX_ATTEMPTS),
        )
        row = cursor.fetchone()
    return bytes(row[0]) if row else None


def _validate(conn: Any, raw_geojson: bytes) -> list[tuple[int, str | None, list[str], list[str]]]:
    """Run the Python rules, then the PostGIS rules; return one result row per feature."""
    loaded = load_geojson_bytes(raw_geojson)

    feature_reasons = []
    for index, (feature, tokens) in enumerate(zip(loaded.features, loaded.coordinate_tokens)):
        try:
            feature_reasons.append(apply_feature_rules(feature, tokens))
        except ValueError as exc:
            raise ValueError(f"feature {index}: {exc}") from exc

    spatial_reasons = apply_spatial_rules(conn, loaded.features, feature_reasons)

    rows = []
    for index, feature in enumerate(loaded.features):
        reasons = feature_reasons[index] + spatial_reasons.get(index, [])
        codes = list(dict.fromkeys(code for code, _ in reasons))  # unique, in order
        messages = [message for _, message in reasons]
        rows.append((index, _farm_id(feature), codes, messages))
    return rows


def _farm_id(feature: dict[str, Any]) -> str | None:
    properties = feature.get("properties")
    farm_id = properties.get("farm_id") if isinstance(properties, dict) else None
    return farm_id if isinstance(farm_id, str) else None


def _save_results_and_mark_done(conn: Any, job_id: int, rows: list[tuple]) -> None:
    """Save every result and mark the job done in one transaction: all or nothing."""
    with conn.transaction(), conn.cursor() as cursor:
        for index, farm_id, codes, messages in rows:
            cursor.execute(
                """
                INSERT INTO results (job_id, feature_index, farm_id, valid, codes, messages)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (job_id, index, farm_id, not codes, codes, messages),
            )
        cursor.execute(
            """
            UPDATE jobs
            SET status = 'done', error = NULL, updated_at = now(), finished_at = now()
            WHERE id = %s AND status = 'validating'
            """,
            (job_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Job {job_id} lost its validating status before completion")


def _mark_failed(conn: Any, job_id: int, error: str) -> None:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
            SET status = 'failed', error = %s, updated_at = now(), finished_at = now()
            WHERE id = %s AND status = 'validating'
            """,
            (error, job_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Job {job_id} lost its validating status after failure")
