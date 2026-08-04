"""Orchestrate PDF → chunks → extraction → validation → DuckDB."""

from __future__ import annotations

from pathlib import Path

from src.agents import DEFAULT_MAX_RETRIES, build_extraction_agent, extract_from_chunk
from src.chunker import split_text_into_chunks
from src.checkpoints import (
    get_completed_chunk_indices,
    load_entity_keys,
    make_run_key,
    record_chunk_checkpoint,
)
from src.config import Settings, get_settings
from src.database import (
    prepare_database,
    insert_point_event_rows,
    print_table_preview,
    rows_from_extraction,
    sync_house_roster_from_characters,
    upsert_rows,
)
from src.pdf_reader import read_pdf_text
from src.schemas import (
    CHARACTER_DATABASE_PLAN,
    HOUSE_DATABASE_PLAN,
    HOUSE_POINT_EVENT_DATABASE_PLAN,
    SPELL_DATABASE_PLAN,
    DatabasePlan,
)
from src.conflicts import (
    FACT_DISPUTES_TABLE,
    count_open_disputes,
    save_run_metadata,
    save_source_chunks,
)
from src.validation import ValidationSummary, validate_chunk_rows
from src.wrapup import reconcile_all_conflicts


async def run_extraction_pipeline(
    *,
    pdf_path: Path,
    user_request: str,
    db_path: Path | None = None,
    settings: Settings | None = None,
    resume: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    skip_wrap_up: bool = False,
) -> Path:
    """
    Full extraction pipeline.

    1. Read PDF text
    2. Print fixed table plans (characters, spells, houses, house_point_events)
    3. Split into overlapping word chunks
    4. Per chunk: extract → validate → upsert/insert → checkpoint
    5. Wrap-up agent reconciles logged fact conflicts (stronger model, once)

    With resume=True, skip chunks already recorded in pipeline_checkpoints.
    """
    settings = settings or get_settings()
    db_path = db_path or (settings.output_dir / "extracted.db")

    print(f"Reading PDF: {pdf_path}")
    text = read_pdf_text(pdf_path)
    word_count = len(text.split())
    print(f"Extracted {word_count:,} words from the PDF")

    extraction_agent = build_extraction_agent(settings)

    print("\nStep 1/3: Using character, spell, house, and point event schemas...")
    character_plan = CHARACTER_DATABASE_PLAN
    spell_plan = SPELL_DATABASE_PLAN
    house_plan = HOUSE_DATABASE_PLAN
    point_event_plan = HOUSE_POINT_EVENT_DATABASE_PLAN
    _print_plan(character_plan)
    _print_plan(spell_plan)
    _print_plan(house_plan)
    _print_plan(point_event_plan)

    print(f"Step 2/3: Splitting text into chunks...")
    chunks = split_text_into_chunks(
        text,
        chunk_size_words=settings.chunk_size_words,
        overlap_words=settings.chunk_overlap_words,
    )
    print(
        f"Created {len(chunks)} chunks "
        f"({settings.chunk_size_words} words each, "
        f"{settings.chunk_overlap_words} word overlap)"
    )

    print(f"Step 3/3: Extracting and validating chunk by chunk...")
    run_key = make_run_key(
        pdf_path,
        chunk_size_words=settings.chunk_size_words,
        chunk_overlap_words=settings.chunk_overlap_words,
    )
    conn = prepare_database(
        db_path,
        character_plan,
        spell_plan,
        house_plan,
        point_event_plan,
        resume=resume,
    )
    save_run_metadata(
        conn,
        run_key=run_key,
        pdf_path=pdf_path,
        chunk_size_words=settings.chunk_size_words,
        chunk_overlap_words=settings.chunk_overlap_words,
    )
    save_source_chunks(conn, chunks)
    completed_chunks = get_completed_chunk_indices(conn, run_key) if resume else set()
    if resume and completed_chunks:
        print(
            f"Resuming: {len(completed_chunks)} of {len(chunks)} chunks already checkpointed"
        )
    elif resume:
        print("Resuming: no completed chunks yet (starting from beginning)")
    character_columns = [column.name for column in character_plan.columns]
    spell_columns = [column.name for column in spell_plan.columns]
    house_columns = [column.name for column in house_plan.columns]
    point_event_columns = [
        column.name
        for column in point_event_plan.columns
        if column.name != "event_id"
    ]
    known_character_keys: list[str] = (
        load_entity_keys(conn, character_plan.table_name, character_plan.unique_key)
        if resume
        else []
    )
    known_spell_keys: list[str] = (
        load_entity_keys(conn, spell_plan.table_name, spell_plan.unique_key)
        if resume
        else []
    )
    skipped_chunks = 0
    failed_chunks: list[str] = []
    total_character_upserts = 0
    total_spell_upserts = 0
    total_house_upserts = 0
    total_point_event_inserts = 0
    validation_summary = ValidationSummary()

    try:
        for chunk in chunks:
            if chunk.index in completed_chunks:
                skipped_chunks += 1
                print(f"  Skipping {chunk.label} (already checkpointed)")
                continue

            print(f"  Processing {chunk.label}...")
            try:
                extraction = await extract_from_chunk(
                    extraction_agent,
                    user_request=user_request,
                    character_plan=character_plan,
                    spell_plan=spell_plan,
                    house_plan=house_plan,
                    point_event_plan=point_event_plan,
                    chunk_text=chunk.text,
                    chunk_label=chunk.label,
                    known_character_keys=known_character_keys,
                    known_spell_keys=known_spell_keys,
                    max_retries=max_retries,
                )
            except Exception as error:
                # One poisoned chunk must not kill the remaining chunks. It is
                # not checkpointed, so a later --resume run will retry it.
                failed_chunks.append(chunk.label)
                print(
                    f"    FAILED {chunk.label} after {max_retries} attempt(s): "
                    f"{type(error).__name__}. Skipping; rerun with --resume to retry."
                )
                continue

            raw_character_rows = rows_from_extraction(extraction.rows, character_columns)
            raw_spell_rows = rows_from_extraction(extraction.spell_rows, spell_columns)
            raw_house_rows = rows_from_extraction(extraction.house_rows, house_columns)
            raw_point_event_rows = rows_from_extraction(
                extraction.point_event_rows, point_event_columns
            )
            (
                character_rows,
                spell_rows,
                house_rows,
                point_event_rows,
                drop_messages,
            ) = validate_chunk_rows(
                raw_character_rows,
                raw_spell_rows,
                raw_house_rows,
                raw_point_event_rows,
                chunk_text=chunk.text,
                chunk_label=chunk.label,
            )
            validation_summary.record_chunk(
                characters_in=len(raw_character_rows),
                characters_out=len(character_rows),
                spells_in=len(raw_spell_rows),
                spells_out=len(spell_rows),
                houses_in=len(raw_house_rows),
                houses_out=len(house_rows),
                point_events_in=len(raw_point_event_rows),
                point_events_out=len(point_event_rows),
                messages=drop_messages,
            )

            char_inserted = upsert_rows(
                conn, character_plan, character_rows, chunk_index=chunk.index
            )
            spell_inserted = upsert_rows(
                conn, spell_plan, spell_rows, chunk_index=chunk.index
            )
            house_inserted = upsert_rows(
                conn, house_plan, house_rows, chunk_index=chunk.index
            )
            point_event_inserted = insert_point_event_rows(
                conn, point_event_plan.table_name, point_event_rows
            )
            sync_house_roster_from_characters(conn, house_plan)
            total_character_upserts += char_inserted
            total_spell_upserts += spell_inserted
            total_house_upserts += house_inserted
            total_point_event_inserts += point_event_inserted

            for row in character_rows:
                key_value = row.get(character_plan.unique_key)
                if key_value and key_value not in known_character_keys:
                    known_character_keys.append(str(key_value))

            for row in spell_rows:
                key_value = row.get(spell_plan.unique_key)
                if key_value and key_value not in known_spell_keys:
                    known_spell_keys.append(str(key_value))

            record_chunk_checkpoint(conn, run_key=run_key, chunk_index=chunk.index)

            dropped = (
                len(raw_character_rows)
                - len(character_rows)
                + (len(raw_spell_rows) - len(spell_rows))
                + (len(raw_house_rows) - len(house_rows))
                + (len(raw_point_event_rows) - len(point_event_rows))
            )
            if drop_messages:
                print(f"    Validation: dropped {dropped} row(s)")
                for message in drop_messages[:3]:
                    print(f"      - {message}")
                if len(drop_messages) > 3:
                    print(f"      - ... and {len(drop_messages) - 3} more")

            if extraction.notes:
                print(f"    Notes: {extraction.notes}")
            print(
                f"    Characters: {len(character_rows)} (upserted: {char_inserted}) | "
                f"Spells: {len(spell_rows)} (upserted: {spell_inserted}) | "
                f"Houses: {len(house_rows)} (upserted: {house_inserted}) | "
                f"Point events: {len(point_event_rows)} (inserted: {point_event_inserted})"
            )

        if skipped_chunks:
            print(f"\nSkipped {skipped_chunks} already-checkpointed chunk(s)")
        if failed_chunks:
            print(
                f"\nWARNING: {len(failed_chunks)} chunk(s) failed and were skipped "
                f"(rerun with --resume to retry them):"
            )
            for label in failed_chunks:
                print(f"  - {label}")

        if skip_wrap_up:
            print("\nWrap-up skipped (--skip-wrap-up)")
            open_count = count_open_disputes(conn)
            if open_count:
                print(
                    f"  {open_count} open dispute(s) — review with: "
                    f"python -m src.inspect --db {db_path} list"
                )
        elif settings.openrouter_wrapup_model:
            print("\nStep 4/4: Reconciling fact conflicts...")
            await reconcile_all_conflicts(conn, settings)
            sync_house_roster_from_characters(conn, house_plan)
        else:
            print(
                "\nWrap-up skipped: set OPENROUTER_WRAPUP_MODEL in .env to enable "
                "conflict reconciliation"
            )

        print(f"\nDone. Character upserts: {total_character_upserts}")
        print(f"Done. Spell upserts: {total_spell_upserts}")
        print(f"Done. House upserts: {total_house_upserts}")
        print(f"Done. Point event inserts: {total_point_event_inserts}")
        print(f"Database saved to: {db_path.resolve()}")
        validation_summary.print_report()
        print_table_preview(conn, character_plan.table_name)
        print_table_preview(conn, spell_plan.table_name)
        print_table_preview(conn, house_plan.table_name)
        print_table_preview(conn, point_event_plan.table_name)
        from src.checkpoints import table_exists

        if table_exists(conn, FACT_DISPUTES_TABLE):
            print_table_preview(conn, FACT_DISPUTES_TABLE, limit=5)
    finally:
        conn.close()

    return db_path


def _print_plan(plan: DatabasePlan) -> None:
    print(f"  Table: {plan.table_name}")
    print(f"  Unique key: {plan.unique_key}")
    print(f"  Reasoning: {plan.reasoning}")
    for column in plan.columns:
        print(f"    - {column.name} ({column.duckdb_type}): {column.description}")
    print()
