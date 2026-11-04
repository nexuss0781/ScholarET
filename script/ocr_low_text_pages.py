#!/usr/bin/env python3
"""OCR pages that have little or no extractable text (image-based/scanned pages).

For each page in book.md with < min_chars chars of text, render the page from
the original PDF at 200 DPI and run tesseract OCR to recover the text.

Usage:
    python3 script/ocr_low_text_pages.py book.md assets/ source.pdf [min_chars]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import pymupdf


def ocr_page_image(pix_bytes: bytes) -> str:
    """Run tesseract OCR on raw PNG bytes, return cleaned text."""
    import tempfile
    import os
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(pix_bytes)
            tf.flush()
            tmp_path = tf.name
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "-l", "eng", "--psm", "4"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        os.unlink(tmp_path)
        text = result.stdout.strip()
        text = re.sub(r"\s+", " ", text)
        return text
    except Exception as exc:
        print(f"  ! OCR failed: {exc}", file=sys.stderr)
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", help="book.md to update")
    parser.add_argument("assets_dir", help="assets/ directory (not used directly)")
    parser.add_argument("pdf", help="original source PDF for rendering pages")
    parser.add_argument("min_chars", type=int, nargs="?", default=200,
                        help="OCR pages with fewer than this many chars of text")
    args = parser.parse_args(argv)

    md_path = Path(args.markdown)
    pdf_path = Path(args.pdf)

    if not pdf_path.is_file():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    text = md_path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    doc = pymupdf.open(pdf_path)
    ocr_count = 0
    skipped = 0

    out: list[str] = []
    current_page = 0
    page_text_chars: dict[int, int] = {}

    # First pass: count text chars per page from existing markdown
    for line in lines:
        m = re.match(r"^## Page (\d+)\s*$", line)
        if m:
            current_page = int(m.group(1))
            page_text_chars[current_page] = 0
        elif current_page > 0 and line.strip() and not line.startswith("!["):
            # Count non-image, non-rule lines as text
            page_text_chars[current_page] = page_text_chars.get(current_page, 0) + len(line)

    # Second pass: insert OCR text for low-text pages
    current_page = 0
    for line in lines:
        out.append(line)
        m = re.match(r"^## Page (\d+)\s*$", line)
        if m:
            current_page = int(m.group(1))
            chars = page_text_chars.get(current_page, 0)
            if chars < args.min_chars and current_page <= doc.page_count:
                print(f"  OCR page {current_page} (only {chars} text chars)...")
                try:
                    page = doc[current_page - 1]
                    pix = page.get_pixmap(dpi=200)
                    png_bytes = pix.tobytes("png")
                    ocr_text = ocr_page_image(png_bytes)
                    if ocr_text and len(ocr_text) > 10:
                        out.append("")
                        out.append(f"[OCR recovered text from page {current_page}:]")
                        out.append(ocr_text)
                        out.append("")
                        ocr_count += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    print(f"  ! Could not OCR page {current_page}: {exc}", file=sys.stderr)
                    skipped += 1
            else:
                skipped += 1

    doc.close()

    md_path.write_text("\n".join(out), encoding="utf-8")
    print(f"OCR: processed {ocr_count} pages, skipped {skipped} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
