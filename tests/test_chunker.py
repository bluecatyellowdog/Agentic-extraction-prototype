import pytest

from pdf2duckdb.chunker import split_text_into_chunks


def test_chunks_overlap_and_cover_all_words():
    text = " ".join(f"w{i}" for i in range(25))
    chunks = split_text_into_chunks(text, chunk_size_words=10, overlap_words=3)

    assert [(c.word_start, c.word_end) for c in chunks] == [(0, 10), (7, 17), (14, 24), (21, 25)]
    assert chunks[1].text.split()[:3] == chunks[0].text.split()[-3:]
    assert chunks[-1].text.split()[-1] == "w24"


def test_short_text_is_one_chunk():
    chunks = split_text_into_chunks("just a few words", chunk_size_words=10, overlap_words=2)
    assert len(chunks) == 1
    assert chunks[0].label == "chunk 1 (words 0-4 of 4)"


def test_empty_text_has_no_chunks():
    assert split_text_into_chunks("   ", chunk_size_words=10, overlap_words=2) == []


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, -1), (10, 10)])
def test_invalid_settings_raise(size, overlap):
    with pytest.raises(ValueError):
        split_text_into_chunks("some text", chunk_size_words=size, overlap_words=overlap)
