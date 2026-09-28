"""Database connection settings shared by the CLI and API."""

from __future__ import annotations

import os
from pathlib import Path

from psycopg import Connection, connect as psycopg_connect

DEFAULT_DATABASE_URL = "postgresql://farm_lists:farm_lists@localhost:5432/farm_lists"
SCHEMA_FILE = Path(__file__).parent.parent / "sql" / "001_init.sql"


def connect() -> Connection:
    return psycopg_connect(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))


def create_schema() -> None:
    """Create the extension and tables if they're missing. Safe to run on every start."""
    with connect() as conn:
        conn.execute(SCHEMA_FILE.read_text())
