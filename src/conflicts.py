"""Log and resolve contradictory facts: disputes, claims, and source chunks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from src.chunker import TextChunk

RUN_METADATA_TABLE = "run_metadata"
SOURCE_CHUNKS_TABLE = "source_chunks"
FACT_DISPUTES_TABLE = "fact_disputes"
FACT_CLAIMS_TABLE = "fact_claims"

DISPUTE_STATUS_OPEN = "open"
DISPUTE_STATUS_RESOLVED_WRAPUP = "resolved_wrapup"
DISPUTE_STATUS_RESOLVED_MANUAL = "resolved_manual"

# Scalar fields where a changed non-empty value is a contradiction (not list merges).
CONFLICT_TRACKED_FIELDS: dict[str, frozenset[str]] = {
    "characters": frozenset({"blood_status", "house", "role_type", "occupation"}),
}


def ensure_dispute_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create run metadata, source chunks, disputes, and claims tables."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{RUN_METADATA_TABLE}" (
            "run_key" VARCHAR PRIMARY KEY,
            "pdf_path" VARCHAR NOT NULL,
            "chunk_size_words" INTEGER NOT NULL,
            "chunk_overlap_words" INTEGER NOT NULL,
            "created_at" TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{SOURCE_CHUNKS_TABLE}" (
            "chunk_index" INTEGER PRIMARY KEY,
            "word_start" INTEGER NOT NULL,
            "word_end" INTEGER NOT NULL,
            "text" VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{FACT_DISPUTES_TABLE}" (
            "dispute_id" INTEGER PRIMARY KEY,
            "table_name" VARCHAR NOT NULL,
            "entity_key" VARCHAR NOT NULL,
            "field_name" VARCHAR NOT NULL,
            "status" VARCHAR NOT NULL DEFAULT '{DISPUTE_STATUS_OPEN}',
            "canonical_value" VARCHAR,
            "resolution_reason" VARCHAR,
            "opened_at" TIMESTAMP DEFAULT current_timestamp,
            "resolved_at" TIMESTAMP,
            UNIQUE ("table_name", "entity_key", "field_name")
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{FACT_CLAIMS_TABLE}" (
            "claim_id" INTEGER PRIMARY KEY,
            "dispute_id" INTEGER NOT NULL,
            "chunk_index" INTEGER NOT NULL,
            "previous_value" VARCHAR,
            "incoming_value" VARCHAR NOT NULL,
            "recorded_at" TIMESTAMP DEFAULT current_timestamp
        )
        """
    )


def ensure_conflicts_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Backward-compatible alias for schema setup."""
    ensure_dispute_schema(conn)


def save_run_metadata(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_key: str,
    pdf_path: Path,
    chunk_size_words: int,
    chunk_overlap_words: int,
) -> None:
    """Record PDF path and chunk settings once per run (skip if already stored)."""
    existing = conn.execute(
        f'SELECT COUNT(*) FROM "{RUN_METADATA_TABLE}" WHERE "run_key" = ?',
        [run_key],
    ).fetchone()[0]
    if existing:
        return
    conn.execute(
        f"""
        INSERT INTO "{RUN_METADATA_TABLE}"
        ("run_key", "pdf_path", "chunk_size_words", "chunk_overlap_words")
        VALUES (?, ?, ?, ?)
        """,
        [run_key, str(pdf_path.resolve()), chunk_size_words, chunk_overlap_words],
    )


def source_chunks_populated(conn: duckdb.DuckDBPyConnection) -> bool:
    if not _table_exists(conn, SOURCE_CHUNKS_TABLE):
        return False
    count = conn.execute(f'SELECT COUNT(*) FROM "{SOURCE_CHUNKS_TABLE}"').fetchone()[0]
    return count > 0


def save_source_chunks(
    conn: duckdb.DuckDBPyConnection,
    chunks: list[TextChunk],
) -> None:
    """Store all chunk text once (skip if source_chunks already has rows)."""
    if not chunks or source_chunks_populated(conn):
        return
    for chunk in chunks:
        conn.execute(
            f"""
            INSERT INTO "{SOURCE_CHUNKS_TABLE}"
            ("chunk_index", "word_start", "word_end", "text")
            VALUES (?, ?, ?, ?)
            """,
            [chunk.index, chunk.word_start, chunk.word_end, chunk.text],
        )


def _is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _normalize(value: Any) -> str:
    return str(value).strip().lower()


def _next_id(conn: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
    return int(
        conn.execute(
            f'SELECT COALESCE(MAX("{column}"), 0) + 1 FROM "{table}"'
        ).fetchone()[0]
    )


def _get_or_create_dispute(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    entity_key: str,
    field_name: str,
) -> int:
    row = conn.execute(
        f"""
        SELECT "dispute_id", "status"
        FROM "{FACT_DISPUTES_TABLE}"
        WHERE "table_name" = ? AND "entity_key" = ? AND "field_name" = ?
        """,
        [table_name, entity_key, field_name],
    ).fetchone()

    if row is not None:
        dispute_id, status = row[0], row[1]
        if status != DISPUTE_STATUS_OPEN:
            conn.execute(
                f"""
                UPDATE "{FACT_DISPUTES_TABLE}"
                SET "status" = ?, "canonical_value" = NULL,
                    "resolution_reason" = NULL, "resolved_at" = NULL
                WHERE "dispute_id" = ?
                """,
                [DISPUTE_STATUS_OPEN, dispute_id],
            )
        return int(dispute_id)

    dispute_id = _next_id(conn, FACT_DISPUTES_TABLE, "dispute_id")
    conn.execute(
        f"""
        INSERT INTO "{FACT_DISPUTES_TABLE}"
        ("dispute_id", "table_name", "entity_key", "field_name", "status")
        VALUES (?, ?, ?, ?, ?)
        """,
        [dispute_id, table_name, entity_key, field_name, DISPUTE_STATUS_OPEN],
    )
    return dispute_id


def record_fact_claim(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    entity_key: str,
    field_name: str,
    previous_value: Any,
    incoming_value: Any,
    chunk_index: int,
) -> None:
    dispute_id = _get_or_create_dispute(
        conn,
        table_name=table_name,
        entity_key=entity_key,
        field_name=field_name,
    )
    claim_id = _next_id(conn, FACT_CLAIMS_TABLE, "claim_id")
    conn.execute(
        f"""
        INSERT INTO "{FACT_CLAIMS_TABLE}"
        ("claim_id", "dispute_id", "chunk_index", "previous_value", "incoming_value")
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            claim_id,
            dispute_id,
            chunk_index,
            None if _is_empty(previous_value) else str(previous_value),
            str(incoming_value),
        ],
    )


def log_scalar_conflicts(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    entity_key: str,
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    chunk_index: int,
) -> None:
    """Record contradictions for tracked scalar fields before merge."""
    tracked = CONFLICT_TRACKED_FIELDS.get(table_name)
    if not tracked:
        return

    for field_name in tracked:
        previous_value = existing.get(field_name) if existing else None
        incoming_value = incoming.get(field_name)
        if _is_empty(previous_value) or _is_empty(incoming_value):
            continue
        if _normalize(previous_value) == _normalize(incoming_value):
            continue
        record_fact_claim(
            conn,
            table_name=table_name,
            entity_key=entity_key,
            field_name=field_name,
            previous_value=previous_value,
            incoming_value=incoming_value,
            chunk_index=chunk_index,
        )


@dataclass(frozen=True)
class ClaimRecord:
    chunk_index: int
    incoming_value: str
    previous_value: str | None
    chunk_text: str
    word_start: int | None = None
    word_end: int | None = None


@dataclass(frozen=True)
class ConflictGroup:
    """All claims for one open dispute on an entity field."""

    dispute_id: int
    table_name: str
    entity_key: str
    field_name: str
    current_value: str | None
    claims: list[ClaimRecord]


def fetch_unresolved_conflict_groups(conn: duckdb.DuckDBPyConnection) -> list[ConflictGroup]:
    rows = conn.execute(
        f"""
        SELECT "dispute_id", "table_name", "entity_key", "field_name"
        FROM "{FACT_DISPUTES_TABLE}"
        WHERE "status" = ?
        ORDER BY "table_name", "entity_key", "field_name"
        """,
        [DISPUTE_STATUS_OPEN],
    ).fetchall()

    groups: list[ConflictGroup] = []
    for dispute_id, table_name, entity_key, field_name in rows:
        claims_rows = conn.execute(
            f"""
            SELECT c."chunk_index", c."incoming_value", c."previous_value",
                   sc."text", sc."word_start", sc."word_end"
            FROM "{FACT_CLAIMS_TABLE}" c
            LEFT JOIN "{SOURCE_CHUNKS_TABLE}" sc ON sc."chunk_index" = c."chunk_index"
            WHERE c."dispute_id" = ?
            ORDER BY c."chunk_index"
            """,
            [dispute_id],
        ).fetchall()
        claims = [
            ClaimRecord(
                chunk_index=int(r[0]),
                incoming_value=str(r[1]),
                previous_value=None if r[2] is None else str(r[2]),
                chunk_text="" if r[3] is None else str(r[3]),
                word_start=None if r[4] is None else int(r[4]),
                word_end=None if r[5] is None else int(r[5]),
            )
            for r in claims_rows
        ]

        current_row = conn.execute(
            f'SELECT "{field_name}" FROM "{table_name}" WHERE '
            f'"{_unique_key_for_table(table_name)}" = ?',
            [entity_key],
        ).fetchone()
        current_value = None if current_row is None else current_row[0]
        if current_value is not None:
            current_value = str(current_value)

        groups.append(
            ConflictGroup(
                dispute_id=int(dispute_id),
                table_name=str(table_name),
                entity_key=str(entity_key),
                field_name=str(field_name),
                current_value=current_value,
                claims=claims,
            )
        )
    return groups


def _unique_key_for_table(table_name: str) -> str:
    if table_name == "characters":
        return "character_name"
    if table_name == "spells":
        return "spell_name"
    if table_name == "houses":
        return "house_name"
    raise ValueError(f"No unique key mapping for table {table_name!r}")


def apply_field_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    entity_key: str,
    field_name: str,
    canonical_value: str | None,
    reason: str,
    status: str = DISPUTE_STATUS_RESOLVED_WRAPUP,
) -> None:
    unique_key = _unique_key_for_table(table_name)
    conn.execute(
        f'UPDATE "{table_name}" SET "{field_name}" = ? WHERE "{unique_key}" = ?',
        [canonical_value, entity_key],
    )
    conn.execute(
        f"""
        UPDATE "{FACT_DISPUTES_TABLE}"
        SET "status" = ?, "canonical_value" = ?, "resolution_reason" = ?,
            "resolved_at" = current_timestamp
        WHERE "table_name" = ? AND "entity_key" = ? AND "field_name" = ?
          AND "status" = ?
        """,
        [
            status,
            canonical_value,
            reason,
            table_name,
            entity_key,
            field_name,
            DISPUTE_STATUS_OPEN,
        ],
    )


def apply_manual_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    canonical_value: str | None,
    reason: str,
    table_name: str = "characters",
) -> bool:
    """Resolve an open dispute manually. Returns False if no open dispute found."""
    row = conn.execute(
        f"""
        SELECT "dispute_id"
        FROM "{FACT_DISPUTES_TABLE}"
        WHERE "table_name" = ? AND "entity_key" = ? AND "field_name" = ?
          AND "status" = ?
        """,
        [table_name, entity_key, field_name, DISPUTE_STATUS_OPEN],
    ).fetchone()
    if row is None:
        return False
    apply_field_resolution(
        conn,
        table_name=table_name,
        entity_key=entity_key,
        field_name=field_name,
        canonical_value=canonical_value,
        reason=reason,
        status=DISPUTE_STATUS_RESOLVED_MANUAL,
    )
    return True


def count_open_disputes(conn: duckdb.DuckDBPyConnection) -> int:
    if not _table_exists(conn, FACT_DISPUTES_TABLE):
        return 0
    return int(
        conn.execute(
            f'SELECT COUNT(*) FROM "{FACT_DISPUTES_TABLE}" WHERE "status" = ?',
            [DISPUTE_STATUS_OPEN],
        ).fetchone()[0]
    )


def count_open_claims(conn: duckdb.DuckDBPyConnection) -> int:
    if not _table_exists(conn, FACT_CLAIMS_TABLE):
        return 0
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM "{FACT_CLAIMS_TABLE}" c
            JOIN "{FACT_DISPUTES_TABLE}" d ON d."dispute_id" = c."dispute_id"
            WHERE d."status" = ?
            """,
            [DISPUTE_STATUS_OPEN],
        ).fetchone()[0]
    )


def count_unresolved_conflicts(conn: duckdb.DuckDBPyConnection) -> int:
    """Backward-compatible alias: returns open claim count."""
    return count_open_claims(conn)


def _table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0] > 0)
