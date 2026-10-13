import pytest

from pdf2duckdb.validation import (
    RowRejected,
    ValidationSummary,
    validate_chunk_rows,
    validate_row,
)

CHUNK = "Alice Smith met Mr. Bob Jones at the bakery. Alice was happy."


def test_valid_row_is_cleaned(profile):
    row = {"name": "  Alice Smith ", "job": "baker", "traits": None, "mood": "HAPPY"}
    cleaned = validate_row(profile.table("people"), row, chunk_text=CHUNK)
    assert cleaned == {"name": "Alice Smith", "job": "baker", "traits": None, "mood": "happy"}


def test_any_token_grounding_accepts_partial_name(profile):
    row = {"name": "Robert Jones"}
    assert validate_row(profile.table("people"), row, chunk_text=CHUNK)["name"] == "Robert Jones"


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"name": None}, "missing key"),
        ({"name": "Zed Nobody"}, "not found in chunk text"),
        ({"name": "Alice Smith", "mood": "furious"}, "invalid mood"),
    ],
)
def test_people_rejections(profile, row, reason):
    with pytest.raises(RowRejected, match=reason):
        validate_row(profile.table("people"), row, chunk_text=CHUNK)


def test_required_and_integer_columns(profile):
    meetings = profile.table("meetings")
    assert validate_row(meetings, {"summary": "Met", "attendees": "2"}, chunk_text=CHUNK) == {
        "summary": "Met",
        "attendees": 2,
    }
    with pytest.raises(RowRejected, match="missing summary"):
        validate_row(meetings, {"summary": " "}, chunk_text=CHUNK)
    with pytest.raises(RowRejected, match="non-integer"):
        validate_row(meetings, {"summary": "Met", "attendees": "two"}, chunk_text=CHUNK)


def test_blocklist(profile):
    table = profile.table("people").model_copy(update={"blocklist": ["Alice Smith"]})
    with pytest.raises(RowRejected, match="blocklisted"):
        validate_row(table, {"name": "alice smith"}, chunk_text=CHUNK)


def test_validate_chunk_rows_tracks_summary(profile):
    summary = ValidationSummary()
    kept, messages = validate_chunk_rows(
        profile,
        {"people": [{"name": "Alice Smith"}, {"name": "Ghost"}], "meetings": []},
        chunk_text=CHUNK,
        chunk_label="chunk 1",
        summary=summary,
    )
    assert [row["name"] for row in kept["people"]] == ["Alice Smith"]
    assert messages == ["chunk 1: people: name 'Ghost' not found in chunk text"]
    assert summary.kept == {"people": 1, "meetings": 0}
    assert summary.dropped == {"people": 1, "meetings": 0}
