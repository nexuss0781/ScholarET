#!/usr/bin/env python3
"""OCR extracted images to recover text trapped inside figures.

Selectively OCRs images that are likely to contain text (tall text boxes,
wide captions, learning-outcome boxes). Skips small icons and photos.

Usage:
    python3 script/ocr_images.py book.md assets/
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def likely_text_image(w: int, h: int) -> bool:
    """Heuristic: text boxes are tall-narrow or wide-short, not square/photos."""
    if w < 100 or h < 40:
        return False  # too small to be a text block
    ar = max(w, h) / max(1, min(w, h))
    # Text boxes: tall (ar > 2) or wide-short (ar > 3 and h < 300)
    if ar > 2.0:
        return True
    if ar > 3.0 and h < 350:
        return True
    return False


def ocr_image(path: Path) -> str:
    """Run tesseract OCR on a single image, return cleaned text."""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = result.stdout.strip()
        text = re.sub(r"\s+", " ", text)
        return text
    except Exception as exc:
        print(f"  ! OCR failed for {path.name}: {exc}", file=sys.stderr)
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", help="book.md to update")
    parser.add_argument("assets_dir", help="assets/ directory containing PNGs")
    args = parser.parse_args(argv)

    md_path = Path(args.markdown)
    assets = Path(args.assets_dir)

    text = md_path.read_text(encoding="utf-8", errors="replace")

    img_re = re.compile(
        r"!\[([^\]]*)\]\(assets/(fig-p(\d+)-i\d+\.png)\)\s*_\(Page (\d+),[^)]*\)_\n?"
    )

    ocr_insertions: dict[int, list[str]] = {}
    ocr_done = 0
    ocr_skipped = 0

    for m in img_re.finditer(text):
        img_file = m.group(2)
        page_num = int(m.group(4))
        img_path = assets / img_file
        if not img_path.is_file():
            continue

        # Quick size check before OCR
        try:
            from PIL import Image
            with Image.open(img_path) as im:
                w, h = im.size
        except Exception:
            continue
        if not likely_text_image(w, h):
            ocr_skipped += 1
            continue

        ocr_text = ocr_image(img_path)
        ocr_done += 1
        if not ocr_text or len(ocr_text) < 6:
            continue
        ocr_insertions.setdefault(page_num, []).append(ocr_text)

    print(f"OCR: tried {ocr_done} text-like images, skipped {ocr_skipped} non-text images")
    if not ocr_insertions:
        print("no image text recovered by OCR")
        return 0

    lines = text.split("\n")
    out: list[str] = []
    current_page = 0
    for line in lines:
        out.append(line)
        m = re.match(r"^## Page (\d+)\s*$", line)
        if m:
            current_page = int(m.group(1))
            if current_page in ocr_insertions:
                for ocr in ocr_insertions[current_page]:
                    out.append("")
                    out.append(f"[OCR recovered text from figure on page {current_page}:]")
                    out.append(f"> {ocr}")
                    out.append("")
                del ocr_insertions[current_page]

    md_path.write_text("\n".join(out), encoding="utf-8")
    print(f"OCR: inserted recovered text for {len(ocr_insertions)} page(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
