"""Validate extracted rows in Python before DuckDB writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ALLOWED_BLOOD_STATUS = frozenset({"muggle", "half_blood", "wizard"})
ALLOWED_HOUSES = frozenset({"Gryffindor", "Hufflepuff", "Ravenclaw", "Slytherin"})
ALLOWED_ROLE_TYPES = frozenset({"student", "faculty"})

# Obvious non-human entities the extractor should never persist as characters.
NON_HUMAN_BLOCKLIST = frozenset(
    name.lower()
    for name in (
        "Dobby",
        "Kreacher",
        "Hedwig",
        "Fang",
        "Norbert",
        "Fluffy",
        "Nearly Headless Nick",
        "Sir Nicholas de Mimsy-Porpington",
        "The Bloody Baron",
        "The Fat Friar",
        "Peeves",
        "Firenze",
        "Ronan",
        "Bane",
    )
)

_NAME_STOPWORDS = frozenset({"mr", "mrs", "ms", "dr", "the", "a", "an"})


@dataclass
class ValidationSummary:
    """Running totals across all chunks in one pipeline run."""

    characters_kept: int = 0
    characters_dropped: int = 0
    spells_kept: int = 0
    spells_dropped: int = 0
    houses_kept: int = 0
    houses_dropped: int = 0
    point_events_kept: int = 0
    point_events_dropped: int = 0
    drop_messages: list[str] = field(default_factory=list)

    def record_chunk(
        self,
        *,
        characters_in: int,
        characters_out: int,
        spells_in: int,
        spells_out: int,
        houses_in: int = 0,
        houses_out: int = 0,
        point_events_in: int = 0,
        point_events_out: int = 0,
        messages: list[str],
    ) -> None:
        self.characters_kept += characters_out
        self.characters_dropped += characters_in - characters_out
        self.spells_kept += spells_out
        self.spells_dropped += spells_in - spells_out
        self.houses_kept += houses_out
        self.houses_dropped += houses_in - houses_out
        self.point_events_kept += point_events_out
        self.point_events_dropped += point_events_in - point_events_out
        self.drop_messages.extend(messages)

    def print_report(self) -> None:
        print("\n--- Validation summary ---")
        print(
            f"Characters: {self.characters_kept} kept, "
            f"{self.characters_dropped} dropped"
        )
        print(f"Spells: {self.spells_kept} kept, {self.spells_dropped} dropped")
        print(f"Houses: {self.houses_kept} kept, {self.houses_dropped} dropped")
        print(
            f"Point events: {self.point_events_kept} kept, "
            f"{self.point_events_dropped} dropped"
        )
        if self.drop_messages:
            print(f"Drop reasons logged: {len(self.drop_messages)}")
            for message in self.drop_messages[:20]:
                print(f"  - {message}")
            if len(self.drop_messages) > 20:
                print(f"  ... and {len(self.drop_messages) - 20} more")
        print("--------------------------\n")


def _is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _normalize_enum(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_house(value: str) -> str | None:
    cleaned = value.strip()
    for house in ALLOWED_HOUSES:
        if cleaned.lower() == house.lower():
            return house
    return None


def _name_tokens(name: str) -> list[str]:
    tokens = [
        token.strip(".,'\"")
        for token in name.replace(".", " ").split()
        if token.strip(".,'\"")
    ]
    significant = [
        token
        for token in tokens
        if len(token) >= 3 and token.lower() not in _NAME_STOPWORDS
    ]
    if significant:
        return significant
    return [token for token in tokens if len(token) >= 2]


def _appears_in_text(needle: str, chunk_text: str) -> bool:
    return needle.lower() in chunk_text.lower()


def _character_name_in_chunk(name: str, chunk_text: str) -> bool:
    if _appears_in_text(name, chunk_text):
        return True
    return any(_appears_in_text(token, chunk_text) for token in _name_tokens(name))


def validate_character_row(
    row: dict[str, Any],
    *,
    chunk_text: str,
    chunk_label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a cleaned row, or None with a drop reason."""
    name = row.get("character_name")
    if _is_empty(name):
        return None, f"{chunk_label}: character missing name"

    name = str(name).strip()
    if name.lower() in NON_HUMAN_BLOCKLIST:
        return None, f"{chunk_label}: blocked non-human '{name}'"

    if not _character_name_in_chunk(name, chunk_text):
        return None, f"{chunk_label}: character '{name}' not found in chunk text"

    cleaned = dict(row)
    cleaned["character_name"] = name

    blood_status = cleaned.get("blood_status")
    if not _is_empty(blood_status):
        normalized = _normalize_enum(str(blood_status))
        if normalized not in ALLOWED_BLOOD_STATUS:
            return None, f"{chunk_label}: character '{name}' has invalid blood_status '{blood_status}'"
        cleaned["blood_status"] = normalized

    house = cleaned.get("house")
    if not _is_empty(house):
        normalized_house = _normalize_house(str(house))
        if normalized_house is None:
            return None, f"{chunk_label}: character '{name}' has invalid house '{house}'"
        cleaned["house"] = normalized_house

    role_type = cleaned.get("role_type")
    if not _is_empty(role_type):
        normalized_role = _normalize_enum(str(role_type))
        if normalized_role not in ALLOWED_ROLE_TYPES:
            return None, f"{chunk_label}: character '{name}' has invalid role_type '{role_type}'"
        cleaned["role_type"] = normalized_role

    if not _is_empty(cleaned.get("classes_attended")) and cleaned.get("role_type") != "student":
        cleaned["classes_attended"] = None

    return cleaned, None


def validate_spell_row(
    row: dict[str, Any],
    *,
    chunk_text: str,
    chunk_label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a cleaned row, or None with a drop reason."""
    spell_name = row.get("spell_name")
    if _is_empty(spell_name):
        return None, f"{chunk_label}: spell missing name"

    spell_name = str(spell_name).strip()
    if not _appears_in_text(spell_name, chunk_text):
        return None, (
            f"{chunk_label}: spell '{spell_name}' incantation not found in chunk text"
        )

    cleaned = dict(row)
    cleaned["spell_name"] = spell_name
    return cleaned, None


def validate_house_row(
    row: dict[str, Any],
    *,
    chunk_label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a cleaned house row, or None with a drop reason."""
    house_name = row.get("house_name")
    if _is_empty(house_name):
        return None, f"{chunk_label}: house missing name"

    normalized_house = _normalize_house(str(house_name))
    if normalized_house is None:
        return None, f"{chunk_label}: invalid house '{house_name}'"

    cleaned = dict(row)
    cleaned["house_name"] = normalized_house
    return cleaned, None


def validate_point_event_row(
    row: dict[str, Any],
    *,
    chunk_label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a cleaned point event row, or None with a drop reason."""
    house_name = row.get("house_name")
    if _is_empty(house_name):
        return None, f"{chunk_label}: point event missing house_name"

    normalized_house = _normalize_house(str(house_name))
    if normalized_house is None:
        return None, f"{chunk_label}: point event invalid house '{house_name}'"

    points_raw = row.get("points")
    if _is_empty(points_raw):
        return None, f"{chunk_label}: point event missing points value"

    try:
        points = int(str(points_raw).strip())
    except ValueError:
        return None, f"{chunk_label}: point event non-integer points '{points_raw}'"

    if points == 0:
        return None, f"{chunk_label}: point event points must be non-zero"

    reason = row.get("reason")
    if _is_empty(reason):
        return None, f"{chunk_label}: point event missing reason"

    cleaned = dict(row)
    cleaned["house_name"] = normalized_house
    cleaned["points"] = points
    cleaned["reason"] = str(reason).strip()

    caused_by = cleaned.get("caused_by_student")
    if not _is_empty(caused_by):
        cleaned["caused_by_student"] = str(caused_by).strip()
    else:
        cleaned["caused_by_student"] = None

    return cleaned, None


def validate_chunk_rows(
    character_rows: list[dict[str, Any]],
    spell_rows: list[dict[str, Any]],
    house_rows: list[dict[str, Any]],
    point_event_rows: list[dict[str, Any]],
    *,
    chunk_text: str,
    chunk_label: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    """Validate and clean all row lists from one chunk before DuckDB writes."""
    valid_characters: list[dict[str, Any]] = []
    valid_spells: list[dict[str, Any]] = []
    valid_houses: list[dict[str, Any]] = []
    valid_point_events: list[dict[str, Any]] = []
    messages: list[str] = []

    for row in character_rows:
        cleaned, reason = validate_character_row(
            row, chunk_text=chunk_text, chunk_label=chunk_label
        )
        if cleaned is None:
            messages.append(reason or f"{chunk_label}: dropped character row")
        else:
            valid_characters.append(cleaned)

    for row in spell_rows:
        cleaned, reason = validate_spell_row(
            row, chunk_text=chunk_text, chunk_label=chunk_label
        )
        if cleaned is None:
            messages.append(reason or f"{chunk_label}: dropped spell row")
        else:
            valid_spells.append(cleaned)

    for row in house_rows:
        cleaned, reason = validate_house_row(row, chunk_label=chunk_label)
        if cleaned is None:
            messages.append(reason or f"{chunk_label}: dropped house row")
        else:
            valid_houses.append(cleaned)

    for row in point_event_rows:
        cleaned, reason = validate_point_event_row(row, chunk_label=chunk_label)
        if cleaned is None:
            messages.append(reason or f"{chunk_label}: dropped point event row")
        else:
            valid_point_events.append(cleaned)

    return valid_characters, valid_spells, valid_houses, valid_point_events, messages
