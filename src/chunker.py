"""Split long plain text into overlapping word chunks for sequential LLM extraction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    """One slice of the source text."""

    index: int
    text: str
    word_start: int
    word_end: int
    total_words: int

    @property
    def label(self) -> str:
        return f"chunk {self.index + 1} (words {self.word_start}-{self.word_end} of {self.total_words})"


def split_text_into_chunks(
    text: str,
    chunk_size_words: int,
    overlap_words: int,
) -> list[TextChunk]:
    """
    Split text by word count with overlap between consecutive chunks.

    Example with chunk_size=1000 and overlap=200:
      chunk 0 -> words 0..999
      chunk 1 -> words 800..1799   (200 words repeated from chunk 0)
    """
    words = text.split()
    total = len(words)

    if total == 0:
        return []

    if chunk_size_words <= 0:
        raise ValueError("chunk_size_words must be positive")

    if overlap_words < 0:
        raise ValueError("overlap_words cannot be negative")

    if overlap_words >= chunk_size_words:
        raise ValueError("overlap_words must be smaller than chunk_size_words")

    step = chunk_size_words - overlap_words
    chunks: list[TextChunk] = []
    chunk_index = 0

    for start in range(0, total, step):
        end = min(start + chunk_size_words, total)
        chunk_words = words[start:end]
        chunks.append(
            TextChunk(
                index=chunk_index,
                text=" ".join(chunk_words),
                word_start=start,
                word_end=end,
                total_words=total,
            )
        )
        chunk_index += 1

        if end >= total:
            break

    return chunks
