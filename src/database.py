"""DuckDB table creation, upsert merge logic, checkpoints, and point-event inserts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from src.checkpoints import ensure_checkpoint_table, table_exists
from src.conflicts import ensure_conflicts_table, log_scalar_conflicts
from src.schemas import (
    HOGWARTS_HOUSE_NAMES,
    ColumnDefinition,
    DatabasePlan,
    row_to_dict,
)


def prepare_database(
    db_path: Path,
    *plans: DatabasePlan,
    resume: bool,
) -> duckdb.DuckDBPyConnection:
    """
    Open or create a DuckDB file.

    When resume=False, delete any existing file and create empty tables.
    When resume=True, keep the file and ensure schema + checkpoint table exist.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if resume:
        conn = duckdb.connect(str(db_path))
        _ensure_schema(conn, plans)
        ensure_checkpoint_table(conn)
        return conn

    if db_path.exists():
        db_path.unlink()

    conn = duckdb.connect(str(db_path))
    _create_schema(conn, plans)
    return conn


def create_database(db_path: Path, *plans: DatabasePlan) -> duckdb.DuckDBPyConnection:
    """Create a fresh DuckDB file (deletes existing file). Backward-compatible alias."""
    return prepare_database(db_path, *plans, resume=False)


def _create_schema(conn: duckdb.DuckDBPyConnection, plans: tuple[DatabasePlan, ...]) -> None:
    for plan in plans:
        _create_table(conn, plan.table_name, plan.columns, plan.unique_key)
    ensure_checkpoint_table(conn)
    ensure_conflicts_table(conn)
    if any(plan.table_name == "houses" for plan in plans):
        seed_houses(conn)


def _ensure_schema(conn: duckdb.DuckDBPyConnection, plans: tuple[DatabasePlan, ...]) -> None:
    for plan in plans:
        if not table_exists(conn, plan.table_name):
            _create_table(conn, plan.table_name, plan.columns, plan.unique_key)
    ensure_conflicts_table(conn)
    if any(plan.table_name == "houses" for plan in plans) and table_exists(conn, "houses"):
        count = conn.execute('SELECT COUNT(*) FROM "houses"').fetchone()[0]
        if count == 0:
            seed_houses(conn)


def seed_houses(conn: duckdb.DuckDBPyConnection) -> None:
    """Insert empty rows for all four Hogwarts houses."""
    for house_name in HOGWARTS_HOUSE_NAMES:
        conn.execute(
            'INSERT INTO "houses" ("house_name") VALUES (?)',
            [house_name],
        )


def _create_table(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: list[ColumnDefinition],
    unique_key: str,
) -> None:
    parts: list[str] = []
    for column in columns:
        sql = f'"{column.name}" {column.duckdb_type}'
        if column.name == unique_key:
            sql += " PRIMARY KEY"
        parts.append(sql)

    conn.execute(f'CREATE TABLE "{table_name}" ({", ".join(parts)})')


def _fetch_existing_row(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    unique_key: str,
    key_value: Any,
    column_names: list[str],
) -> dict[str, Any] | None:
    columns_sql = ", ".join(f'"{name}"' for name in column_names)
    row = conn.execute(
        f'SELECT {columns_sql} FROM "{table_name}" WHERE "{unique_key}" = ?',
        [key_value],
    ).fetchone()
    if row is None:
        return None
    return dict(zip(column_names, row))


def _merge_combined_text(
    existing: str | None,
    incoming: str | None,
    *,
    separator: str,
) -> str | None:
    """Combine text fragments without duplicate segments (case-insensitive)."""
    if not incoming:
        return existing
    if not existing:
        return incoming

    split_on = separator.strip()
    seen: set[str] = set()
    merged: list[str] = []
    for part in existing.split(split_on) + incoming.split(split_on):
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return separator.join(merged) if merged else None


def _merge_rows(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    column_names: list[str],
) -> dict[str, Any]:
    """Keep existing values unless the incoming row has a non-empty new value."""
    merged = dict(existing or {})
    for name in column_names:
        new_value = incoming.get(name)
        if new_value is None or new_value == "":
            continue
        if name == "character_description":
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator="; ",
            )
        elif name == "classes_attended":
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator=", ",
            )
        elif name == "used_by":
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator=", ",
            )
        elif name == "effect_description":
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator="; ",
            )
        elif name in {
            "current_students",
            "prior_students",
            "others_not_in_house",
        }:
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator=", ",
            )
        elif name == "lore_facts":
            merged[name] = _merge_combined_text(
                merged.get(name) if merged else None,
                str(new_value),
                separator="; ",
            )
        else:
            merged[name] = new_value
    return merged


def upsert_rows(
    conn: duckdb.DuckDBPyConnection,
    plan: DatabasePlan,
    rows: list[dict[str, Any]],
    *,
    chunk_index: int | None = None,
) -> int:
    """
    Insert or update rows by unique_key.

    New non-empty values overwrite old ones; empty values keep what we already had.
    When chunk_index is set, logs scalar contradictions as dispute claims.
    """
    if not rows:
        return 0

    column_names = [column.name for column in plan.columns]
    placeholders = ", ".join("?" for _ in column_names)
    columns_sql = ", ".join(f'"{name}"' for name in column_names)

    insert_sql = (
        f'INSERT OR REPLACE INTO "{plan.table_name}" ({columns_sql}) '
        f"VALUES ({placeholders})"
    )

    upserted = 0
    for row in rows:
        key_value = row.get(plan.unique_key)
        if key_value is None or key_value == "":
            continue

        existing = _fetch_existing_row(
            conn, plan.table_name, plan.unique_key, key_value, column_names
        )
        if chunk_index is not None:
            log_scalar_conflicts(
                conn,
                table_name=plan.table_name,
                entity_key=str(key_value),
                existing=existing,
                incoming=row,
                chunk_index=chunk_index,
            )
        merged = _merge_rows(existing, row, column_names)
        values = [merged.get(name) for name in column_names]
        conn.execute(insert_sql, values)
        upserted += 1

    return upserted


def sync_house_roster_from_characters(
    conn: duckdb.DuckDBPyConnection,
    house_plan: DatabasePlan,
) -> int:
    """
    Merge current student names from the characters table into each house row.

    Keeps house rosters aligned with characters who have role_type=student.
    """
    rows = conn.execute(
        """
        SELECT house, character_name
        FROM characters
        WHERE house IS NOT NULL
          AND role_type = 'student'
          AND character_name IS NOT NULL
          AND character_name != ''
        """
    ).fetchall()

    students_by_house: dict[str, list[str]] = {}
    for house, character_name in rows:
        if house not in HOGWARTS_HOUSE_NAMES:
            continue
        students_by_house.setdefault(house, []).append(str(character_name))

    updated = 0
    for house_name, student_names in students_by_house.items():
        if upsert_rows(
            conn,
            house_plan,
            [{"house_name": house_name, "current_students": ", ".join(student_names)}],
        ):
            updated += 1
    return updated


def insert_point_event_rows(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    rows: list[dict[str, Any]],
) -> int:
    """Append house point events (one row per award/deduction; not upserted by house)."""
    if not rows:
        return 0

    inserted = 0
    for row in rows:
        house_name = row.get("house_name")
        points = row.get("points")
        if not house_name or points is None:
            continue

        next_id = conn.execute(
            f'SELECT COALESCE(MAX("event_id"), 0) + 1 FROM "{table_name}"'
        ).fetchone()[0]

        conn.execute(
            f'INSERT INTO "{table_name}" '
            f'("event_id", "house_name", "points", "caused_by_student", "reason") '
            f"VALUES (?, ?, ?, ?, ?)",
            [
                next_id,
                house_name,
                int(points),
                row.get("caused_by_student"),
                row.get("reason"),
            ],
        )
        inserted += 1

    return inserted


def rows_from_extraction(
    extraction_rows: list,
    column_names: list[str],
) -> list[dict[str, Any]]:
    """Convert agent output rows into plain dicts for DuckDB."""
    return [row_to_dict(row, column_names) for row in extraction_rows]


def print_table_preview(conn: duckdb.DuckDBPyConnection, table_name: str, limit: int = 10) -> None:
    """Print a small preview of the finished table."""
    result = conn.execute(f'SELECT * FROM "{table_name}" LIMIT {limit}').fetchall()
    columns = [desc[0] for desc in conn.description]

    print("\n--- Database preview ---")
    print("Columns:", ", ".join(columns))
    print(f"Rows shown: {len(result)} (limit {limit})")
    for row in result:
        print(dict(zip(columns, row)))

    total = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
    print(f"Total rows in table: {total}")
    print("------------------------\n")
