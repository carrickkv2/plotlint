"""Database connection settings shared by the CLI and API."""

from __future__ import annotations

import os

from psycopg import Connection, connect as psycopg_connect

DEFAULT_DATABASE_URL = "postgresql://farm_lists:farm_lists@localhost:5432/farm_lists"


def connect() -> Connection:
    return psycopg_connect(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
