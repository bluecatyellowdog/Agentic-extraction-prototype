"""Extraction agent: structured output via Pydantic AI (no tools)."""

from __future__ import annotations

import asyncio

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import ValidationError
from pydantic_ai import Agent, ModelSettings
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    ToolRetryError,
    UnexpectedModelBehavior,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from src.config import Settings
from src.schemas import ChunkExtractionResult, DatabasePlan

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 5.0
_MAX_RETRY_DELAY_SECONDS = 60.0
# Hard cap per HTTP request. Without this the OpenAI client can hang on a dead
# OpenRouter connection for 10+ minutes (observed on chunk 78).
_REQUEST_TIMEOUT_SECONDS = 180.0
# Total deadline per attempt. OpenRouter sends keep-alive bytes on slow
# non-streaming requests, so HTTP read timeouts never fire when the model is
# stuck generating; this wall-clock cap is the only reliable cutoff.
_ATTEMPT_DEADLINE_SECONDS = 240.0
# Cap output so runaway generation fails fast instead of running for minutes.
_MAX_OUTPUT_TOKENS = 8192


def _retry_delay_seconds(attempt: int, base_delay: float) -> float:
    return min(base_delay * (2**attempt), _MAX_RETRY_DELAY_SECONDS)


def _is_retryable_api_error(error: BaseException) -> bool:
    if isinstance(error, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    # asyncio.wait_for deadline on a hung attempt (includes asyncio.TimeoutError).
    if isinstance(error, TimeoutError):
        return True
    # ModelHTTPError subclasses ModelAPIError, so check it first: hard client
    # errors (401 bad key, 402 credits, 404 unknown model) must not be retried.
    if isinstance(error, ModelHTTPError):
        return error.status_code in {400, 408, 429, 500, 502, 503, 504}
    if isinstance(error, (ValidationError, ToolRetryError, ModelAPIError)):
        return True
    if isinstance(error, UnexpectedModelBehavior):
        return True
    return False


def build_extraction_agent(settings: Settings) -> Agent[None, ChunkExtractionResult]:
    """
    Build the single extraction agent.

    Returns one ChunkExtractionResult per chunk (characters, spells, houses,
    point events). Database writes happen in pipeline.py after validation.
    """
    # Explicit client so the request timeout is actually enforced; internal
    # SDK retries are disabled because extract_from_chunk retries with backoff.
    openai_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    model = OpenAIChatModel(
        settings.openrouter_model,
        provider=OpenRouterProvider(openai_client=openai_client),
    )
    return Agent(
        model,
        output_type=ChunkExtractionResult,
        retries=DEFAULT_MAX_RETRIES,
        model_settings=ModelSettings(
            temperature=settings.openrouter_temperature,
            max_tokens=_MAX_OUTPUT_TOKENS,
        ),
        system_prompt=(
            "You extract human characters, named spells, and Hogwarts house facts "
            "from narrative text.\n\n"
            "CHARACTERS (return in 'rows'):\n"
            "- Include every human character mentioned in this chunk (speaking, named, "
            "or clearly referenced). One row per character.\n"
            "- ONLY include humans (wizards, witches, muggles). Exclude non-human "
            "beings: house-elves, goblins, ghosts, portraits, centaurs, giants, "
            "trolls, animals, and other magical creatures.\n"
            "- blood_status: use exactly muggle, half_blood, or wizard when the text "
            "supports it; otherwise null.\n"
            "- house: Gryffindor, Hufflepuff, Ravenclaw, or Slytherin when stated; "
            "otherwise null.\n"
            "- role_type: use exactly student or faculty when the text supports it. "
            "student = Hogwarts pupil; faculty = Hogwarts teacher or staff. "
            "Otherwise null (e.g. muggles, Ministry workers, shopkeepers).\n"
            "- classes_attended: ONLY for students — comma-separated class or subject "
            "names they attend or are seen in this chunk. Null for faculty or if no "
            "classes appear.\n"
            "- occupation: job or role if mentioned. Otherwise null.\n"
            "- character_description: a short prose description (1–2 sentences). "
            "Otherwise null.\n"
            "- Do NOT put spells or house lore on character rows.\n\n"
            "SPELLS (return in 'spell_rows'):\n"
            "- ONLY include spells where the incantation is explicitly spoken, "
            "shouted, or quoted in the text.\n"
            "- spell_name: exact incantation words, title case, no exclamation marks.\n"
            "- effect_description: brief description from this chunk; else null.\n"
            "- used_by: full name(s) who speak or cast it (comma-separated).\n"
            "- One row per distinct incantation in the chunk.\n\n"
            "HOUSES (return in 'house_rows'):\n"
            "- One row per Hogwarts house mentioned or relevant in this chunk "
            "(Gryffindor, Hufflepuff, Ravenclaw, Slytherin).\n"
            "- current_students: comma-separated full names of students currently in "
            "this house per the text.\n"
            "- prior_students: comma-separated full names of alumni or former students "
            "of this house mentioned in the text.\n"
            "- others_not_in_house: comma-separated full names of humans explicitly "
            "NOT in this house (sorted elsewhere, rejected by the hat, never attended, "
            "or clearly belonging to another house).\n"
            "- lore_facts: semicolon-separated house-specific facts from this chunk "
            "only — traits, values, common room password, how it looks or is "
            "decorated, entrance location, founder notes, etc. Do NOT include "
            "house point awards or deductions here.\n"
            "- Return partial house rows when only some fields apply.\n\n"
            "HOUSE POINT EVENTS (return in 'point_event_rows'):\n"
            "- One row per explicit house point award or deduction in this chunk.\n"
            "- house_name: Gryffindor, Hufflepuff, Ravenclaw, or Slytherin.\n"
            "- points: positive integer when awarded, negative when deducted "
            "(e.g. 10 or -5). Never zero.\n"
            "- caused_by_student: full name of the student who caused the change, "
            "if the text says who; otherwise null.\n"
            "- reason: brief explanation from the text of why points changed.\n"
            "- Do NOT put point events in house_rows or lore_facts.\n\n"
            "GENERAL:\n"
            "- Use null for any field not supported by this chunk. Do not guess.\n"
            "- Do not invent characters, spells, or facts.\n"
            "- Merge duplicates within the same chunk into one row.\n"
            "- Skip entities already listed under 'already captured' unless this "
            "chunk adds new facts (return partial rows with only new fields).\n"
            "- Return empty lists if nothing relevant appears in this chunk."
        ),
    )


async def extract_from_chunk(
    extraction_agent: Agent[None, ChunkExtractionResult],
    *,
    user_request: str,
    character_plan: DatabasePlan,
    spell_plan: DatabasePlan,
    house_plan: DatabasePlan,
    point_event_plan: DatabasePlan,
    chunk_text: str,
    chunk_label: str,
    known_character_keys: list[str],
    known_spell_keys: list[str],
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
) -> ChunkExtractionResult:
    """
    Run extraction on one chunk.

    The user prompt includes column descriptions from each DatabasePlan, the CLI
    --request string, known entity keys, and the chunk text. Retries transient API
    errors with exponential backoff before giving up on a chunk.
    """
    character_columns = "\n".join(
        f"- {col.name} ({col.duckdb_type}): {col.description}"
        for col in character_plan.columns
    )
    spell_columns = "\n".join(
        f"- {col.name} ({col.duckdb_type}): {col.description}" for col in spell_plan.columns
    )
    house_columns = "\n".join(
        f"- {col.name} ({col.duckdb_type}): {col.description}" for col in house_plan.columns
    )
    point_event_columns = "\n".join(
        f"- {col.name} ({col.duckdb_type}): {col.description}"
        for col in point_event_plan.columns
        if col.name != "event_id"
    )

    known_characters = (
        ", ".join(known_character_keys[:50]) if known_character_keys else "(none yet)"
    )
    known_spells = (
        ", ".join(known_spell_keys[:50]) if known_spell_keys else "(none yet)"
    )

    prompt = (
        f"Extraction goal:\n{user_request}\n\n"
        f"=== CHARACTERS ===\n"
        f"Table: {character_plan.table_name}\n"
        f"Unique key: {character_plan.unique_key}\n"
        f"Columns:\n{character_columns}\n"
        f"Characters already captured:\n{known_characters}\n\n"
        f"=== SPELLS ===\n"
        f"Table: {spell_plan.table_name}\n"
        f"Unique key: {spell_plan.unique_key}\n"
        f"Columns:\n{spell_columns}\n"
        f"Spells already captured:\n{known_spells}\n\n"
        f"=== HOUSES ===\n"
        f"Table: {house_plan.table_name}\n"
        f"Unique key: {house_plan.unique_key}\n"
        f"Columns:\n{house_columns}\n\n"
        f"=== HOUSE POINT EVENTS ===\n"
        f"Table: {point_event_plan.table_name}\n"
        f"Columns:\n{point_event_columns}\n\n"
        f"If a listed entity appears again with new facts, return a partial row "
        f"with only the new or updated fields.\n\n"
        f"Text chunk ({chunk_label}):\n"
        f"{chunk_text}"
    )

    last_error: BaseException | None = None
    attempts = max(max_retries, 1)

    for attempt in range(attempts):
        try:
            result = await asyncio.wait_for(
                extraction_agent.run(prompt), timeout=_ATTEMPT_DEADLINE_SECONDS
            )
            return result.output
        except Exception as e:
            last_error = e
            retryable = _is_retryable_api_error(e)
            if attempt >= attempts - 1 or not retryable:
                raise
            delay = _retry_delay_seconds(attempt, retry_base_delay_seconds)
            print(
                f"    API error on {chunk_label} (attempt {attempt + 1}/{attempts}): "
                f"{type(e).__name__}. Retrying in {delay:.0f}s..."
            )
            print(f"    Retry attempt {attempt + 2}/{attempts} for {chunk_label}...")
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
