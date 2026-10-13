"""Small DuckDB catalog helpers shared across modules."""

from __future__ import annotations

import duckdb


def table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()
    return bool(row and row[0] > 0)


def primary_key_column(conn: duckdb.DuckDBPyConnection, table_name: str) -> str:
    """Return the single-column primary key of a table (read from the catalog)."""
    row = conn.execute(
        """
        SELECT constraint_column_names
        FROM duckdb_constraints()
        WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'
        """,
        [table_name],
    ).fetchone()
    if row is None or len(row[0]) != 1:
        raise ValueError(f"Table {table_name!r} has no single-column primary key")
    return str(row[0][0])


def count_rows(conn: duckdb.DuckDBPyConnection, table_name: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
