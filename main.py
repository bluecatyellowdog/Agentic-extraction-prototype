"""
CLI entry point for the PDF extraction pipeline.

Example:
  python main.py --pdf data/harrypotter-sample.pdf ^
    --request "Extract human characters, named spells spoken in the text, Hogwarts house facts, and house point events" ^
    --output output/extracted.db
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from src.pipeline import run_extraction_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract structured data from a PDF into DuckDB using an LLM agent."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        required=True,
        help="Path to the input PDF file",
    )
    parser.add_argument(
        "--request",
        type=str,
        required=True,
        help=(
            'Plain-English extraction goal, e.g. '
            '"Extract human characters, named spells, Hogwarts house facts, and house point events"'
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/extracted.db"),
        help="Path for the DuckDB file (default: output/extracted.db)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=3_000,
        help="Words per chunk (default: 3000)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=400,
        help="Overlapping words between chunks (default: 400)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue a previous run: keep the DB and skip checkpointed chunks",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="API retry attempts per chunk on transient errors (default: 3)",
    )
    parser.add_argument(
        "--skip-wrap-up",
        action="store_true",
        help="Skip the final wrap-up agent that reconciles fact conflicts",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    from src.config import get_settings

    settings = replace(
        get_settings(),
        chunk_size_words=args.chunk_size,
        chunk_overlap_words=args.chunk_overlap,
    )

    await run_extraction_pipeline(
        pdf_path=args.pdf,
        user_request=args.request,
        db_path=args.output,
        settings=settings,
        resume=args.resume,
        max_retries=args.max_retries,
        skip_wrap_up=args.skip_wrap_up,
    )


if __name__ == "__main__":
    asyncio.run(main())
