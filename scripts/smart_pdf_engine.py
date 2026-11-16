#!/usr/bin/env python3
"""
Smart, quality-first PDF extraction engine.

Priorities: fidelity over speed. Uses layout-aware extraction (Docling when available),
OCR-aware processing, page-level quality checks, and fallback strategies for
garbled or image-heavy pages.

Designed for CI batch/matrix execution (per book/page-range jobs).
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


RAW_BASE = "https://github.com/ethcocoders/Digital-Library/raw/main/Source/System"
KNOWN_BIOLOGY_URLS = {
    "9": f"{RAW_BASE}/Grade%209/Biology.pdf",
    "10": f"{RAW_BASE}/Grade%2010/Biology.pdf",
    "11": f"{RAW_BASE}/Grade%2011/Biology.pdf",
    "12": f"{RAW_BASE}/Grade%2012/Biology.pdf",
}


@dataclass
class PageResult:
    grade: str
    page: int
    method: str
    quality_score: float
    char_count: int
    markdown: str
    warnings: list[str]


def download(url: str, dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    urllib.request.urlretrieve(url, dest)
    return time.perf_counter() - t0


def text_quality(text: str) -> float:
    t = text or ""
    if not t.strip():
        return 0.0
    chars = [c for c in t if not c.isspace()]
    if not chars:
        return 0.0
    good = sum(1 for c in chars if c.isalnum() or c in ".,;:!?()[]{}'\"-/\\@#$%&*+=<>|_~`…·•፡።፣፤፥፦፧፨" or '\u1200' <= c <= '\u137F')
    repl = sum(1 for c in chars if c in "\ufffd\uFFFD" or ord(c) < 32 and c not in "\n\t\r")
    # penalize control/private-use/odd replacement-like chars
    odd = sum(1 for c in chars if (0xE000 <= ord(c) <= 0xF8FF) or c == '�')
    base = good / max(1, len(chars))
    penalty = (repl + odd) / max(1, len(chars))
    score = max(0.0, min(1.0, base - penalty * 1.5))
    # very short but present text may be legit for image-heavy pages; don't over-penalize length alone
    if len(t.strip()) < 20:
        score *= 0.7
    return round(score, 4)


def extract_pymupdf_page(pdf_path: Path, page_no_1based: int) -> tuple[str, float, list[str]]:
    import pymupdf
    warnings: list[str] = []
    doc = pymupdf.open(pdf_path)
    idx = page_no_1based - 1
    if idx < 0 or idx >= len(doc):
        doc.close()
        return "", 0.0, [f"page {page_no_1based} out of range (1-{len(doc)})"]
    page = doc[idx]
    # try markdown then text
    text = ""
    try:
        text = page.get_text("markdown") or ""
        method_note = "pymupdf-markdown"
    except Exception:
        method_note = "pymupdf-text"
    if not text.strip():
        try:
            text = page.get_text("text") or ""
        except Exception as e:
            warnings.append(f"pymupdf text extraction failed: {e}")
            text = ""
    # also try blocks-based recovery if very poor
    q = text_quality(text)
    if q < 0.45:
        try:
            blocks = page.get_text("blocks") or []
            blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1), b[5] if len(b) > 5 else 0))
            bt = "\n".join((b[4] or "").strip() for b in blocks if len(b) > 4 and (b[6] == 0 if len(b) > 6 else True))
            if text_quality(bt) > q:
                text = bt
                q = text_quality(text)
                warnings.append("used pymupdf block reflow fallback")
        except Exception:
            pass
    doc.close()
    return text.strip(), q, warnings


def extract_docling_page(pdf_path: Path, page_no_1based: int) -> tuple[str, float, list[str], str]:
    warnings: list[str] = []
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except Exception as e:
        return "", 0.0, [f"docling unavailable: {e}"], "docling-unavailable"

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True
    try:
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    except Exception:
        pass
    pipeline_options.generate_page_images = False
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = 2.0

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )
    # convert only this page for focused quality work
    result = converter.convert(str(pdf_path), page_range=(page_no_1based, page_no_1based))
    doc = result.document
    try:
        # Prefer markdown export; include image refs as placeholders handled upstream if needed
        text = doc.export_to_markdown() or ""
    except Exception as e:
        warnings.append(f"docling markdown export failed: {e}")
        text = ""
    try:
        result.input.release()
    except Exception:
        pass
    q = text_quality(text)
    return text.strip(), q, warnings, "docling-ocr-tables"


def choose_best(grade: str, page: int, pym: tuple[str, float, list[str]], doc: tuple[str, float, list[str], str]) -> PageResult:
    pym_text, pym_q, pym_w = pym
    doc_text, doc_q, doc_w, doc_method = doc
    warnings = []
    warnings.extend([f"pymupdf: {w}" for w in pym_w])
    warnings.extend([f"docling: {w}" for w in doc_w])

    # Prefer docling when it is meaningfully better or comparable and structured; quality-first.
    # But if docling unavailable/empty, use pymupdf.
    if not doc_text.strip() and pym_text.strip():
        return PageResult(grade, page, "pymupdf-fallback", pym_q, len(pym_text), pym_text, warnings)
    if not pym_text.strip() and doc_text.strip():
        return PageResult(grade, page, doc_method, doc_q, len(doc_text), doc_text, warnings)
    if not doc_text.strip() and not pym_text.strip():
        return PageResult(grade, page, "none", 0.0, 0, "*(No extractable text on this page)*", warnings)

    # quality-first selection with bias toward docling for structure
    if doc_q >= pym_q - 0.05:
        chosen_text, chosen_q, method = doc_text, doc_q, doc_method
    else:
        chosen_text, chosen_q, method = pym_text, pym_q, "pymupdf"

    # if chosen still poor, mark warning
    if chosen_q < 0.45:
        warnings.append("low quality score after extraction; page may need manual review/VLM OCR")
    return PageResult(grade, page, method, chosen_q, len(chosen_text), chosen_text, warnings)


def main() -> int:
    ap = argparse.ArgumentParser(description="Smart quality-first PDF extraction engine")
    ap.add_argument("--grade", required=True, choices=["9", "10", "11", "12"])
    ap.add_argument("--page", type=int, default=None, help="Single 1-based page to extract")
    ap.add_argument("--start-page", type=int, default=None)
    ap.add_argument("--end-page", type=int, default=None)
    ap.add_argument("--out-dir", default="out/smart_extract")
    ap.add_argument("--cache-dir", default=".cache/smart_pdfs")
    ap.add_argument("--prefer", choices=["auto", "docling", "pymupdf"], default="auto")
    args = ap.parse_args()

    if args.page is not None:
        start = end = args.page
    else:
        start = args.start_page or 1
        end = args.end_page or start
    if start < 1 or end < start:
        raise SystemExit("invalid page range")

    grade = args.grade
    url = KNOWN_BIOLOGY_URLS[grade]
    out_dir = Path(args.out_dir)
    cache_dir = Path(args.cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / f"grade{grade}.pdf"

    timings: dict[str, Any] = {}
    t0 = time.perf_counter()
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        timings["download_s"] = 0.0
        timings["cached"] = True
    else:
        timings["download_s"] = round(download(url, pdf_path), 4)
        timings["cached"] = False

    # total pages
    try:
        import pymupdf
        d = pymupdf.open(pdf_path)
        total_pages = len(d)
        d.close()
    except Exception:
        total_pages = None

    results = []
    for p in range(start, end + 1):
        t1 = time.perf_counter()
        pym = extract_pymupdf_page(pdf_path, p)
        if args.prefer == "pymupdf":
            doc = ("", 0.0, ["skipped by prefer=pymupdf"], "docling-skipped")
        else:
            doc = extract_docling_page(pdf_path, p)
            if args.prefer == "docling" and not doc[0].strip():
                # still allow fallback in choose_best logic if needed
                pass
        pr = choose_best(grade, p, pym, doc)
        pr_dict = asdict(pr)
        pr_dict["elapsed_s"] = round(time.perf_counter() - t1, 4)
        results.append(pr_dict)

    timings["total_s"] = round(time.perf_counter() - t0, 4)
    payload = {
        "grade": grade,
        "source": url,
        "pdf_path": str(pdf_path),
        "total_pages": total_pages,
        "start_page": start,
        "end_page": end,
        "timings": timings,
        "pages": results,
    }

    out_json = out_dir / f"grade{grade}_p{start:03d}-{end:03d}.json"
    out_md = out_dir / f"grade{grade}_p{start:03d}-{end:03d}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_parts = [f"# Grade {grade} Biology — Smart Extract (Pages {start}-{end})\n",
                f"- **Source:** {url}",
                f"- **Total pages in book:** {total_pages}",
                f"- **Engine:** smart_pdf_engine (quality-first)",
                f"- **Cached PDF:** {timings.get('cached')}\n"]
    for r in results:
        md_parts.append(f"\n## Page {r['page']}\n")
        md_parts.append(f"*Method: `{r['method']}` | Quality: {r['quality_score']} | Chars: {r['char_count']}*\n")
        if r.get("warnings"):
            for w in r["warnings"]:
                md_parts.append(f"> Warning: {w}\n")
        md_parts.append((r.get("markdown") or "").rstrip() + "\n")
    out_md.write_text("\n".join(md_parts), encoding="utf-8")

    print(json.dumps({"status": "ok", "grade": grade, "range": [start, end], "json": str(out_json), "md": str(out_md), "timings": timings}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
