from pathlib import Path

import pytest
from pydantic import ValidationError

from pdf2duckdb.agents import build_system_prompt
from pdf2duckdb.profile import ExtractionProfile, builtin_profile_names, load_profile
from pdf2duckdb.schemas import build_chunk_result_model

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_builtin_profiles_load():
    assert "novel" in builtin_profile_names()
    for name in builtin_profile_names():
        profile = load_profile(name)
        build_chunk_result_model(profile)
        assert profile.name == name


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "examples" / "profiles").glob("*.yaml")))
def test_example_profiles_load(path):
    profile = load_profile(path)
    build_chunk_result_model(profile)


def test_unknown_profile_lists_builtins():
    with pytest.raises(ValueError, match="novel"):
        load_profile("does_not_exist")


def test_output_model_has_one_field_per_table(profile):
    model = build_chunk_result_model(profile)
    assert set(model.model_fields) == {"people", "meetings", "notes"}

    result = model.model_validate({"meetings": [{"summary": "Lunch", "attendees": 3}]})
    assert result.meetings[0].attendees == "3"  # numbers are coerced to strings
    assert result.people == []


def test_system_prompt_includes_columns_and_allowed_values(profile):
    prompt = build_system_prompt(profile)
    assert "TABLE 'people'" in prompt
    assert "One of: happy, sad." in prompt
    assert "Append-only" in prompt


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"key": "missing"}, "is not a column"),
        ({"columns": [{"name": "id", "description": "x"}], "key": None}, "added automatically"),
        ({"seed_keys": ["a"], "key": None}, "need a key column"),
    ],
)
def test_invalid_table_specs_are_rejected(change, message):
    table = {
        "name": "things",
        "description": "Things",
        "key": "name",
        "columns": [{"name": "name", "description": "Name"}],
        **change,
    }
    with pytest.raises(ValidationError, match=message):
        ExtractionProfile.model_validate({"name": "p", "description": "d", "tables": [table]})


def test_append_merge_requires_varchar():
    column = {"name": "n", "description": "d", "type": "INTEGER", "merge": "append"}
    table = {"name": "t", "description": "d", "columns": [column]}
    with pytest.raises(ValidationError, match="VARCHAR"):
        ExtractionProfile.model_validate({"name": "p", "description": "d", "tables": [table]})
