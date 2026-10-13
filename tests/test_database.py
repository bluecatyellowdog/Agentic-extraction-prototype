import pytest

from pdf2duckdb.database import merge_text, prepare_database, write_rows
from pdf2duckdb.dbutil import primary_key_column
from pdf2duckdb.disputes import (
    apply_manual_resolution,
    count_open_disputes,
    fetch_unresolved_conflict_groups,
)


@pytest.fixture
def conn(tmp_path, profile):
    connection = prepare_database(tmp_path / "test.db", profile, resume=False)
    yield connection
    connection.close()


def test_merge_text_dedupes_case_insensitively():
    assert merge_text("brave, kind", "Kind, loud", separator=", ") == "brave, kind, loud"
    assert merge_text(None, "x", separator=", ") == "x"
    assert merge_text("x", None, separator=", ") == "x"


def test_upsert_merges_by_key(conn, profile):
    people = profile.table("people")
    write_rows(conn, people, [{"name": "Alice", "traits": "brave"}], chunk_index=0)
    write_rows(conn, people, [{"name": "Alice", "job": "baker", "traits": "kind"}], chunk_index=1)

    rows = conn.execute('SELECT name, job, traits FROM "people"').fetchall()
    assert rows == [("Alice", "baker", "brave, kind")]


def test_empty_incoming_value_keeps_existing(conn, profile):
    people = profile.table("people")
    write_rows(conn, people, [{"name": "Alice", "job": "baker"}], chunk_index=0)
    write_rows(conn, people, [{"name": "Alice", "job": None}], chunk_index=1)
    assert conn.execute('SELECT job FROM "people"').fetchone() == ("baker",)


def test_append_only_table_gets_ids_and_chunk_index(conn, profile):
    meetings = profile.table("meetings")
    write_rows(conn, meetings, [{"summary": "a"}, {"summary": "b"}], chunk_index=0)
    write_rows(conn, meetings, [{"summary": "c", "attendees": 4}], chunk_index=3)

    rows = conn.execute('SELECT id, chunk_index, summary, attendees FROM "meetings"').fetchall()
    assert rows == [(1, 0, "a", None), (2, 0, "b", None), (3, 3, "c", 4)]


def test_conflicting_tracked_field_opens_dispute(conn, profile):
    people = profile.table("people")
    write_rows(conn, people, [{"name": "Alice", "job": "baker"}], chunk_index=0)
    write_rows(conn, people, [{"name": "Alice", "job": "Baker"}], chunk_index=1)  # same, new case
    assert count_open_disputes(conn) == 0

    write_rows(conn, people, [{"name": "Alice", "job": "spy"}], chunk_index=2)
    [group] = fetch_unresolved_conflict_groups(conn)
    assert (group.table_name, group.entity_key, group.field_name) == ("people", "Alice", "job")
    assert group.current_value == "spy"
    assert [(c.previous_value, c.incoming_value) for c in group.claims] == [("Baker", "spy")]


def test_manual_resolution_updates_entity(conn, profile):
    people = profile.table("people")
    write_rows(conn, people, [{"name": "Alice", "job": "baker"}], chunk_index=0)
    write_rows(conn, people, [{"name": "Alice", "job": "spy"}], chunk_index=1)

    resolved = apply_manual_resolution(
        conn, entity_key="Alice", field_name="job", canonical_value="baker", reason="checked"
    )
    assert resolved
    assert conn.execute('SELECT job FROM "people"').fetchone() == ("baker",)
    assert count_open_disputes(conn) == 0


def test_primary_key_lookup(conn):
    assert primary_key_column(conn, "people") == "name"
    assert primary_key_column(conn, "meetings") == "id"


def test_seed_keys_are_inserted(tmp_path, profile):
    table = profile.table("people").model_copy(update={"seed_keys": ["Alice", "Bob"]})
    seeded = profile.model_copy(update={"tables": [table]})
    with prepare_database(tmp_path / "seeded.db", seeded, resume=False) as connection:
        assert connection.execute('SELECT name FROM "people" ORDER BY name').fetchall() == [
            ("Alice",),
            ("Bob",),
        ]
