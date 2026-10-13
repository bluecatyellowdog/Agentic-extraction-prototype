"""Fixed database schemas and Pydantic models for agent output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# Allowed DuckDB column types in DatabasePlan definitions.
DuckDBType = Literal["VARCHAR", "INTEGER", "DOUBLE", "BOOLEAN", "DATE"]


class ColumnDefinition(BaseModel):
    """One column in a table plan."""

    name: str = Field(description="Snake_case column name, e.g. character_name")
    duckdb_type: DuckDBType = Field(description="DuckDB data type")
    description: str = Field(
        description="What this column stores; injected into the extraction prompt per chunk"
    )


class DatabasePlan(BaseModel):
    """
    Declarative definition of one DuckDB table.

    Used to create tables, build per-chunk prompts, and drive upsert/insert logic.
    The reasoning field is documentation for humans (printed at startup only).
    """

    table_name: str = Field(description="Snake_case table name, e.g. characters")
    unique_key: str = Field(
        description="Column used to merge duplicate rows across chunks (except append-only tables)"
    )
    columns: list[ColumnDefinition] = Field(min_length=1)
    reasoning: str = Field(
        description="Why this table is shaped this way (human docs only, not sent to the LLM)"
    )


CHARACTER_DATABASE_PLAN = DatabasePlan(
    table_name="characters",
    unique_key="character_name",
    columns=[
        ColumnDefinition(
            name="character_name",
            duckdb_type="VARCHAR",
            description="Full canonical name of the human character.",
        ),
        ColumnDefinition(
            name="blood_status",
            duckdb_type="VARCHAR",
            description=(
                "Blood status: exactly one of muggle, half_blood, or wizard. "
                "Use null if not stated or unclear in the text."
            ),
        ),
        ColumnDefinition(
            name="house",
            duckdb_type="VARCHAR",
            description=(
                "Hogwarts house if the character belongs to one: Gryffindor, "
                "Hufflepuff, Ravenclaw, or Slytherin. Null if not in a house or unknown."
            ),
        ),
        ColumnDefinition(
            name="role_type",
            duckdb_type="VARCHAR",
            description=(
                "Hogwarts affiliation: exactly student or faculty when applicable. "
                "Use student for pupils, faculty for teachers/staff at Hogwarts. "
                "Null if neither applies (e.g. muggles, non-Hogwarts adults)."
            ),
        ),
        ColumnDefinition(
            name="classes_attended",
            duckdb_type="VARCHAR",
            description=(
                "Comma-separated Hogwarts classes or subjects this student attends "
                "or is seen in (e.g. Transfiguration, Potions, Defense Against the Dark Arts). "
                "Null if not a student or no classes mentioned in the text."
            ),
        ),
        ColumnDefinition(
            name="occupation",
            duckdb_type="VARCHAR",
            description=(
                "Job, profession, or role outside Hogwarts context (e.g. Auror, shopkeeper). "
                "For faculty, use the teaching subject if mentioned (e.g. Transfiguration professor). "
                "Null if none is mentioned."
            ),
        ),
        ColumnDefinition(
            name="character_description",
            duckdb_type="VARCHAR",
            description=(
                "Short prose description of this person's character and personality as "
                "shown or described in the text — how they behave, speak, and relate to "
                "others. One or two sentences. Null if none mentioned."
            ),
        ),
    ],
    reasoning=(
        "One row per human character, tracking blood status, house, Hogwarts role, "
        "classes, occupation, and a narrative character description."
    ),
)


SPELL_DATABASE_PLAN = DatabasePlan(
    table_name="spells",
    unique_key="spell_name",
    columns=[
        ColumnDefinition(
            name="spell_name",
            duckdb_type="VARCHAR",
            description=(
                "Exact incantation name as exclaimed or spoken in the text "
                "(e.g. Wingardium Leviosa, Expelliarmus, Lumos)."
            ),
        ),
        ColumnDefinition(
            name="effect_description",
            duckdb_type="VARCHAR",
            description=(
                "Brief description of what the spell does, based only on this text."
            ),
        ),
        ColumnDefinition(
            name="used_by",
            duckdb_type="VARCHAR",
            description=(
                "Human character(s) who speak or cast this spell in the text, "
                "using full names when known (comma-separated if multiple)."
            ),
        ),
    ],
    reasoning=(
        "One row per named spell incantation explicitly spoken in the source text, "
        "with its effect and who uses it."
    ),
)


HOGWARTS_HOUSE_NAMES = ("Gryffindor", "Hufflepuff", "Ravenclaw", "Slytherin")

HOUSE_DATABASE_PLAN = DatabasePlan(
    table_name="houses",
    unique_key="house_name",
    columns=[
        ColumnDefinition(
            name="house_name",
            duckdb_type="VARCHAR",
            description="Hogwarts house: Gryffindor, Hufflepuff, Ravenclaw, or Slytherin.",
        ),
        ColumnDefinition(
            name="current_students",
            duckdb_type="VARCHAR",
            description=(
                "Comma-separated full names of current Hogwarts students in this house."
            ),
        ),
        ColumnDefinition(
            name="prior_students",
            duckdb_type="VARCHAR",
            description=(
                "Comma-separated full names of prior students or alumni of this house "
                "mentioned in the text."
            ),
        ),
        ColumnDefinition(
            name="others_not_in_house",
            duckdb_type="VARCHAR",
            description=(
                "Comma-separated full names of humans explicitly mentioned as NOT "
                "belonging to this house (e.g. sorted elsewhere, rejected, or never attended)."
            ),
        ),
        ColumnDefinition(
            name="lore_facts",
            duckdb_type="VARCHAR",
            description=(
                "Semicolon-separated house-specific lore from the text: traits, "
                "common room password, appearance, decorations, founder notes, etc. "
                "Do NOT include house point awards or deductions here."
            ),
        ),
    ],
    reasoning=(
        "One row per Hogwarts house with student rosters (current and prior), "
        "people not in the house, and house-specific lore (not point events)."
    ),
)


HOUSE_POINT_EVENT_DATABASE_PLAN = DatabasePlan(
    table_name="house_point_events",
    unique_key="event_id",
    columns=[
        ColumnDefinition(
            name="event_id",
            duckdb_type="INTEGER",
            description="Auto-assigned row id (do not set in extraction output).",
        ),
        ColumnDefinition(
            name="house_name",
            duckdb_type="VARCHAR",
            description="Hogwarts house that gained or lost points.",
        ),
        ColumnDefinition(
            name="points",
            duckdb_type="INTEGER",
            description=(
                "Point change: positive integer when awarded, negative when deducted "
                "(e.g. 10 or -5)."
            ),
        ),
        ColumnDefinition(
            name="caused_by_student",
            duckdb_type="VARCHAR",
            description=(
                "Full name of the student who caused this point change, if stated "
                "in the text. Null if not attributed to a student."
            ),
        ),
        ColumnDefinition(
            name="reason",
            duckdb_type="VARCHAR",
            description="Brief explanation from the text of why points were awarded or deducted.",
        ),
    ],
    reasoning=(
        "One row per explicit house point award or deduction in the source text, "
        "separate from house lore."
    ),
)


class ExtractedRow(BaseModel):
    """One row of extracted data. Values are strings; DuckDB handles casting."""

    values: dict[str, str | None] = Field(
        description="Column name -> value. Use null for unknown fields."
    )

    @field_validator("values", mode="before")
    @classmethod
    def _coerce_values_to_strings(cls, value: object) -> object:
        """LLMs often return JSON numbers for INTEGER columns (e.g. points: -50)."""
        if not isinstance(value, dict):
            return value
        coerced: dict[str, str | None] = {}
        for key, item in value.items():
            if item is None:
                coerced[key] = None
            elif isinstance(item, bool):
                coerced[key] = str(item).lower()
            elif isinstance(item, (int, float)):
                coerced[key] = str(item)
            else:
                coerced[key] = item
        return coerced


class ChunkExtractionResult(BaseModel):
    """Structured output from one extraction-agent call on a single text chunk."""

    rows: list[ExtractedRow] = Field(
        default_factory=list,
        description="Human character rows for this chunk",
    )
    spell_rows: list[ExtractedRow] = Field(
        default_factory=list,
        description="Named spell rows for this chunk (incantations spoken in the text)",
    )
    house_rows: list[ExtractedRow] = Field(
        default_factory=list,
        description="Hogwarts house rows updated in this chunk (lore and rosters only)",
    )
    point_event_rows: list[ExtractedRow] = Field(
        default_factory=list,
        description="House point award/deduction events in this chunk",
    )
    notes: str = Field(
        default="",
        description="Optional notes about ambiguity or partial information",
    )


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


def row_to_dict(row: ExtractedRow, column_names: list[str]) -> dict[str, Any]:
    """Convert an ExtractedRow into a plain dict with all expected columns."""
    result: dict[str, Any] = {}
    for name in column_names:
        result[name] = row.values.get(name)
    return result
