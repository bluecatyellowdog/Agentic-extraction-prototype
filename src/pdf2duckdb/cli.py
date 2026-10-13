"""Command-line interface.

pdf2duckdb extract --pdf book.pdf --profile novel --output output/book.db
pdf2duckdb disputes --db output/book.db list
pdf2duckdb profiles
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

from pdf2duckdb.agents import DEFAULT_MAX_RETRIES
from pdf2duckdb.disputes_cli import add_disputes_parser, run_disputes_command
from pdf2duckdb.profile import builtin_profile_names, load_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2duckdb",
        description="Extract structured data from long PDFs into DuckDB using an LLM.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Run the extraction pipeline on a PDF")
    extract.add_argument("--pdf", type=Path, required=True, help="Input PDF file")
    extract.add_argument(
        "--profile",
        default="novel",
        help="Built-in profile name or path to a profile .yaml (default: novel)",
    )
    extract.add_argument(
        "--request",
        help="Optional extra guidance for the extractor, added to every chunk prompt",
    )
    extract.add_argument(
        "--output",
        type=Path,
        default=Path("output/extracted.db"),
        help="DuckDB output file (default: output/extracted.db)",
    )
    extract.add_argument("--chunk-size", type=int, default=3_000, help="Words per chunk")
    extract.add_argument(
        "--chunk-overlap", type=int, default=400, help="Overlapping words between chunks"
    )
    extract.add_argument(
        "--resume",
        action="store_true",
        help="Continue a previous run: keep the database and skip checkpointed chunks",
    )
    extract.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"API attempts per chunk on transient errors (default: {DEFAULT_MAX_RETRIES})",
    )
    extract.add_argument(
        "--skip-wrap-up",
        action="store_true",
        help="Leave fact disputes open instead of reconciling them with the wrap-up model",
    )

    add_disputes_parser(subparsers)
    subparsers.add_parser("profiles", help="List built-in extraction profiles")
    return parser


def _run_extract(args: argparse.Namespace) -> int:
    from pdf2duckdb.config import get_settings
    from pdf2duckdb.pipeline import run_extraction_pipeline

    settings = replace(
        get_settings(),
        chunk_size_words=args.chunk_size,
        chunk_overlap_words=args.chunk_overlap,
    )
    result = asyncio.run(
        run_extraction_pipeline(
            pdf_path=args.pdf,
            profile=load_profile(args.profile),
            user_request=args.request,
            db_path=args.output,
            settings=settings,
            resume=args.resume,
            max_retries=args.max_retries,
            skip_wrap_up=args.skip_wrap_up,
        )
    )

    print(f"\nDatabase: {result.db_path.resolve()}")
    for table_name, count in result.table_row_counts.items():
        print(f"  {table_name}: {count} row(s)")
    if result.open_disputes:
        print(
            f"\n{result.open_disputes} open dispute(s). Review with:\n"
            f"  pdf2duckdb disputes --db {result.db_path} list"
        )
    return 1 if result.failed_chunks else 0


def _run_profiles() -> int:
    for name in builtin_profile_names():
        profile = load_profile(name)
        tables = ", ".join(table.name for table in profile.tables)
        print(f"{name}: {profile.description}\n  tables: {tables}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    # Third-party HTTP clients are noisy at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        if args.command == "extract":
            code = _run_extract(args)
        elif args.command == "disputes":
            code = run_disputes_command(args)
        else:
            code = _run_profiles()
    except (ValueError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
