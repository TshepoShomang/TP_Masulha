"""Utility helpers to create/reset the PostgreSQL database schema.

Run:
    python backend/database/db.py --schema backend/database/postgres_schema.sql

The script reads DATABASE_URL from .env (or environment) and applies the SQL
statements sequentially so the remote database matches the repository schema.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable

import psycopg2
from dotenv import load_dotenv


def _load_sql_statements(sql_text: str) -> Iterable[str]:
    """Split a SQL file into executable statements (naive but works for our schema)."""
    statement_lines = []
    for raw_line in sql_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        statement_lines.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(statement_lines).rstrip().rstrip(";")
            if statement:
                yield statement
            statement_lines = []
    # Catch any trailing statement without semicolon
    if statement_lines:
        statement = "\n".join(statement_lines).rstrip().rstrip(";")
        if statement:
            yield statement


def apply_schema(schema_path: Path) -> None:
    """Execute every SQL statement in schema_path against DATABASE_URL."""
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured in environment or .env file.")

    sql_text = schema_path.read_text(encoding="utf-8")
    statements = list(_load_sql_statements(sql_text))
    if not statements:
        raise RuntimeError(f"No SQL statements found in {schema_path}")

    with psycopg2.connect(database_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply the project schema to the configured database.")
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("backend/database/postgres_schema.sql"),
        help="Path to the SQL schema file (defaults to backend/database/postgres_schema.sql).",
    )
    args = parser.parse_args()

    apply_schema(args.schema)
    print(f"Schema from {args.schema} applied successfully.")


if __name__ == "__main__":
    main()
