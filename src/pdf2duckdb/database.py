"""DuckDB table creation and merge/insert logic, driven by the extraction profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from pdf2duckdb.checkpoints import ensure_checkpoint_table
from pdf2duckdb.dbutil import count_rows, table_exists
from pdf2duckdb.disputes import ensure_dispute_schema, log_scalar_conflicts
from pdf2duckdb.profile import (
    CHUNK_INDEX_COLUMN,
    ROW_ID_COLUMN,
    ColumnSpec,
    ExtractionProfile,
    TableSpec,
)


def prepare_database(
    db_path: Path,
    profile: ExtractionProfile,
    *,
    resume: bool,
) -> duckdb.DuckDBPyConnection:
    """
    Open or create a DuckDB file for a profile.

    When resume=False, delete any existing file and create empty tables.
    When resume=True, keep the file and create only what is missing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not resume and db_path.exists():
        db_path.unlink()

    conn = duckdb.connect(str(db_path))
    for table in profile.tables:
        if not table_exists(conn, table.name):
            _create_table(conn, table)
        if table.seed_keys and count_rows(conn, table.name) == 0:
            _seed_table(conn, table)
    ensure_checkpoint_table(conn)
    ensure_dispute_schema(conn)
    return conn


def _create_table(conn: duckdb.DuckDBPyConnection, table: TableSpec) -> None:
    parts: list[str] = []
    if table.is_append_only:
        parts += [f'"{ROW_ID_COLUMN}" INTEGER PRIMARY KEY', f'"{CHUNK_INDEX_COLUMN}" INTEGER']
    for column in table.columns:
        sql = f'"{column.name}" {column.type}'
        if column.name == table.key:
            sql += " PRIMARY KEY"
        parts.append(sql)
    conn.execute(f'CREATE TABLE "{table.name}" ({", ".join(parts)})')


def _seed_table(conn: duckdb.DuckDBPyConnection, table: TableSpec) -> None:
    for key_value in table.seed_keys:
        conn.execute(f'INSERT INTO "{table.name}" ("{table.key}") VALUES (?)', [key_value])


def merge_text(existing: str | None, incoming: str | None, *, separator: str) -> str | None:
    """Combine delimited fragments, dropping case-insensitive duplicates."""
    if not incoming:
        return existing
    if not existing:
        return incoming

    split_on = separator.strip() or separator
    seen: set[str] = set()
    merged: list[str] = []
    for part in existing.split(split_on) + incoming.split(split_on):
        item = part.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            merged.append(item)
    return separator.join(merged) if merged else None


def _merge_value(column: ColumnSpec, existing: Any, incoming: Any) -> Any:
    if incoming is None or incoming == "":
        return existing
    if column.merge == "append":
        return merge_text(existing, str(incoming), separator=column.separator)
    return incoming


def merge_rows(
    table: TableSpec,
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Keep existing values unless the incoming row has a non-empty new value."""
    existing = existing or {}
    return {
        column.name: _merge_value(column, existing.get(column.name), incoming.get(column.name))
        for column in table.columns
    }


def _fetch_existing_row(
    conn: duckdb.DuckDBPyConnection,
    table: TableSpec,
    key_value: Any,
) -> dict[str, Any] | None:
    names = [column.name for column in table.columns]
    columns_sql = ", ".join(f'"{name}"' for name in names)
    row = conn.execute(
        f'SELECT {columns_sql} FROM "{table.name}" WHERE "{table.key}" = ?',
        [key_value],
    ).fetchone()
    return None if row is None else dict(zip(names, row, strict=True))


def upsert_rows(
    conn: duckdb.DuckDBPyConnection,
    table: TableSpec,
    rows: list[dict[str, Any]],
    *,
    chunk_index: int | None = None,
) -> int:
    """
    Insert or merge rows into an entity table by its key.

    When chunk_index is set, contradictions on tracked columns are logged as disputes.
    """
    names = [column.name for column in table.columns]
    columns_sql = ", ".join(f'"{name}"' for name in names)
    placeholders = ", ".join("?" for _ in names)
    insert_sql = f'INSERT OR REPLACE INTO "{table.name}" ({columns_sql}) VALUES ({placeholders})'

    upserted = 0
    for row in rows:
        key_value = row.get(table.key)
        if key_value is None or key_value == "":
            continue
        existing = _fetch_existing_row(conn, table, key_value)
        if chunk_index is not None and table.tracked_fields:
            log_scalar_conflicts(
                conn,
                table_name=table.name,
                entity_key=str(key_value),
                tracked_fields=table.tracked_fields,
                existing=existing,
                incoming=row,
                chunk_index=chunk_index,
            )
        merged = merge_rows(table, existing, row)
        conn.execute(insert_sql, [merged.get(name) for name in names])
        upserted += 1
    return upserted


def insert_append_rows(
    conn: duckdb.DuckDBPyConnection,
    table: TableSpec,
    rows: list[dict[str, Any]],
    *,
    chunk_index: int,
) -> int:
    """Append rows to an append-only table with auto ids and source chunk index."""
    names = [ROW_ID_COLUMN, CHUNK_INDEX_COLUMN, *(column.name for column in table.columns)]
    columns_sql = ", ".join(f'"{name}"' for name in names)
    placeholders = ", ".join("?" for _ in names)
    next_id = int(
        conn.execute(
            f'SELECT COALESCE(MAX("{ROW_ID_COLUMN}"), 0) + 1 FROM "{table.name}"'
        ).fetchone()[0]
    )
    for offset, row in enumerate(rows):
        values = [next_id + offset, chunk_index, *(row.get(c.name) for c in table.columns)]
        conn.execute(f'INSERT INTO "{table.name}" ({columns_sql}) VALUES ({placeholders})', values)
    return len(rows)


def write_rows(
    conn: duckdb.DuckDBPyConnection,
    table: TableSpec,
    rows: list[dict[str, Any]],
    *,
    chunk_index: int,
) -> int:
    if not rows:
        return 0
    if table.is_append_only:
        return insert_append_rows(conn, table, rows, chunk_index=chunk_index)
    return upsert_rows(conn, table, rows, chunk_index=chunk_index)
