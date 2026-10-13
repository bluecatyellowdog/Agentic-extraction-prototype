"""Pydantic models for agent output, generated from an extraction profile."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model, model_validator

from pdf2duckdb.profile import ColumnSpec, ExtractionProfile, TableSpec

NOTES_FIELD = "notes"


class ExtractedRowBase(BaseModel):
    """Base for generated row models. Every column is an optional string."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_scalars_to_strings(cls, value: Any) -> Any:
        """LLMs often return JSON numbers or booleans (e.g. points: -50)."""
        if not isinstance(value, dict):
            return value
        coerced: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, bool):
                coerced[key] = str(item).lower()
            elif isinstance(item, (int, float)):
                coerced[key] = str(item)
            else:
                coerced[key] = item
        return coerced


def column_prompt_description(column: ColumnSpec) -> str:
    description = column.description.strip()
    if column.allowed_values:
        description += f" One of: {', '.join(column.allowed_values)}."
    if column.type != "VARCHAR":
        description += f" ({column.type})"
    return description


def build_row_model(table: TableSpec) -> type[ExtractedRowBase]:
    fields: dict[str, Any] = {
        column.name: (
            str | None,
            Field(default=None, description=column_prompt_description(column)),
        )
        for column in table.columns
    }
    model_name = "".join(part.title() for part in table.name.split("_")) + "Row"
    return create_model(model_name, __base__=ExtractedRowBase, **fields)


def build_chunk_result_model(profile: ExtractionProfile) -> type[BaseModel]:
    """One list of rows per profile table, plus free-text notes."""
    fields: dict[str, Any] = {
        table.name: (
            list[build_row_model(table)],
            Field(default_factory=list, description=table.description),
        )
        for table in profile.tables
    }
    if NOTES_FIELD in fields:
        raise ValueError(f"Table name {NOTES_FIELD!r} is reserved")
    fields[NOTES_FIELD] = (
        str,
        Field(default="", description="Optional notes about ambiguity or partial information"),
    )
    return create_model("ChunkExtractionResult", **fields)


def rows_by_table(result: BaseModel, profile: ExtractionProfile) -> dict[str, list[dict[str, Any]]]:
    """Convert agent output into plain dicts per table."""
    return {
        table.name: [row.model_dump() for row in getattr(result, table.name)]
        for table in profile.tables
    }


class FieldResolution(BaseModel):
    """One canonical value chosen for a contradicted entity field."""

    table_name: str
    entity_key: str
    field_name: str
    canonical_value: str | None = Field(
        description="Final value to store; null if the field should be cleared"
    )
    reason: str = Field(description="Brief justification using the claim timeline")


class WrapUpResult(BaseModel):
    """Output of the wrap-up agent reconciling logged fact conflicts."""

    resolutions: list[FieldResolution] = Field(default_factory=list)
