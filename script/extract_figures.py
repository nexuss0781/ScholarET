#!/usr/bin/env python3
"""Extract the figures embedded in a PDF and wire them into book.md.

This is the production replacement for the old C++ image path, which rendered a
page region for every image *instance* it saw. On books whose figures are built
from thousands of inline glyph tiles that meant thousands of tiny slivers and,
on the Amharic book, a full-page re-render per tile (effectively a hang).

PyMuPDF's ``page.get_images()`` lists the image XObjects in the page resources,
so each embedded image is decoded once at its real resolution. Inline images are
only considered when they are large both in pixels and on the page; the 3.8pt
glyph tiles that triggered the old behaviour are ignored by construction.

Output layout (relative to --out, which is the per-book folder)::

    book.md
    assets/fig-p001-i00.png
    assets/fig-p012-i01.png

The script appends/replaces a "## Figures" section in book.md, then verifies
that every generated markdown link resolves to a non-empty file.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# A real figure has to be more than a spacer/icon. The Amharic glyph tiles are
# 16x16 px drawn at 3.8pt; these thresholds reject them on both axes.
MIN_PIXEL_DIM = 24
MIN_PIXEL_AREA = 48 * 48
MIN_DRAWN_PT = 24.0
INLINE_MIN_PIXEL_DIM = 200
INLINE_MIN_DRAWN_PT = 100.0
# Drop extreme thin strips (fraction bars, rules, underline fragments) that are
# embedded as image objects but are not figures. A real wide diagram such as
# 1707x491 (aspect 3.5) is kept; 2942x114 (aspect 26) is dropped.
MAX_ASPECT = 24
# A saved PNG smaller than this is almost certainly noise, not a figure.
MIN_SAVE_BYTES = 1200
# Inline-image enumeration (get_image_info) is quadratic on pages that tile a
# picture from thousands of tiny inline images: one Amharic page took 47 s for
# 9,719 tiles. The content stream reveals the count in milliseconds, so pages
# above this limit are treated as glyph/tile pages and skipped.
INLINE_MAX_CANDIDATES = 32

# A fully uniform bitmap is a blank page background, not a figure.
BLANK_RANGE = 4
BLANK_SAMPLES = 4096


@dataclass
class Figure:
    page: int
    index: int
    filename: str
    width: int
    height: int
    xref: int
    inline: bool = False


def _is_blank(pix: pymupdf.Pixmap) -> bool:
    """Return True when the sampled colour channels are effectively constant."""
    samples = pix.samples
    comps = max(1, pix.n - pix.alpha)
    total = pix.width * pix.height
    if total <= 0 or not samples:
        return True
    step = max(1, total // BLANK_SAMPLES)
    lo = 255
    hi = 0
    for px in range(0, total, step):
        base = px * pix.n
        for c in range(comps):
            value = samples[base + c]
            if value < lo:
                lo = value
            if value > hi:
                hi = value
        if hi - lo > BLANK_RANGE:
            return False
    return True


def _save_pixmap(pix: pymupdf.Pixmap, target: Path) -> None:
    """Save a pixmap as PNG, converting CMYK/indexed colour to gray or RGB."""
    comps = pix.n - pix.alpha
    colorspace = pix.colorspace
    if comps in (1, 3) and colorspace is not None and "Indexed" not in str(colorspace):
        pix.save(target)
        return
    try:
        gray = pymupdf.Pixmap(pymupdf.csGRAY, pix)
        gray.save(target)
        return
    except Exception:
        pass
    rgb = pymupdf.Pixmap(pymupdf.csRGB, pix)
    rgb.save(target)


def _decode_inline(data: bytes, ext: str) -> pymupdf.Pixmap:
    doc = pymupdf.open(stream=data, filetype=(ext or "png"))
    try:
        return doc[0].get_pixmap()
    finally:
        doc.close()


def _inline_candidates(page: pymupdf.Page) -> list[dict]:
    """Large inline images, identified from the cheap image-info pass.

    ``page.read_contents()`` is fast even for multi-megabyte streams. Counting
    ``BI`` operators there lets us avoid ``get_image_info()`` on tile pages,
    where it is catastrophically slow.
    """
    try:
        inline_count = page.read_contents().count(b"BI")
    except Exception:
        return []
    if inline_count == 0 or inline_count > INLINE_MAX_CANDIDATES:
        return []
    out = []
    for info in page.get_image_info(xrefs=True):
        if info.get("xref", 0) != 0:
            continue
        width = int(info.get("width", 0))
        height = int(info.get("height", 0))
        bbox = info.get("bbox") or (0, 0, 0, 0)
        drawn_w = bbox[2] - bbox[0]
        drawn_h = bbox[3] - bbox[1]
        if width < INLINE_MIN_PIXEL_DIM or height < INLINE_MIN_PIXEL_DIM:
            continue
        if drawn_w < INLINE_MIN_DRAWN_PT or drawn_h < INLINE_MIN_DRAWN_PT:
            continue
        out.append(info)
    return out


def _inline_payloads(page: pymupdf.Page, wanted: list[dict]) -> list[dict]:
    """Fetch the bytes for wanted inline images via one structured-text pass."""
    if not wanted:
        return []
    blocks = [
        b
        for b in page.get_text("dict")["blocks"]
        if b.get("type") == 1 and b.get("image")
    ]
    matches = []
    used: set[int] = set()
    for info in wanted:
        ib = info.get("bbox") or (0, 0, 0, 0)
        best = None
        best_d = 1e9
        for i, block in enumerate(blocks):
            if i in used:
                continue
            bb = block.get("bbox") or (0, 0, 0, 0)
            d = abs(bb[0] - ib[0]) + abs(bb[1] - ib[1]) + abs(bb[2] - ib[2]) + abs(bb[3] - ib[3])
            if d < best_d:
                best_d = d
                best = (i, block)
        if best is not None and best_d <= 4.0:
            used.add(best[0])
            matches.append(best[1])
    return matches


def extract(pdf_path: Path, out_dir: Path) -> list[Figure]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    figures: list[Figure] = []
    seen_xrefs: set[int] = set()
    skipped = {"small": 0, "mask": 0, "blank": 0, "error": 0}
    try:
        for page_number, page in enumerate(doc, start=1):
            entries = page.get_images(full=True)
            mask_xrefs = {int(e[1]) for e in entries if len(e) > 1 and e[1]}

            for image_index, entry in enumerate(entries):
                xref = int(entry[0])
                if xref <= 0 or xref in seen_xrefs:
                    continue
                if xref in mask_xrefs:
                    skipped["mask"] += 1
                    continue
                width = int(entry[2])
                height = int(entry[3])
                if (
                    width < MIN_PIXEL_DIM
                    or height < MIN_PIXEL_DIM
                    or width * height < MIN_PIXEL_AREA
                    or max(width, height) / max(1, min(width, height)) > MAX_ASPECT
                ):
                    skipped["small"] += 1
                    continue

                filename = f"fig-p{page_number:03d}-i{image_index:02d}.png"
                target = assets / filename
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if _is_blank(pix):
                        skipped["blank"] += 1
                        continue
                    _save_pixmap(pix, target)
                except Exception as exc:  # keep one bad image from failing a book
                    skipped["error"] += 1
                    print(
                        f"  ! page {page_number} xref {xref}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if target.stat().st_size < MIN_SAVE_BYTES:
                    target.unlink(missing_ok=True)
                    skipped["small"] += 1
                    continue

                seen_xrefs.add(xref)
                figures.append(
                    Figure(page_number, image_index, filename, width, height, xref)
                )

            # Inline images are rare in these books; only sizeable ones count.
            wanted = _inline_candidates(page)
            for payload in _inline_payloads(page, wanted):
                width = int(payload.get("width", 0))
                height = int(payload.get("height", 0))
                if width < INLINE_MIN_PIXEL_DIM or height < INLINE_MIN_PIXEL_DIM:
                    continue
                filename = f"fig-p{page_number:03d}-x{len(figures):03d}.png"
                target = assets / filename
                try:
                    pix = _decode_inline(payload["image"], payload.get("ext", ""))
                    if _is_blank(pix):
                        skipped["blank"] += 1
                        continue
                    _save_pixmap(pix, target)
                except Exception as exc:
                    skipped["error"] += 1
                    print(
                        f"  ! inline page {page_number}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if target.stat().st_size < MIN_SAVE_BYTES:
                    target.unlink(missing_ok=True)
                    skipped["small"] += 1
                    continue
                figures.append(
                    Figure(page_number, len(figures), filename, width, height, 0, True)
                )
    finally:
        doc.close()

    print(
        f"figures: {len(figures)} saved; skipped small={skipped['small']} "
        f"mask={skipped['mask']} blank={skipped['blank']} error={skipped['error']}"
    )
    return figures


def _figures_section(figures: list[Figure]) -> str:
    lines = ["## Figures", ""]
    for fig in figures:
        alt = f"Figure from page {fig.page}"
        lines.append(
            f"- ![{alt}](assets/{fig.filename}) "
            f"_(Page {fig.page}, {fig.width}x{fig.height}, {fig.filename})_"
        )
    lines.append("")
    return "\n".join(lines)


_FIGURES_RE = re.compile(r"(?ms)^## Figures\s*\n.*?(?=^---\s*$|^## |\Z)")


def inject_markdown(md_path: Path, figures: list[Figure]) -> None:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    section = _figures_section(figures) if figures else ""

    if _FIGURES_RE.search(text):
        text = _FIGURES_RE.sub(section, text, count=1)
    elif section:
        # Place the list after the table of contents, immediately before the
        # ``---`` rule the extractor writes between front matter and body.
        marker = "\n---\n"
        pos = text.find(marker, 1)
        if pos == -1:
            h1 = text.find("\n", text.find("# ")) + 1
            text = text[:h1] + "\n" + section + "\n" + text[h1:]
        else:
            text = text[:pos] + "\n" + section + text[pos:]

    # Collapse the accidental blank runs this insertion can create.
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    md_path.write_text(text, encoding="utf-8")


_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def verify(md_path: Path, out_dir: Path) -> int:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    missing = []
    checked = 0
    for target in _LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "#")):
            continue
        checked += 1
        path = out_dir / target
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(target)
    if missing:
        print(f"MISSING/BROKEN: {len(missing)} figure link(s)", file=sys.stderr)
        for target in missing[:20]:
            print(f"  {target}", file=sys.stderr)
        return 1
    print(f"links verified: {checked}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--md", default="book.md")
    args = parser.parse_args(argv)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / args.md
    if not md_path.is_file():
        print(f"error: {md_path} does not exist; run the text extractor first", file=sys.stderr)
        return 2

    figures = extract(args.pdf, out_dir)
    inject_markdown(md_path, figures)
    return verify(md_path, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
