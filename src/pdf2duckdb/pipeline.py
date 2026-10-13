"""Orchestrate PDF -> chunks -> extraction -> validation -> DuckDB."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.models import Model

from pdf2duckdb.agents import (
    DEFAULT_MAX_RETRIES,
    build_chunk_prompt,
    build_extraction_agent,
    extract_from_chunk,
)
from pdf2duckdb.checkpoints import (
    get_completed_chunk_indices,
    load_entity_keys,
    make_run_key,
    record_chunk_checkpoint,
)
from pdf2duckdb.chunker import split_text_into_chunks
from pdf2duckdb.config import Settings, get_settings
from pdf2duckdb.database import prepare_database, write_rows
from pdf2duckdb.dbutil import count_rows
from pdf2duckdb.disputes import count_open_disputes, save_run_metadata, save_source_chunks
from pdf2duckdb.pdf_reader import read_pdf_text
from pdf2duckdb.profile import ExtractionProfile, load_profile
from pdf2duckdb.schemas import NOTES_FIELD, rows_by_table
from pdf2duckdb.validation import ValidationSummary, validate_chunk_rows
from pdf2duckdb.wrapup import reconcile_all_conflicts

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    db_path: Path
    chunks_total: int
    chunks_processed: int = 0
    chunks_skipped: int = 0
    failed_chunks: list[str] = field(default_factory=list)
    table_row_counts: dict[str, int] = field(default_factory=dict)
    open_disputes: int = 0


async def run_extraction_pipeline(
    *,
    pdf_path: Path,
    profile: ExtractionProfile | str = "novel",
    user_request: str | None = None,
    db_path: Path | None = None,
    settings: Settings | None = None,
    resume: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    skip_wrap_up: bool = False,
    model: Model | None = None,
) -> PipelineResult:
    """
    Full extraction pipeline.

    1. Read PDF text and split it into overlapping word chunks
    2. Per chunk: extract -> validate -> upsert/append -> checkpoint
    3. Optionally reconcile logged fact disputes with a stronger model

    With resume=True, chunks already recorded in pipeline_checkpoints are skipped.
    `model` overrides the OpenRouter model (used by tests and custom providers).
    """
    settings = settings or get_settings()
    if not isinstance(profile, ExtractionProfile):
        profile = load_profile(profile)
    db_path = db_path or (settings.output_dir / "extracted.db")

    logger.info("Reading PDF: %s", pdf_path)
    text = read_pdf_text(pdf_path)
    chunks = split_text_into_chunks(
        text,
        chunk_size_words=settings.chunk_size_words,
        overlap_words=settings.chunk_overlap_words,
    )
    logger.info(
        "Profile '%s': %d table(s). %s words -> %d chunk(s) of %d words (%d overlap)",
        profile.name,
        len(profile.tables),
        f"{len(text.split()):,}",
        len(chunks),
        settings.chunk_size_words,
        settings.chunk_overlap_words,
    )

    extraction_agent = build_extraction_agent(settings, profile, model=model)
    run_key = make_run_key(
        pdf_path,
        profile_name=profile.name,
        chunk_size_words=settings.chunk_size_words,
        chunk_overlap_words=settings.chunk_overlap_words,
    )
    conn = prepare_database(db_path, profile, resume=resume)
    result = PipelineResult(db_path=db_path, chunks_total=len(chunks))
    try:
        save_run_metadata(
            conn,
            run_key=run_key,
            pdf_path=pdf_path,
            profile_name=profile.name,
            chunk_size_words=settings.chunk_size_words,
            chunk_overlap_words=settings.chunk_overlap_words,
        )
        save_source_chunks(conn, chunks)
        completed = get_completed_chunk_indices(conn, run_key, profile) if resume else set()
        if resume:
            logger.info("Resuming: %d of %d chunk(s) already done", len(completed), len(chunks))

        known_keys = {
            table.name: load_entity_keys(conn, table.name, table.key)
            for table in profile.tables
            if table.key
        }
        validation_summary = ValidationSummary()

        for chunk in chunks:
            if chunk.index in completed:
                result.chunks_skipped += 1
                continue

            logger.info("Processing %s", chunk.label)
            prompt = build_chunk_prompt(
                chunk_text=chunk.text,
                chunk_label=chunk.label,
                known_keys=known_keys,
                user_request=user_request,
            )
            try:
                extraction = await extract_from_chunk(
                    extraction_agent,
                    prompt=prompt,
                    chunk_label=chunk.label,
                    max_retries=max_retries,
                )
            except Exception as error:
                # One poisoned chunk must not kill the run. It is not checkpointed,
                # so a later --resume run will retry it.
                result.failed_chunks.append(chunk.label)
                logger.warning(
                    "    FAILED %s: %s. Skipping; rerun with --resume to retry.",
                    chunk.label,
                    type(error).__name__,
                )
                continue

            valid_rows, drop_messages = validate_chunk_rows(
                profile,
                rows_by_table(extraction, profile),
                chunk_text=chunk.text,
                chunk_label=chunk.label,
                summary=validation_summary,
            )
            written: list[str] = []
            for table in profile.tables:
                rows = valid_rows[table.name]
                count = write_rows(conn, table, rows, chunk_index=chunk.index)
                written.append(f"{table.name}={count}")
                if table.key:
                    keys = known_keys[table.name]
                    for row in rows:
                        key_value = str(row[table.key])
                        if key_value not in keys:
                            keys.append(key_value)

            record_chunk_checkpoint(conn, run_key=run_key, chunk_index=chunk.index)
            result.chunks_processed += 1

            logger.info("    wrote %s", ", ".join(written))
            if drop_messages:
                logger.info("    dropped %d invalid row(s)", len(drop_messages))
                for message in drop_messages[:3]:
                    logger.debug("      - %s", message)
            notes = getattr(extraction, NOTES_FIELD, "")
            if notes:
                logger.info("    notes: %s", notes)

        if result.failed_chunks:
            logger.warning(
                "%d chunk(s) failed (rerun with --resume to retry): %s",
                len(result.failed_chunks),
                ", ".join(result.failed_chunks),
            )

        if skip_wrap_up:
            logger.info("Dispute wrap-up skipped (--skip-wrap-up)")
        elif settings.openrouter_wrapup_model:
            await reconcile_all_conflicts(conn, settings)
        elif count_open_disputes(conn):
            logger.info("Dispute wrap-up skipped: set OPENROUTER_WRAPUP_MODEL to enable it")

        validation_summary.log_report()
        result.table_row_counts = {t.name: count_rows(conn, t.name) for t in profile.tables}
        result.open_disputes = count_open_disputes(conn)
    finally:
        conn.close()

    return result
