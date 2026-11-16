#!/usr/bin/env python3
"""
Download (if needed) and process an Amharic PDF with Docling, recording timing.

Example:
  python3 scripts/process_amharic_docling.py \
    --source "https://github.com/ethcocoders/Digital-Library/raw/main/Source/System/Grade%203/Amharic.pdf" \
    --output-dir out/amharic_docling
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


DEFAULT_SOURCE = (
    "https://github.com/ethcocoders/Digital-Library/raw/main/Source/System/Grade%203/Amharic.pdf"
)


def is_url(s: str) -> bool:
    p = urlparse(s)
    return p.scheme in ("http", "https")


def download(url: str, dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with urlopen(url) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description="Download and process a PDF with Docling, reporting timings.")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="Local path or URL to PDF")
    ap.add_argument("--output-dir", default="out/amharic_docling", help="Directory for outputs/report")
    ap.add_argument("--download-to", default=None, help="Where to save downloaded PDF (if source is URL)")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timings: dict[str, float | str | int | None] = {}
    total_t0 = time.perf_counter()

    source = args.source
    local_pdf: Path
    if is_url(source):
        local_pdf = Path(args.download_to) if args.download_to else out_dir / "input.pdf"
        print(f"Downloading: {source}")
        timings["download_s"] = round(download(source, local_pdf), 4)
        print(f"Downloaded to: {local_pdf} ({local_pdf.stat().st_size} bytes) in {timings['download_s']}s")
    else:
        local_pdf = Path(source)
        if not local_pdf.exists():
            raise SystemExit(f"Local source not found: {local_pdf}")
        timings["download_s"] = 0.0
        print(f"Using local file: {local_pdf} ({local_pdf.stat().st_size} bytes)")

    # Import here so download can succeed even if docling missing (for clearer error after)
    t_import0 = time.perf_counter()
    from docling.document_converter import DocumentConverter
    timings["import_s"] = round(time.perf_counter() - t_import0, 4)

    print("Processing with Docling...")
    t_conv0 = time.perf_counter()
    converter = DocumentConverter()
    result = converter.convert(str(local_pdf))
    timings["convert_s"] = round(time.perf_counter() - t_conv0, 4)
    doc = result.document

    t_exp0 = time.perf_counter()
    md_path = out_dir / "output.md"
    json_path = out_dir / "output.json"
    md_path.write_text(doc.export_to_markdown(), encoding="utf-8")
    json_path.write_text(json.dumps(doc.export_to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    timings["export_s"] = round(time.perf_counter() - t_exp0, 4)

    try:
        result.input.release()
    except Exception:
        pass

    timings["total_s"] = round(time.perf_counter() - total_t0, 4)
    timings["source"] = source
    timings["local_pdf"] = str(local_pdf)
    timings["pdf_bytes"] = local_pdf.stat().st_size
    timings["markdown_bytes"] = md_path.stat().st_size
    timings["json_bytes"] = json_path.stat().st_size

    report = {
        "status": "ok",
        "timings_seconds": timings,
        "outputs": {"markdown": str(md_path), "json": str(json_path)},
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Docling processing report ===")
    print(f"Source:        {source}")
    print(f"PDF:           {local_pdf} ({timings['pdf_bytes']} bytes)")
    print(f"Import:        {timings['import_s']} s")
    print(f"Download:      {timings['download_s']} s")
    print(f"Convert:       {timings['convert_s']} s")
    print(f"Export:        {timings['export_s']} s")
    print(f"Total:         {timings['total_s']} s")
    print(f"Output (md):   {md_path} ({timings['markdown_bytes']} bytes)")
    print(f"Output (json): {json_path} ({timings['json_bytes']} bytes)")
    print(f"Report:        {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
