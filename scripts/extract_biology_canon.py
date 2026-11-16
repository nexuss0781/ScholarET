#!/usr/bin/env python3
"""
Discover Grade 9-12 Biology textbooks from ethcocoders/Digital-Library, extract the
first 10 pages of each in parallel (4 jobs), and produce a single Markdown canon.

Uses PyMuPDF (pymupdf) for lightweight PDF text extraction.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


REPO_API_BASE = "https://api.github.com/repos/ethcocoders/Digital-Library/contents/Source/System"
RAW_BASE = "https://github.com/ethcocoders/Digital-Library/raw/main/Source/System"
GRADES = ["9", "10", "11", "12"]

# Known Grade 9-12 Biology textbook links (from Digital-Library tree)
KNOWN_BIOLOGY_URLS = {
    "9": f"{RAW_BASE}/Grade%209/Biology.pdf",
    "10": f"{RAW_BASE}/Grade%2010/Biology.pdf",
    "11": f"{RAW_BASE}/Grade%2011/Biology.pdf",
    "12": f"{RAW_BASE}/Grade%2012/Biology.pdf",
}


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "biology-canon-extractor"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_biology_url(grade: str) -> str:
    if grade in KNOWN_BIOLOGY_URLS:
        return KNOWN_BIOLOGY_URLS[grade]
    items = http_get_json(f"{REPO_API_BASE}/Grade%20{grade}?ref=main")
    for it in items:
        if it.get("type") == "file" and it.get("name", "").lower() == "biology.pdf":
            # Use github.com/raw URL for reliable LFS download
            return f"{RAW_BASE}/Grade%20{grade}/Biology.pdf"
    raise RuntimeError(f"Biology.pdf not found for Grade {grade}")


def extract_one(args: tuple[str, str, str, str, int, bool]) -> dict:
    grade, url, out_dir_s, download_dir_s, max_pages, keep_inputs = args
    out_dir = Path(out_dir_s)
    download_dir = Path(download_dir_s)
    out_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = download_dir / f"grade{grade}.pdf"
    md_path = out_dir / f"grade{grade}_first{max_pages}.md"

    t0 = time.perf_counter()
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        dl_s = 0.0
        cached = True
    else:
        urllib.request.urlretrieve(url, pdf_path)
        dl_s = time.perf_counter() - t0
        cached = False

    import pymupdf  # imported in worker

    t1 = time.perf_counter()
    doc = pymupdf.open(pdf_path)
    n = min(max_pages, len(doc))
    parts = []
    parts.append(f"# Grade {grade} Biology (First {n} Pages)\n")
    parts.append(f"- **Grade:** {grade}")
    parts.append(f"- **Source:** {url}")
    parts.append(f"- **Total pages in book:** {len(doc)}")
    parts.append(f"- **Extracted pages:** 1-{n}\n")
    for i in range(n):
        page = doc[i]
        parts.append(f"\n## Page {i+1}\n")
        try:
            txt = page.get_text("markdown")
        except Exception:
            txt = page.get_text("text")
        txt = (txt or "").strip()
        parts.append(txt + "\n" if txt else "*(No extractable text on this page)*\n")
    total_pages = len(doc)
    doc.close()
    md_path.write_text("\n".join(parts), encoding="utf-8")
    ex_s = time.perf_counter() - t1

    pdf_size = pdf_path.stat().st_size
    if not keep_inputs:
        try:
            pdf_path.unlink()
        except Exception:
            pass

    return {
        "grade": grade,
        "url": url,
        "pdf_path": str(pdf_path),
        "pdf_bytes": pdf_size,
        "md_path": str(md_path),
        "md_bytes": md_path.stat().st_size,
        "total_pages": total_pages,
        "extracted_pages": n,
        "download_s": round(dl_s, 4),
        "extract_s": round(ex_s, 4),
        "cached": cached,
        "input_kept": keep_inputs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract first N pages of Grade 9-12 Biology books into one canon.")
    ap.add_argument("--out-dir", default="out/biology_canon", help="Output directory for processed results")
    ap.add_argument("--download-dir", default=None, help="Directory for downloaded PDFs (default: out-dir)")
    ap.add_argument("--pages", type=int, default=10, help="Number of pages to extract per book")
    ap.add_argument("--workers", type=int, default=4, help="Parallel extraction jobs")
    ap.add_argument("--keep-inputs", action="store_true", help="Keep downloaded/input PDFs after extraction")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    download_dir = Path(args.download_dir) if args.download_dir else out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    t_total0 = time.perf_counter()

    print("Discovering Biology textbook links (Grades 9-12)...")
    links = {}
    for g in GRADES:
        url = find_biology_url(g)
        links[g] = url
        print(f"  Grade {g}: {url}")

    print(f"\nStarting parallel extraction: {len(GRADES)} books, first {args.pages} pages, {args.workers} workers...")
    print(f"Output dir: {out_dir} | Download dir: {download_dir} | Keep inputs: {args.keep_inputs}")
    jobs = [(g, links[g], str(out_dir), str(download_dir), args.pages, args.keep_inputs) for g in GRADES]
    results = []
    t_jobs0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(extract_one, j): j[0] for j in jobs}
        for fut in as_completed(futs):
            grade = futs[fut]
            try:
                res = fut.result()
                results.append(res)
                cache_note = " (cached)" if res.get("cached") else ""
                print(f"[done] Grade {res['grade']}: {res['pdf_bytes']} bytes, pages {res['extracted_pages']}/{res['total_pages']}, "
                      f"dl {res['download_s']}s{cache_note}, extract {res['extract_s']}s -> {res['md_path']}")
            except Exception as e:
                print(f"[error] Grade {grade}: {e}")
                raise
    t_jobs = round(time.perf_counter() - t_jobs0, 4)

    # deterministic order
    results.sort(key=lambda r: int(r["grade"]))

    canon_path = out_dir / "biology_grade9-12_first10_canon.md"
    canon_parts = []
    canon_parts.append("# Biology Canon — Grades 9–12 (First 10 Pages Each)\n")
    canon_parts.append("Extracted from [ethcocoders/Digital-Library](https://github.com/ethcocoders/Digital-Library/tree/main/Source/System) using PyMuPDF.\n")
    canon_parts.append("## Sources")
    for r in results:
        canon_parts.append(f"- **Grade {r['grade']} Biology:** {r['url']} (total {r['total_pages']} pages, extracted 1-{r['extracted_pages']})")
    canon_parts.append("\n---\n")
    for r in results:
        md_text = Path(r["md_path"]).read_text(encoding="utf-8")
        canon_parts.append(md_text.rstrip())
        canon_parts.append("\n---\n")
    canon_path.write_text("\n".join(canon_parts).rstrip() + "\n", encoding="utf-8")

    # Keep only the single processed canon output by default
    if not args.keep_inputs:
        for r in results:
            try:
                Path(r["md_path"]).unlink(missing_ok=True)
            except Exception:
                pass

    total_s = round(time.perf_counter() - t_total0, 4)
    report = {
        "status": "ok",
        "grades": GRADES,
        "pages_per_book": args.pages,
        "workers": args.workers,
        "timings_seconds": {"parallel_jobs_wall_s": t_jobs, "total_s": total_s},
        "books": results,
        "outputs": {"canon_markdown": str(canon_path), "canon_bytes": canon_path.stat().st_size},
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Biology canon report ===")
    print(f"Parallel jobs wall time: {t_jobs} s")
    print(f"Total time:              {total_s} s")
    print(f"Canon:                   {canon_path} ({canon_path.stat().st_size} bytes)")
    print(f"Report:                  {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
