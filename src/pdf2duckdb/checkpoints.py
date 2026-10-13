"""Pipeline checkpoint helpers for resumable DuckDB extraction runs."""

from __future__ import annotations

from pathlib import Path

import duckdb

CHECKPOINT_TABLE = "pipeline_checkpoints"
_DATA_TABLES = ("characters", "spells", "houses", "house_point_events")
_RESUME_DATA_TABLES = ("characters", "spells", "house_point_events")


def make_run_key(
    pdf_path: Path,
    *,
    chunk_size_words: int,
    chunk_overlap_words: int,
) -> str:
    """Stable id for a PDF + chunking configuration."""
    return f"{pdf_path.resolve()}|{chunk_size_words}|{chunk_overlap_words}"


def ensure_checkpoint_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{CHECKPOINT_TABLE}" (
            "run_key" VARCHAR,
            "chunk_index" INTEGER,
            "completed_at" TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY ("run_key", "chunk_index")
        )
        """
    )


def table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0] > 0)


def has_extraction_data(conn: duckdb.DuckDBPyConnection) -> bool:
    for table_name in _DATA_TABLES:
        if not table_exists(conn, table_name):
            continue
        count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        if count > 0:
            return True
    return False


def has_resume_blocking_data(conn: duckdb.DuckDBPyConnection) -> bool:
    """Rows that block --resume when no checkpoints exist (excludes seeded house rows)."""
    for table_name in _RESUME_DATA_TABLES:
        if not table_exists(conn, table_name):
            continue
        count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        if count > 0:
            return True
    return False


def get_completed_chunk_indices(conn: duckdb.DuckDBPyConnection, run_key: str) -> set[int]:
    """Return chunk indices already checkpointed for this run."""
    if not table_exists(conn, CHECKPOINT_TABLE):
        return set()

    distinct_keys = {
        row[0]
        for row in conn.execute(
            f'SELECT DISTINCT "run_key" FROM "{CHECKPOINT_TABLE}"'
        ).fetchall()
    }
    if not distinct_keys:
        if has_resume_blocking_data(conn):
            raise ValueError(
                "Cannot --resume: the database has extracted rows but no checkpoints "
                "(likely from a run before checkpointing existed). "
                "Use a new --output path or re-run without --resume to start fresh."
            )
        return set()

    if distinct_keys != {run_key}:
        raise ValueError(
            "Cannot --resume: checkpointed run settings do not match this run "
            f"(expected {run_key!r}, found {sorted(distinct_keys)!r}). "
            "Use a new --output path or re-run without --resume to start fresh."
        )

    return {
        row[0]
        for row in conn.execute(
            f'SELECT "chunk_index" FROM "{CHECKPOINT_TABLE}" WHERE "run_key" = ?',
            [run_key],
        ).fetchall()
    }


def record_chunk_checkpoint(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_key: str,
    chunk_index: int,
) -> None:
    """Mark a chunk as fully written (call only after successful DB upsert/insert)."""
    conn.execute(
        f"""
        INSERT OR REPLACE INTO "{CHECKPOINT_TABLE}"
        ("run_key", "chunk_index", "completed_at")
        VALUES (?, ?, current_timestamp)
        """,
        [run_key, chunk_index],
    )


def load_entity_keys(conn: duckdb.DuckDBPyConnection, table_name: str, key_column: str) -> list[str]:
    """Load unique keys from a table for resume (known entities in prompts)."""
    if not table_exists(conn, table_name):
        return []
    rows = conn.execute(
        f'SELECT "{key_column}" FROM "{table_name}" '
        f'WHERE "{key_column}" IS NOT NULL AND "{key_column}" != \'\''
    ).fetchall()
    return [str(row[0]) for row in rows]
