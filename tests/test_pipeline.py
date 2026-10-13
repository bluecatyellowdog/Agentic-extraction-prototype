import asyncio
from dataclasses import replace

import duckdb
import pytest

from pdf2duckdb.pipeline import run_extraction_pipeline


def run(**kwargs):
    return asyncio.run(run_extraction_pipeline(**kwargs))


STORY = (
    "Alice Smith opened her bakery early. Bob Jones arrived with flour and news. "
    "Later on, Alice Smith confessed to being a spy. "
    "Nobody else came to the meeting that evening."
)


def respond(chunk_text: str) -> dict:
    """Fake extractor: reports people who appear, plus one hallucinated name."""
    people = [{"name": "Zed Quimby"}]
    for name in ("Alice Smith", "Bob Jones"):
        if name in chunk_text:
            job = "spy" if "spy" in chunk_text and name == "Alice Smith" else "baker"
            people.append({"name": name, "job": job})
    meetings = [{"summary": "A meeting"}] if "meeting" in chunk_text else []
    return {"people": people, "meetings": meetings}


def test_end_to_end(tmp_path, make_pdf, profile, settings, fake_model):
    result = run(
        pdf_path=make_pdf(STORY),
        profile=profile,
        db_path=tmp_path / "out.db",
        settings=settings,
        skip_wrap_up=True,
        model=fake_model(respond),
    )

    assert result.failed_chunks == []
    assert result.chunks_processed == result.chunks_total > 1
    assert result.table_row_counts["people"] == 2
    assert result.open_disputes == 1  # Alice: baker -> spy

    with duckdb.connect(str(result.db_path)) as conn:
        # Append-only rows: one per chunk that mentions a meeting, tagged with that chunk.
        meeting_chunks = conn.execute(
            "SELECT chunk_index FROM source_chunks WHERE text LIKE '%meeting%' ORDER BY 1"
        ).fetchall()
        assert conn.execute("SELECT chunk_index FROM meetings ORDER BY 1").fetchall() == (
            meeting_chunks
        )
        names = {row[0] for row in conn.execute('SELECT name FROM "people"').fetchall()}
        assert names == {"Alice Smith", "Bob Jones"}  # hallucinated name was dropped
        assert conn.execute('SELECT COUNT(*) FROM "source_chunks"').fetchone()[0] == (
            result.chunks_total
        )


def test_resume_skips_checkpointed_chunks(tmp_path, make_pdf, profile, settings, fake_model):
    pdf, db = make_pdf(STORY), tmp_path / "out.db"
    seen: list[str] = []

    def failing_after_first(chunk_text: str) -> dict:
        seen.append(chunk_text)
        if len(seen) > 1:
            raise RuntimeError("provider down")
        return respond(chunk_text)

    first = run(
        pdf_path=pdf,
        profile=profile,
        db_path=db,
        settings=settings,
        skip_wrap_up=True,
        max_retries=1,
        model=fake_model(failing_after_first),
    )
    assert first.chunks_processed == 1
    assert len(first.failed_chunks) == first.chunks_total - 1

    second = run(
        pdf_path=pdf,
        profile=profile,
        db_path=db,
        settings=settings,
        resume=True,
        skip_wrap_up=True,
        model=fake_model(respond),
    )
    assert second.chunks_skipped == 1
    assert second.chunks_processed == second.chunks_total - 1
    assert second.failed_chunks == []


def test_resume_with_different_settings_is_refused(
    tmp_path, make_pdf, profile, settings, fake_model
):
    pdf, db = make_pdf(STORY), tmp_path / "out.db"
    common = dict(pdf_path=pdf, profile=profile, db_path=db, skip_wrap_up=True)
    run(**common, settings=settings, model=fake_model(respond))

    with pytest.raises(ValueError, match="do not match"):
        run(
            **common,
            settings=replace(settings, chunk_size_words=20),
            resume=True,
            model=fake_model(respond),
        )
