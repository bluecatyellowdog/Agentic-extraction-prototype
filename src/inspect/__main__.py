"""Dispute inspection and manual resolution CLI."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import duckdb

from src.checkpoints import table_exists
from src.conflicts import (
    DISPUTE_STATUS_OPEN,
    FACT_CLAIMS_TABLE,
    FACT_DISPUTES_TABLE,
    SOURCE_CHUNKS_TABLE,
    apply_manual_resolution,
)
from src.database import sync_house_roster_from_characters
from src.schemas import HOUSE_DATABASE_PLAN

_CHUNK_PREVIEW_LIMIT = 2000


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    return duckdb.connect(str(db_path))


def cmd_list(conn: duckdb.DuckDBPyConnection, *, open_only: bool) -> int:
    if not table_exists(conn, FACT_DISPUTES_TABLE):
        print("No fact_disputes table in this database.")
        return 1

    status_filter = f'WHERE d."status" = \'{DISPUTE_STATUS_OPEN}\'' if open_only else ""
    rows = conn.execute(
        f"""
        SELECT d."dispute_id", d."entity_key", d."field_name", d."status",
               COUNT(c."claim_id") AS claim_count,
               MIN(c."chunk_index") AS first_chunk,
               MAX(c."chunk_index") AS last_chunk
        FROM "{FACT_DISPUTES_TABLE}" d
        LEFT JOIN "{FACT_CLAIMS_TABLE}" c ON c."dispute_id" = d."dispute_id"
        {status_filter}
        GROUP BY d."dispute_id", d."entity_key", d."field_name", d."status"
        ORDER BY d."entity_key", d."field_name"
        """
    ).fetchall()

    label = "open" if open_only else "all"
    if not rows:
        print(f"No {label} disputes.")
        return 0

    print(f"Disputes ({label}): {len(rows)}\n")
    for dispute_id, entity_key, field_name, status, claim_count, first_chunk, last_chunk in rows:
        chunk_range = (
            f"chunk {first_chunk}"
            if first_chunk == last_chunk
            else f"chunks {first_chunk}-{last_chunk}"
        )
        print(
            f"  #{dispute_id}  {entity_key}.{field_name}  "
            f"({claim_count} claim(s), {chunk_range})  status={status}"
        )
    return 0


def cmd_show(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    table_name: str = "characters",
) -> int:
    row = conn.execute(
        f"""
        SELECT "dispute_id", "status", "canonical_value", "resolution_reason"
        FROM "{FACT_DISPUTES_TABLE}"
        WHERE "table_name" = ? AND "entity_key" = ? AND "field_name" = ?
        """,
        [table_name, entity_key, field_name],
    ).fetchone()
    if row is None:
        print(f"No dispute found for {entity_key}.{field_name}")
        return 1

    dispute_id, status, canonical_value, resolution_reason = row
    current = conn.execute(
        f'SELECT "{field_name}" FROM "{table_name}" WHERE "{_unique_key(table_name)}" = ?',
        [entity_key],
    ).fetchone()
    current_value = None if current is None else current[0]

    print(f"Dispute #{dispute_id}: {entity_key} / {field_name}  [{status}]")
    print(f"Current DB value: {current_value!r}")
    if canonical_value is not None or resolution_reason:
        print(f"Resolved to: {canonical_value!r}")
        print(f"Reason: {resolution_reason}")

    claims = conn.execute(
        f"""
        SELECT c."chunk_index", c."previous_value", c."incoming_value",
               sc."word_start", sc."word_end", sc."text"
        FROM "{FACT_CLAIMS_TABLE}" c
        LEFT JOIN "{SOURCE_CHUNKS_TABLE}" sc ON sc."chunk_index" = c."chunk_index"
        WHERE c."dispute_id" = ?
        ORDER BY c."chunk_index"
        """,
        [dispute_id],
    ).fetchall()

    for index, (chunk_index, previous_value, incoming_value, word_start, word_end, text) in enumerate(
        claims, start=1
    ):
        print(f"\nClaim {index} (chunk {chunk_index}", end="")
        if word_start is not None and word_end is not None:
            print(f", words {word_start}-{word_end}", end="")
        print("):")
        print(f"  was {previous_value!r} -> claimed {incoming_value!r}")
        print("  --- chunk text ---")
        chunk_text = text or "(source chunk not stored)"
        if len(chunk_text) > _CHUNK_PREVIEW_LIMIT:
            chunk_text = chunk_text[:_CHUNK_PREVIEW_LIMIT] + "\n... [truncated]"
        print(chunk_text)

    return 0


def cmd_export(conn: duckdb.DuckDBPyConnection, csv_path: Path, *, open_only: bool) -> int:
    if not table_exists(conn, FACT_DISPUTES_TABLE):
        print("No fact_disputes table in this database.")
        return 1

    status_clause = f'AND d."status" = \'{DISPUTE_STATUS_OPEN}\'' if open_only else ""
    rows = conn.execute(
        f"""
        SELECT d."dispute_id", d."entity_key", d."field_name", d."status",
               c."chunk_index", c."previous_value", c."incoming_value",
               sc."word_start", sc."word_end", sc."text"
        FROM "{FACT_DISPUTES_TABLE}" d
        JOIN "{FACT_CLAIMS_TABLE}" c ON c."dispute_id" = d."dispute_id"
        LEFT JOIN "{SOURCE_CHUNKS_TABLE}" sc ON sc."chunk_index" = c."chunk_index"
        WHERE 1=1 {status_clause}
        ORDER BY d."entity_key", d."field_name", c."chunk_index"
        """
    ).fetchall()

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "dispute_id",
                "entity_key",
                "field_name",
                "status",
                "chunk_index",
                "previous_value",
                "incoming_value",
                "word_start",
                "word_end",
                "chunk_text",
            ]
        )
        writer.writerows(rows)

    print(f"Exported {len(rows)} claim row(s) to {csv_path.resolve()}")
    return 0


def cmd_resolve(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    value: str | None,
    reason: str,
    table_name: str = "characters",
) -> int:
    canonical = None if value is None or value.lower() == "null" else value
    if not apply_manual_resolution(
        conn,
        entity_key=entity_key,
        field_name=field_name,
        canonical_value=canonical,
        reason=reason,
        table_name=table_name,
    ):
        print(f"No open dispute for {entity_key}.{field_name}")
        return 1

    if table_name == "characters" and field_name == "house":
        sync_house_roster_from_characters(conn, HOUSE_DATABASE_PLAN)

    print(f"Resolved {entity_key}.{field_name} -> {canonical!r}")
    return 0


def _unique_key(table_name: str) -> str:
    if table_name == "characters":
        return "character_name"
    if table_name == "spells":
        return "spell_name"
    if table_name == "houses":
        return "house_name"
    raise ValueError(f"Unknown table: {table_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and manually resolve fact disputes in a DuckDB extraction database."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("output/extracted.db"),
        help="Path to DuckDB file (default: output/extracted.db)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List disputes")
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="Include resolved disputes (default: open only)",
    )

    show_parser = subparsers.add_parser("show", help="Show one dispute with chunk text")
    show_parser.add_argument("--entity", required=True, help="Entity key, e.g. 'Harry Potter'")
    show_parser.add_argument("--field", required=True, help="Field name, e.g. house")
    show_parser.add_argument(
        "--table",
        default="characters",
        help="Table name (default: characters)",
    )

    export_parser = subparsers.add_parser("export", help="Export disputes to CSV")
    export_parser.add_argument(
        "--csv",
        type=Path,
        default=Path("output/disputes.csv"),
        help="Output CSV path (default: output/disputes.csv)",
    )
    export_parser.add_argument(
        "--all",
        action="store_true",
        help="Include resolved disputes (default: open only)",
    )

    resolve_parser = subparsers.add_parser("resolve", help="Manually resolve an open dispute")
    resolve_parser.add_argument("--entity", required=True)
    resolve_parser.add_argument("--field", required=True)
    resolve_parser.add_argument(
        "--value",
        required=True,
        help="Canonical value (use 'null' to clear the field)",
    )
    resolve_parser.add_argument(
        "--reason",
        required=True,
        help="Short note explaining your decision",
    )
    resolve_parser.add_argument(
        "--table",
        default="characters",
        help="Table name (default: characters)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        conn = _connect(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        if args.command == "list":
            return cmd_list(conn, open_only=not args.all)
        if args.command == "show":
            return cmd_show(
                conn,
                entity_key=args.entity,
                field_name=args.field,
                table_name=args.table,
            )
        if args.command == "export":
            return cmd_export(conn, args.csv, open_only=not args.all)
        if args.command == "resolve":
            return cmd_resolve(
                conn,
                entity_key=args.entity,
                field_name=args.field,
                value=args.value,
                reason=args.reason,
                table_name=args.table,
            )
    finally:
        conn.close()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
