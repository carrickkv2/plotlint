"""Small command-line interface over the job and report services."""

from __future__ import annotations

from pathlib import Path

import typer
import psycopg

from farm_list_check.jobs import run_job, submit_job
from farm_list_check.report import build_report, render_csv, render_json

app = typer.Typer(no_args_is_help=True, help="Validate GeoJSON farm lists.")


def _fail(message: str, code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


@app.command()
def submit(file: Path) -> None:
    """Store a GeoJSON file and print its job id."""
    try:
        job = submit_job(file)
    except (OSError, ValueError, psycopg.Error) as exc:
        _fail(str(exc))
    typer.echo(job["id"])


@app.command()
def run(job_id: int) -> None:
    """Run one queued job or explicitly retry a failed job."""
    try:
        job = run_job(job_id)
    except (LookupError, ValueError, psycopg.Error) as exc:
        _fail(str(exc), 1)
    if job["status"] == "done":
        typer.echo(f"Job {job_id}: done (attempt {job['attempts']})")
        return
    message = job["error"] or f"status is {job['status']}"
    _fail(f"Job {job_id} did not complete: {message}", 1)


@app.command()
def report(job_id: int, format: str = typer.Option("json", "--format")) -> None:
    """Print the completed per-feature report as JSON or CSV."""
    if format not in {"json", "csv"}:
        _fail("--format must be json or csv")
    try:
        data = build_report(job_id)
    except (LookupError, RuntimeError, psycopg.Error) as exc:
        _fail(str(exc), 1)
    typer.echo(render_json(data) if format == "json" else render_csv(data), nl=False)


if __name__ == "__main__":
    app()
