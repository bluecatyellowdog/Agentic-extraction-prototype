"""Environment-backed settings: OpenRouter credentials and chunk defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a .env file in the project root (if it exists).
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Everything you might want to tweak lives here."""

    openrouter_api_key: str
    openrouter_model: str

    # Sampling temperature for extraction (lower = more deterministic).
    openrouter_temperature: float = 0.1

    # Stronger model for one-shot conflict wrap-up (optional; set to enable step 4).
    openrouter_wrapup_model: str | None = None
    openrouter_wrapup_temperature: float = 0.1

    # How many words per chunk when processing long texts.
    chunk_size_words: int = 3_000

    # Overlap between chunks so entities split across boundaries are not lost.
    chunk_overlap_words: int = 400

    # Folder where DuckDB files are written.
    output_dir: Path = Path("output")


def get_settings() -> Settings:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing OPENROUTER_API_KEY. Copy .env.example to .env and add your key.")

    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.5-flash").strip()

    temperature_raw = os.getenv("OPENROUTER_TEMPERATURE", "0.1").strip()
    try:
        temperature = float(temperature_raw)
    except ValueError as e:
        raise ValueError(f"OPENROUTER_TEMPERATURE must be a number, got {temperature_raw!r}") from e
    if not 0.0 <= temperature <= 2.0:
        raise ValueError(f"OPENROUTER_TEMPERATURE must be between 0.0 and 2.0, got {temperature}")

    wrapup_model = os.getenv("OPENROUTER_WRAPUP_MODEL", "").strip() or None

    wrapup_temperature_raw = os.getenv("OPENROUTER_WRAPUP_TEMPERATURE", "0.1").strip()
    try:
        wrapup_temperature = float(wrapup_temperature_raw)
    except ValueError as e:
        raise ValueError(
            f"OPENROUTER_WRAPUP_TEMPERATURE must be a number, got {wrapup_temperature_raw!r}"
        ) from e
    if not 0.0 <= wrapup_temperature <= 2.0:
        raise ValueError(
            f"OPENROUTER_WRAPUP_TEMPERATURE must be between 0.0 and 2.0, got {wrapup_temperature}"
        )

    return Settings(
        openrouter_api_key=api_key,
        openrouter_model=model,
        openrouter_temperature=temperature,
        openrouter_wrapup_model=wrapup_model,
        openrouter_wrapup_temperature=wrapup_temperature,
    )
