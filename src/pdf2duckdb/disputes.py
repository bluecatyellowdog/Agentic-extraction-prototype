"""Log and resolve contradictory facts: disputes, claims, and source chunks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from pdf2duckdb.chunker import TextChunk
from pdf2duckdb.dbutil import primary_key_column, table_exists

RUN_METADATA_TABLE = "run_metadata"
SOURCE_CHUNKS_TABLE = "source_chunks"
FACT_DISPUTES_TABLE = "fact_disputes"
FACT_CLAIMS_TABLE = "fact_claims"

DISPUTE_STATUS_OPEN = "open"
DISPUTE_STATUS_RESOLVED_WRAPUP = "resolved_wrapup"
DISPUTE_STATUS_RESOLVED_MANUAL = "resolved_manual"


def ensure_dispute_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create run metadata, source chunks, disputes, and claims tables."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{RUN_METADATA_TABLE}" (
            "run_key" VARCHAR PRIMARY KEY,
            "pdf_path" VARCHAR NOT NULL,
            "profile" VARCHAR,
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


def save_run_metadata(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_key: str,
    pdf_path: Path,
    profile_name: str,
    chunk_size_words: int,
    chunk_overlap_words: int,
) -> None:
    """Record PDF path, profile and chunk settings once per run (skip if already stored)."""
    existing = conn.execute(
        f'SELECT COUNT(*) FROM "{RUN_METADATA_TABLE}" WHERE "run_key" = ?',
        [run_key],
    ).fetchone()[0]
    if existing:
        return
    conn.execute(
        f"""
        INSERT INTO "{RUN_METADATA_TABLE}"
        ("run_key", "pdf_path", "profile", "chunk_size_words", "chunk_overlap_words")
        VALUES (?, ?, ?, ?, ?)
        """,
        [run_key, str(pdf_path.resolve()), profile_name, chunk_size_words, chunk_overlap_words],
    )


def source_chunks_populated(conn: duckdb.DuckDBPyConnection) -> bool:
    if not table_exists(conn, SOURCE_CHUNKS_TABLE):
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
        conn.execute(f'SELECT COALESCE(MAX("{column}"), 0) + 1 FROM "{table}"').fetchone()[0]
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
    tracked_fields: list[str],
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    chunk_index: int,
) -> None:
    """Record contradictions for tracked scalar fields before merge."""
    for field_name in tracked_fields:
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
            f'"{primary_key_column(conn, table_name)}" = ?',
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
    unique_key = primary_key_column(conn, table_name)
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


def find_dispute_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    table_name: str | None = None,
    open_only: bool = False,
) -> str | None:
    """Return the table of the dispute on entity.field, or None if there is none.

    Raises ValueError when table_name is omitted and several tables match.
    """
    query = f"""
        SELECT DISTINCT "table_name" FROM "{FACT_DISPUTES_TABLE}"
        WHERE "entity_key" = ? AND "field_name" = ?
    """
    params: list[Any] = [entity_key, field_name]
    if table_name is not None:
        query += ' AND "table_name" = ?'
        params.append(table_name)
    if open_only:
        query += ' AND "status" = ?'
        params.append(DISPUTE_STATUS_OPEN)
    tables = [row[0] for row in conn.execute(query, params).fetchall()]
    if len(tables) > 1:
        raise ValueError(
            f"{entity_key}.{field_name} is disputed in several tables ({', '.join(tables)}); "
            "pass --table"
        )
    return tables[0] if tables else None


def apply_manual_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    canonical_value: str | None,
    reason: str,
    table_name: str | None = None,
) -> bool:
    """Resolve an open dispute manually. Returns False if no open dispute found."""
    table_name = find_dispute_table(
        conn,
        entity_key=entity_key,
        field_name=field_name,
        table_name=table_name,
        open_only=True,
    )
    if table_name is None:
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
    if not table_exists(conn, FACT_DISPUTES_TABLE):
        return 0
    return int(
        conn.execute(
            f'SELECT COUNT(*) FROM "{FACT_DISPUTES_TABLE}" WHERE "status" = ?',
            [DISPUTE_STATUS_OPEN],
        ).fetchone()[0]
    )


def count_open_claims(conn: duckdb.DuckDBPyConnection) -> int:
    if not table_exists(conn, FACT_CLAIMS_TABLE):
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
