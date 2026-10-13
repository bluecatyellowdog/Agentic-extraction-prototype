"""Validate extracted rows in Python before DuckDB writes.

All rules come from the profile: required columns, closed value sets, grounding in
the chunk text, blocklists and column types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pdf2duckdb.profile import ColumnSpec, ExtractionProfile, TableSpec

logger = logging.getLogger(__name__)

_NAME_STOPWORDS = frozenset({"mr", "mrs", "ms", "miss", "dr", "sir", "the", "a", "an", "of"})
_TRUE_VALUES = frozenset({"true", "yes", "1"})
_FALSE_VALUES = frozenset({"false", "no", "0"})


class RowRejected(ValueError):
    """Raised when a row fails validation; the message is the drop reason."""


@dataclass
class ValidationSummary:
    """Running totals across all chunks in one pipeline run."""

    kept: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    drop_messages: list[str] = field(default_factory=list)

    def record(self, table_name: str, *, kept: int, dropped: int, messages: list[str]) -> None:
        self.kept[table_name] = self.kept.get(table_name, 0) + kept
        self.dropped[table_name] = self.dropped.get(table_name, 0) + dropped
        self.drop_messages.extend(messages)

    def log_report(self, max_messages: int = 20) -> None:
        logger.info("Validation summary:")
        for table_name in self.kept:
            logger.info(
                "  %s: %d kept, %d dropped",
                table_name,
                self.kept[table_name],
                self.dropped.get(table_name, 0),
            )
        for message in self.drop_messages[:max_messages]:
            logger.info("  - %s", message)
        if len(self.drop_messages) > max_messages:
            logger.info("  ... and %d more", len(self.drop_messages) - max_messages)


def _is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _enum_form(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _significant_tokens(text: str) -> list[str]:
    tokens = [token.strip(".,'\"") for token in text.replace(".", " ").split()]
    tokens = [token for token in tokens if token]
    significant = [t for t in tokens if len(t) >= 3 and t.lower() not in _NAME_STOPWORDS]
    return significant or [t for t in tokens if len(t) >= 2]


def appears_in_text(value: str, chunk_text: str, mode: str) -> bool:
    haystack = chunk_text.lower()
    if value.lower() in haystack:
        return True
    if mode == "any_token":
        return any(token.lower() in haystack for token in _significant_tokens(value))
    return False


def _coerce_type(column: ColumnSpec, value: str) -> Any:
    if column.type == "INTEGER":
        try:
            return int(float(value))
        except ValueError:
            raise RowRejected(f"non-integer {column.name} {value!r}") from None
    if column.type == "DOUBLE":
        try:
            return float(value)
        except ValueError:
            raise RowRejected(f"non-numeric {column.name} {value!r}") from None
    if column.type == "BOOLEAN":
        lowered = value.lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        raise RowRejected(f"non-boolean {column.name} {value!r}")
    if column.type == "DATE":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise RowRejected(f"{column.name} {value!r} is not an ISO date") from None
    return value


def clean_value(column: ColumnSpec, raw: Any, chunk_text: str) -> Any:
    """Return the cleaned value for one column, or raise RowRejected."""
    if _is_empty(raw):
        if column.required:
            raise RowRejected(f"missing {column.name}")
        return None

    value = str(raw).strip()

    if column.allowed_values:
        by_form = {_enum_form(allowed): allowed for allowed in column.allowed_values}
        canonical = by_form.get(_enum_form(value))
        if canonical is None:
            raise RowRejected(f"invalid {column.name} {value!r}")
        value = canonical

    if column.grounding and not appears_in_text(value, chunk_text, column.grounding):
        raise RowRejected(f"{column.name} {value!r} not found in chunk text")

    return _coerce_type(column, value)


def validate_row(table: TableSpec, row: dict[str, Any], *, chunk_text: str) -> dict[str, Any]:
    """Return a cleaned copy of the row, or raise RowRejected."""
    if table.key:
        key_value = row.get(table.key)
        if _is_empty(key_value):
            raise RowRejected(f"missing key {table.key}")
        blocked = {name.lower() for name in table.blocklist}
        if str(key_value).strip().lower() in blocked:
            raise RowRejected(f"{key_value!r} is blocklisted")

    return {
        column.name: clean_value(column, row.get(column.name), chunk_text)
        for column in table.columns
    }


def validate_chunk_rows(
    profile: ExtractionProfile,
    rows: dict[str, list[dict[str, Any]]],
    *,
    chunk_text: str,
    chunk_label: str,
    summary: ValidationSummary | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Validate every table's rows from one chunk. Returns kept rows and drop messages."""
    kept: dict[str, list[dict[str, Any]]] = {}
    all_messages: list[str] = []
    for table in profile.tables:
        table_rows = rows.get(table.name, [])
        valid: list[dict[str, Any]] = []
        messages: list[str] = []
        for row in table_rows:
            try:
                valid.append(validate_row(table, row, chunk_text=chunk_text))
            except RowRejected as rejection:
                messages.append(f"{chunk_label}: {table.name}: {rejection}")
        kept[table.name] = valid
        all_messages.extend(messages)
        if summary is not None:
            summary.record(
                table.name,
                kept=len(valid),
                dropped=len(table_rows) - len(valid),
                messages=messages,
            )
    return kept, all_messages
