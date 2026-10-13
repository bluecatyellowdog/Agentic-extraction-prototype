"""Extraction profiles: the declarative description of what to pull out of a document.

A profile names the tables to build, their columns, and the rules the LLM and the
validator must follow. Everything domain-specific lives in a profile (YAML), so the
pipeline itself stays generic.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

DuckDBType = Literal["VARCHAR", "INTEGER", "DOUBLE", "BOOLEAN", "DATE"]

# Columns added automatically to append-only tables (tables without a key).
ROW_ID_COLUMN = "id"
CHUNK_INDEX_COLUMN = "chunk_index"


class ColumnSpec(BaseModel):
    """One column in an extracted table."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: DuckDBType = "VARCHAR"
    description: str
    required: bool = Field(
        default=False,
        description="Drop the row if this column is empty.",
    )
    allowed_values: list[str] | None = Field(
        default=None,
        description=(
            "Closed set of values. Matched case-insensitively (ignoring spaces, hyphens and "
            "underscores) and stored in the spelling given here; anything else drops the row."
        ),
    )
    merge: Literal["replace", "append"] = Field(
        default="replace",
        description=(
            "How a new value combines with an existing one on upsert: 'replace' overwrites, "
            "'append' joins distinct fragments with `separator`."
        ),
    )
    separator: str = ", "
    track_conflicts: bool = Field(
        default=False,
        description="Log a dispute when a later chunk claims a different non-empty value.",
    )
    grounding: Literal["exact", "any_token"] | None = Field(
        default=None,
        description=(
            "Require the value to appear in the chunk text: 'exact' as a substring, "
            "'any_token' if any significant word of it appears (useful for names)."
        ),
    )

    @model_validator(mode="after")
    def _check_merge_mode(self) -> ColumnSpec:
        if self.merge == "append" and self.type != "VARCHAR":
            raise ValueError(f"column {self.name!r}: merge='append' requires type VARCHAR")
        if self.track_conflicts and self.merge == "append":
            raise ValueError(
                f"column {self.name!r}: track_conflicts only applies to merge='replace' columns"
            )
        return self


class TableSpec(BaseModel):
    """One output table.

    Tables with a `key` are entity tables: rows with the same key are merged across
    chunks. Tables without a key are append-only event logs; they get an auto `id`
    and the `chunk_index` the row came from.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    key: str | None = None
    columns: list[ColumnSpec] = Field(min_length=1)
    instructions: list[str] = Field(
        default_factory=list,
        description="Extra rules for the LLM, specific to this table.",
    )
    seed_keys: list[str] = Field(
        default_factory=list,
        description="Rows created up front (entity tables only), e.g. a fixed set of categories.",
    )
    blocklist: list[str] = Field(
        default_factory=list,
        description="Key values that are always dropped (case-insensitive).",
    )

    @model_validator(mode="after")
    def _check_columns(self) -> TableSpec:
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"table {self.name!r}: duplicate column names")
        if self.key is None:
            reserved = {ROW_ID_COLUMN, CHUNK_INDEX_COLUMN} & set(names)
            if reserved:
                raise ValueError(
                    f"table {self.name!r}: {sorted(reserved)} are added automatically "
                    "to append-only tables"
                )
            if self.seed_keys or self.blocklist:
                raise ValueError(f"table {self.name!r}: seed_keys and blocklist need a key column")
        elif self.key not in names:
            raise ValueError(f"table {self.name!r}: key {self.key!r} is not a column")
        return self

    @property
    def is_append_only(self) -> bool:
        return self.key is None

    def column(self, name: str) -> ColumnSpec:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    @property
    def tracked_fields(self) -> list[str]:
        return [column.name for column in self.columns if column.track_conflicts]


class ExtractionProfile(BaseModel):
    """A complete extraction setup: general instructions plus the tables to fill."""

    name: str
    description: str
    instructions: list[str] = Field(default_factory=list)
    tables: list[TableSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_tables(self) -> ExtractionProfile:
        names = [table.name for table in self.tables]
        if len(names) != len(set(names)):
            raise ValueError("duplicate table names")
        return self

    def table(self, name: str) -> TableSpec:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)


BUILTIN_PROFILE_PACKAGE = "pdf2duckdb.profiles"


def builtin_profile_names() -> list[str]:
    files = resources.files(BUILTIN_PROFILE_PACKAGE).iterdir()
    return sorted(f.name.removesuffix(".yaml") for f in files if f.name.endswith(".yaml"))


def load_profile(name_or_path: str | Path) -> ExtractionProfile:
    """Load a built-in profile by name, or any profile from a YAML file path."""
    path = Path(name_or_path)
    if path.suffix in {".yaml", ".yml"}:
        if not path.exists():
            raise ValueError(f"Profile file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        resource = resources.files(BUILTIN_PROFILE_PACKAGE) / f"{name_or_path}.yaml"
        if not resource.is_file():
            available = ", ".join(builtin_profile_names())
            raise ValueError(
                f"Unknown profile {str(name_or_path)!r}. Built-in profiles: {available}. "
                "Or pass a path to a .yaml file."
            )
        text = resource.read_text(encoding="utf-8")
    return ExtractionProfile.model_validate(yaml.safe_load(text))
