"""HTTP API over the same job and report services the CLI uses."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from plotlint.db import create_schema
from plotlint.jobs import delete_jobs_older_than, get_job, run_job, submit_job
from plotlint.report import JobNotDoneError, build_report, build_result_geojson, render_csv

logger = logging.getLogger(__name__)

# Limits for a public demo: anyone can upload, so keep files small and jobs short-lived.
MAX_UPLOAD_BYTES = 2_000_000
KEEP_JOBS_FOR_DAYS = 7


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_schema()  # a fresh database (e.g. on Railway) gets its tables on first start
    yield


app = FastAPI(title="plotlint", description="Validate EUDR farm lists (GeoJSON).", lifespan=lifespan)


# ---------- response models ----------

class SubmittedJob(BaseModel):
    job_id: int
    status: str


class Job(BaseModel):
    id: int
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class FeatureResult(BaseModel):
    feature_index: int
    farm_id: str | None
    valid: bool
    codes: list[str]
    messages: list[str]


class Summary(BaseModel):
    total: int
    valid: int
    invalid: int
    counts_by_code: dict[str, int]


class Report(BaseModel):
    job_id: int
    results: list[FeatureResult]
    summary: Summary


class ResultFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    features: list[dict[str, Any]]
    summary: Summary


# ---------- routes ----------

@app.get("/", include_in_schema=False)
def map_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/farm-lists", status_code=202, response_model=SubmittedJob)
async def submit_farm_list(request: Request, background_tasks: BackgroundTasks) -> SubmittedJob:
    """Upload a GeoJSON FeatureCollection as the raw request body. The same file returns the same job."""
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Farm lists are limited to {MAX_UPLOAD_BYTES:,} bytes")
    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BYTES:  # the header can be missing or wrong
        raise HTTPException(413, f"Farm lists are limited to {MAX_UPLOAD_BYTES:,} bytes")
    if not raw:
        raise HTTPException(422, "Request body must be a GeoJSON FeatureCollection")

    await run_in_threadpool(delete_jobs_older_than, KEEP_JOBS_FOR_DAYS)
    try:
        # submit_job does blocking database I/O, so run it off the event loop.
        job = await run_in_threadpool(submit_job, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if job["status"] == "queued":
        background_tasks.add_task(_run_in_background, job["id"])
    return SubmittedJob(job_id=job["id"], status=job["status"])


@app.get("/jobs/{job_id}", response_model=Job)
def read_job(job_id: int) -> dict[str, Any]:
    return _or_404(get_job, job_id)


@app.get("/jobs/{job_id}/report", response_model=Report)
def read_report(job_id: int, format: Literal["json", "csv"] = "json") -> Any:
    report = _or_404(build_report, job_id)
    if format == "csv":
        return PlainTextResponse(render_csv(report), media_type="text/csv")
    return report


@app.get("/jobs/{job_id}/geojson", response_model=ResultFeatureCollection)
def read_result_geojson(job_id: int) -> dict[str, Any]:
    return _or_404(build_result_geojson, job_id)


# ---------- helpers ----------

def _or_404(fetch: Any, job_id: int) -> Any:
    """Turn 'no such job' into 404 and 'job not finished' into 409."""
    try:
        return fetch(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except JobNotDoneError as exc:
        raise HTTPException(409, str(exc)) from exc


def _run_in_background(job_id: int) -> None:
    # BackgroundTasks runs after the response is sent, in the same process. Fine for a demo;
    # in production this would be a separate worker reading from a queue (e.g. SQS).
    try:
        run_job(job_id)
    except Exception:
        # run_job has already saved the failure on the job, so the client sees it via GET /jobs/{id}.
        logger.exception("Job %s failed", job_id)
