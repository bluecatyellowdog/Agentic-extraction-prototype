"""Read plain text from PDF files."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def read_pdf_text(pdf_path: Path) -> str:
    """
    Extract all text from a PDF and return one big string.

    Each page is separated by two newlines so page breaks stay visible.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pages: list[str] = []
    with pymupdf.open(pdf_path) as document:
        for page in document:
            pages.append(page.get_text("text"))

    return "\n\n".join(pages).strip()
