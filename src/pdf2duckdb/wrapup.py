"""Wrap-up agent: reconcile logged fact disputes using a stronger model."""

from __future__ import annotations

import logging

import duckdb
from pydantic_ai import Agent, ModelSettings

from pdf2duckdb.config import Settings
from pdf2duckdb.disputes import (
    ClaimRecord,
    ConflictGroup,
    apply_field_resolution,
    count_open_claims,
    count_open_disputes,
    fetch_unresolved_conflict_groups,
)
from pdf2duckdb.schemas import WrapUpResult

logger = logging.getLogger(__name__)

WRAPUP_BATCH_SIZE = 30
_WRAPUP_CHUNK_TEXT_LIMIT = 1500


def build_wrap_up_agent(settings: Settings) -> Agent[None, WrapUpResult]:
    """Build the reconciliation agent (uses OPENROUTER_WRAPUP_MODEL)."""
    if not settings.openrouter_wrapup_model:
        raise ValueError("OPENROUTER_WRAPUP_MODEL is required for wrap-up")

    return Agent(
        f"openrouter:{settings.openrouter_wrapup_model}",
        output_type=WrapUpResult,
        model_settings=ModelSettings(temperature=settings.openrouter_wrapup_temperature),
        system_prompt=(
            "You reconcile contradictory facts extracted from one document "
            "across many text chunks.\n\n"
            "For each disputed field you receive:\n"
            "- table_name, entity_key, field_name\n"
            "- current_value (what the database holds after all merges)\n"
            "- a timeline of claims ordered by chunk_index (higher = later in the document)\n"
            "- the source chunk text for each claim\n\n"
            "Rules:\n"
            "- Prefer later chunk_index when the text supports an update, reveal or correction.\n"
            "- Prefer earlier well-supported facts when later claims look like extraction errors.\n"
            "- Use null for canonical_value only if no claim is credible.\n"
            "- Return exactly one resolution per dispute listed in the prompt.\n"
            "- reason: one short sentence citing chunk indices.\n"
            "- Base decisions on the chunk text provided, not outside knowledge."
        ),
    )


def _truncate_chunk_text(text: str, limit: int = _WRAPUP_CHUNK_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _format_claim(claim: ClaimRecord) -> list[str]:
    lines = [
        f"  chunk {claim.chunk_index}: "
        f"was {claim.previous_value!r} -> claimed {claim.incoming_value!r}"
    ]
    if claim.word_start is not None and claim.word_end is not None:
        lines.append(f"    (words {claim.word_start}-{claim.word_end})")
    if claim.chunk_text:
        lines.append("    source text:")
        for paragraph in _truncate_chunk_text(claim.chunk_text).splitlines():
            lines.append(f"      {paragraph}")
    else:
        lines.append("    source text: (not stored)")
    return lines


def _format_conflict_groups(groups: list[ConflictGroup]) -> str:
    parts: list[str] = []
    for group in groups:
        parts.append(
            f"DISPUTE: table={group.table_name} entity={group.entity_key!r} "
            f"field={group.field_name} current_db_value={group.current_value!r}"
        )
        for claim in group.claims:
            parts.extend(_format_claim(claim))
        parts.append("")
    return "\n".join(parts)


async def run_wrap_up_batch(
    wrap_up_agent: Agent[None, WrapUpResult],
    groups: list[ConflictGroup],
) -> WrapUpResult:
    prompt = (
        "Resolve every dispute below. Match table_name, entity_key, and field_name exactly.\n\n"
        f"{_format_conflict_groups(groups)}"
    )
    result = await wrap_up_agent.run(prompt)
    return result.output


async def reconcile_all_conflicts(
    conn: duckdb.DuckDBPyConnection,
    settings: Settings,
) -> int:
    """Run wrap-up agent on all open disputes. Returns fields resolved."""
    dispute_count = count_open_disputes(conn)
    if dispute_count == 0:
        logger.info("Wrap-up: no open fact disputes")
        return 0

    claim_count = count_open_claims(conn)
    groups = fetch_unresolved_conflict_groups(conn)
    logger.info(
        "Wrap-up: reconciling %d open dispute(s) (%d claim(s)) using %s",
        dispute_count,
        claim_count,
        settings.openrouter_wrapup_model,
    )

    wrap_up_agent = build_wrap_up_agent(settings)
    applied = 0

    for start in range(0, len(groups), WRAPUP_BATCH_SIZE):
        batch = groups[start : start + WRAPUP_BATCH_SIZE]
        batch_keys = {(g.table_name, g.entity_key, g.field_name) for g in batch}
        result = await run_wrap_up_batch(wrap_up_agent, batch)

        for resolution in result.resolutions:
            key = (
                resolution.table_name,
                resolution.entity_key,
                resolution.field_name,
            )
            if key not in batch_keys:
                continue
            apply_field_resolution(
                conn,
                table_name=resolution.table_name,
                entity_key=resolution.entity_key,
                field_name=resolution.field_name,
                canonical_value=resolution.canonical_value,
                reason=resolution.reason,
            )
            applied += 1
            logger.info(
                "  Resolved %s.%s -> %r",
                resolution.entity_key,
                resolution.field_name,
                resolution.canonical_value,
            )

    remaining = count_open_disputes(conn)
    logger.info("Wrap-up done: %d dispute(s) resolved, %d still open", applied, remaining)
    return applied
