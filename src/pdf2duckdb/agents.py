"""Extraction agent: structured output via Pydantic AI (no tools)."""

from __future__ import annotations

import asyncio
import logging

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, ModelSettings
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    ToolRetryError,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from pdf2duckdb.config import Settings
from pdf2duckdb.profile import ExtractionProfile
from pdf2duckdb.schemas import build_chunk_result_model, column_prompt_description

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 5.0
_MAX_RETRY_DELAY_SECONDS = 60.0
# Hard cap per HTTP request; without it the client can hang on a dead connection.
_REQUEST_TIMEOUT_SECONDS = 180.0
# Total deadline per attempt. OpenRouter sends keep-alive bytes on slow
# non-streaming requests, so HTTP read timeouts never fire when the model is
# stuck generating; this wall-clock cap is the only reliable cutoff.
_ATTEMPT_DEADLINE_SECONDS = 240.0
# Cap output so runaway generation fails fast instead of running for minutes.
_MAX_OUTPUT_TOKENS = 8192
# How many already-known keys per table to list in each chunk prompt.
_KNOWN_KEYS_IN_PROMPT = 50

_GENERAL_RULES = (
    "Use null for any field the chunk does not support. Do not guess.",
    "Return one row per entity per table; merge duplicates within the chunk.",
    "Entities listed under 'already captured' only need a row if this chunk adds new "
    "facts; then return a partial row with the key and only the new fields.",
    "Return empty lists when nothing relevant appears in the chunk.",
)


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
    return isinstance(
        error, (ValidationError, ToolRetryError, ModelAPIError, UnexpectedModelBehavior)
    )


def build_system_prompt(profile: ExtractionProfile) -> str:
    """Render the profile into the extraction agent's system prompt."""
    lines = [
        f"You extract structured data from document text. Goal: {profile.description}",
        "",
        "GENERAL RULES:",
        *(f"- {rule}" for rule in (*profile.instructions, *_GENERAL_RULES)),
    ]
    for table in profile.tables:
        lines += ["", f"TABLE '{table.name}': {table.description}"]
        if table.key:
            lines.append(f"Key column: {table.key} (rows with the same key are merged)")
        else:
            lines.append("Append-only: every row is stored as a separate record.")
        lines.append("Columns:")
        lines += [f"- {c.name}: {column_prompt_description(c)}" for c in table.columns]
        lines += [f"- Rule: {rule}" for rule in table.instructions]
    return "\n".join(lines)


def build_openrouter_model(settings: Settings) -> Model:
    # Explicit client so the request timeout is actually enforced; internal
    # SDK retries are disabled because extract_from_chunk retries with backoff.
    openai_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    return OpenAIChatModel(
        settings.openrouter_model,
        provider=OpenRouterProvider(openai_client=openai_client),
    )


def build_extraction_agent(
    settings: Settings,
    profile: ExtractionProfile,
    *,
    model: Model | None = None,
) -> Agent[None, BaseModel]:
    """Build the extraction agent for a profile. `model` overrides OpenRouter (e.g. in tests)."""
    return Agent(
        model or build_openrouter_model(settings),
        output_type=build_chunk_result_model(profile),
        retries=DEFAULT_MAX_RETRIES,
        model_settings=ModelSettings(
            temperature=settings.openrouter_temperature,
            max_tokens=_MAX_OUTPUT_TOKENS,
        ),
        system_prompt=build_system_prompt(profile),
    )


def build_chunk_prompt(
    *,
    chunk_text: str,
    chunk_label: str,
    known_keys: dict[str, list[str]],
    user_request: str | None = None,
) -> str:
    parts: list[str] = []
    if user_request:
        parts.append(f"Additional guidance for this document:\n{user_request}")
    for table_name, keys in known_keys.items():
        shown = ", ".join(keys[-_KNOWN_KEYS_IN_PROMPT:]) if keys else "(none yet)"
        parts.append(f"Already captured in '{table_name}': {shown}")
    parts.append(f"Text chunk ({chunk_label}):\n{chunk_text}")
    return "\n\n".join(parts)


async def extract_from_chunk(
    extraction_agent: Agent[None, BaseModel],
    *,
    prompt: str,
    chunk_label: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
) -> BaseModel:
    """Run extraction on one chunk, retrying transient API errors with exponential backoff."""
    attempts = max(max_retries, 1)
    for attempt in range(attempts):
        try:
            result = await asyncio.wait_for(
                extraction_agent.run(prompt), timeout=_ATTEMPT_DEADLINE_SECONDS
            )
            return result.output
        except Exception as error:
            if attempt >= attempts - 1 or not _is_retryable_api_error(error):
                raise
            delay = _retry_delay_seconds(attempt, retry_base_delay_seconds)
            logger.warning(
                "    %s on %s (attempt %d/%d), retrying in %.0fs",
                type(error).__name__,
                chunk_label,
                attempt + 1,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
