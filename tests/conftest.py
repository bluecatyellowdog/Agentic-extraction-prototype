from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pdf2duckdb.config import Settings
from pdf2duckdb.profile import ExtractionProfile

TEST_PROFILE = {
    "name": "test_people",
    "description": "People and meetings.",
    "tables": [
        {
            "name": "people",
            "description": "One row per named person.",
            "key": "name",
            "columns": [
                {"name": "name", "description": "Full name", "grounding": "any_token"},
                {"name": "job", "description": "Occupation", "track_conflicts": True},
                {"name": "traits", "description": "Traits", "merge": "append"},
                {
                    "name": "mood",
                    "description": "Mood",
                    "allowed_values": ["happy", "sad"],
                },
            ],
        },
        {
            "name": "meetings",
            "description": "One row per meeting.",
            "columns": [
                {"name": "summary", "description": "What happened", "required": True},
                {"name": "attendees", "description": "Count", "type": "INTEGER"},
            ],
        },
    ],
}


@pytest.fixture
def profile() -> ExtractionProfile:
    return ExtractionProfile.model_validate(TEST_PROFILE)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openrouter_api_key="test-key",
        openrouter_model="test/model",
        chunk_size_words=12,
        chunk_overlap_words=2,
    )


@pytest.fixture
def make_pdf(tmp_path: Path) -> Callable[[str], Path]:
    def _make(text: str, name: str = "input.pdf") -> Path:
        path = tmp_path / name
        document = pymupdf.open()
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
        document.save(path)
        document.close()
        return path

    return _make


def chunk_text_from(messages: list[ModelMessage]) -> str:
    """Pull the chunk text out of the last user prompt sent to the model."""
    prompt = str(messages[-1].parts[-1].content)
    return prompt.split("Text chunk (", 1)[1].split("\n", 1)[1]


@pytest.fixture
def fake_model() -> Callable[[Callable[[str], dict[str, Any]]], FunctionModel]:
    """Build a model that answers each chunk with `respond(chunk_text)` as structured output."""

    def _build(respond: Callable[[str], dict[str, Any]]) -> FunctionModel:
        def _call(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_name = info.output_tools[0].name
            return ModelResponse(
                parts=[ToolCallPart(tool_name, respond(chunk_text_from(messages)))]
            )

        return FunctionModel(_call)

    return _build
