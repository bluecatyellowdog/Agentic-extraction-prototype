"""`pdf2duckdb disputes ...`: inspect, export and manually resolve fact disputes."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import duckdb

from pdf2duckdb.dbutil import primary_key_column, table_exists
from pdf2duckdb.disputes import (
    DISPUTE_STATUS_OPEN,
    FACT_CLAIMS_TABLE,
    FACT_DISPUTES_TABLE,
    SOURCE_CHUNKS_TABLE,
    apply_manual_resolution,
    find_dispute_table,
)

_CHUNK_PREVIEW_LIMIT = 2000


def cmd_list(conn: duckdb.DuckDBPyConnection, *, open_only: bool) -> int:
    status_filter = f"WHERE d.\"status\" = '{DISPUTE_STATUS_OPEN}'" if open_only else ""
    rows = conn.execute(
        f"""
        SELECT d."dispute_id", d."table_name", d."entity_key", d."field_name", d."status",
               COUNT(c."claim_id") AS claim_count,
               MIN(c."chunk_index") AS first_chunk,
               MAX(c."chunk_index") AS last_chunk
        FROM "{FACT_DISPUTES_TABLE}" d
        LEFT JOIN "{FACT_CLAIMS_TABLE}" c ON c."dispute_id" = d."dispute_id"
        {status_filter}
        GROUP BY ALL
        ORDER BY d."table_name", d."entity_key", d."field_name"
        """
    ).fetchall()

    label = "open" if open_only else "all"
    if not rows:
        print(f"No {label} disputes.")
        return 0

    print(f"Disputes ({label}): {len(rows)}\n")
    for dispute_id, table, entity, field, status, claims, first_chunk, last_chunk in rows:
        chunk_range = (
            f"chunk {first_chunk}"
            if first_chunk == last_chunk
            else f"chunks {first_chunk}-{last_chunk}"
        )
        print(
            f"  #{dispute_id}  {table}: {entity}.{field}  "
            f"({claims} claim(s), {chunk_range})  status={status}"
        )
    return 0


def cmd_show(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_key: str,
    field_name: str,
    table_name: str | None,
) -> int:
    table_name = find_dispute_table(
        conn, entity_key=entity_key, field_name=field_name, table_name=table_name
    )
    if table_name is None:
        print(f"No dispute found for {entity_key}.{field_name}")
        return 1

    dispute_id, status, canonical_value, resolution_reason = conn.execute(
        f"""
        SELECT "dispute_id", "status", "canonical_value", "resolution_reason"
        FROM "{FACT_DISPUTES_TABLE}"
        WHERE "table_name" = ? AND "entity_key" = ? AND "field_name" = ?
        """,
        [table_name, entity_key, field_name],
    ).fetchone()
    current = conn.execute(
        f'SELECT "{field_name}" FROM "{table_name}" '
        f'WHERE "{primary_key_column(conn, table_name)}" = ?',
        [entity_key],
    ).fetchone()

    print(f"Dispute #{dispute_id}: {table_name}: {entity_key}.{field_name}  [{status}]")
    print(f"Current DB value: {None if current is None else current[0]!r}")
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

    for number, (chunk_index, previous, incoming, word_start, word_end, text) in enumerate(
        claims, start=1
    ):
        words = f", words {word_start}-{word_end}" if word_start is not None else ""
        print(f"\nClaim {number} (chunk {chunk_index}{words}):")
        print(f"  was {previous!r} -> claimed {incoming!r}")
        print("  --- chunk text ---")
        chunk_text = text or "(source chunk not stored)"
        if len(chunk_text) > _CHUNK_PREVIEW_LIMIT:
            chunk_text = chunk_text[:_CHUNK_PREVIEW_LIMIT] + "\n... [truncated]"
        print(chunk_text)
    return 0


def cmd_export(conn: duckdb.DuckDBPyConnection, csv_path: Path, *, open_only: bool) -> int:
    status_clause = f"AND d.\"status\" = '{DISPUTE_STATUS_OPEN}'" if open_only else ""
    rows = conn.execute(
        f"""
        SELECT d."dispute_id", d."table_name", d."entity_key", d."field_name", d."status",
               c."chunk_index", c."previous_value", c."incoming_value",
               sc."word_start", sc."word_end", sc."text"
        FROM "{FACT_DISPUTES_TABLE}" d
        JOIN "{FACT_CLAIMS_TABLE}" c ON c."dispute_id" = d."dispute_id"
        LEFT JOIN "{SOURCE_CHUNKS_TABLE}" sc ON sc."chunk_index" = c."chunk_index"
        WHERE 1=1 {status_clause}
        ORDER BY d."table_name", d."entity_key", d."field_name", c."chunk_index"
        """
    ).fetchall()

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "dispute_id",
                "table_name",
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
    value: str,
    reason: str,
    table_name: str | None,
) -> int:
    canonical = None if value.lower() == "null" else value
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
    print(f"Resolved {entity_key}.{field_name} -> {canonical!r}")
    return 0


def add_disputes_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "disputes", help="Inspect and manually resolve fact disputes in an output database"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("output/extracted.db"),
        help="DuckDB file (default: output/extracted.db)",
    )
    commands = parser.add_subparsers(dest="disputes_command", required=True)

    list_parser = commands.add_parser("list", help="List disputes")
    list_parser.add_argument("--all", action="store_true", help="Include resolved disputes")

    show_parser = commands.add_parser("show", help="Show one dispute with its source text")
    show_parser.add_argument("--entity", required=True, help="Entity key, e.g. 'Irene Adler'")
    show_parser.add_argument("--field", required=True, help="Field name, e.g. occupation")
    show_parser.add_argument("--table", help="Table name (only needed if ambiguous)")

    export_parser = commands.add_parser("export", help="Export disputes and claims to CSV")
    export_parser.add_argument("--csv", type=Path, default=Path("output/disputes.csv"))
    export_parser.add_argument("--all", action="store_true", help="Include resolved disputes")

    resolve_parser = commands.add_parser("resolve", help="Manually resolve an open dispute")
    resolve_parser.add_argument("--entity", required=True)
    resolve_parser.add_argument("--field", required=True)
    resolve_parser.add_argument("--value", required=True, help="Value to keep ('null' clears)")
    resolve_parser.add_argument("--reason", required=True, help="Short note on your decision")
    resolve_parser.add_argument("--table", help="Table name (only needed if ambiguous)")


def run_disputes_command(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    with duckdb.connect(str(args.db)) as conn:
        if not table_exists(conn, FACT_DISPUTES_TABLE):
            print(f"No {FACT_DISPUTES_TABLE} table in {args.db}", file=sys.stderr)
            return 1
        try:
            if args.disputes_command == "list":
                return cmd_list(conn, open_only=not args.all)
            if args.disputes_command == "show":
                return cmd_show(
                    conn, entity_key=args.entity, field_name=args.field, table_name=args.table
                )
            if args.disputes_command == "export":
                return cmd_export(conn, args.csv, open_only=not args.all)
            if args.disputes_command == "resolve":
                return cmd_resolve(
                    conn,
                    entity_key=args.entity,
                    field_name=args.field,
                    value=args.value,
                    reason=args.reason,
                    table_name=args.table,
                )
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    return 1
