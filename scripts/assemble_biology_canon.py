#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble page-level smart extraction JSONs into a single canon")
    ap.add_argument("--input-dir", required=True, help="Directory containing grade*_p*.json files")
    ap.add_argument("--out", required=True, help="Output markdown canon path")
    ap.add_argument("--report-out", required=True, help="Output report JSON path")
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    files = sorted(in_dir.glob("grade*_p*.json"))
    if not files:
        raise SystemExit(f"no input json files found in {in_dir}")

    pages = []
    metas = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        metas.append({k: data.get(k) for k in ["grade", "source", "total_pages", "start_page", "end_page", "timings"]})
        for p in data.get("pages", []):
            pages.append(p)

    # unique by grade,page keep highest quality if duplicates
    best = {}
    for p in pages:
        key = (str(p.get("grade")), int(p.get("page")))
        if key not in best or float(p.get("quality_score", 0)) > float(best[key].get("quality_score", 0)):
            best[key] = p
    ordered = [best[k] for k in sorted(best.keys(), key=lambda x: (int(x[0]), x[1]))]

    by_grade: dict[str, list[dict]] = {}
    for p in ordered:
        by_grade.setdefault(str(p["grade"]), []).append(p)

    out = []
    out.append("# Biology Canon — Grades 9–12 (First 10 Pages Each, Smart Extract)\n")
    out.append("Quality-first extraction (Docling OCR/table/figure-aware with PyMuPDF fallback and page quality checks).\n")
    out.append("## Sources")
    for g in sorted(by_grade.keys(), key=lambda x: int(x)):
        src = None
        total = None
        for m in metas:
            if str(m.get("grade")) == g:
                src = src or m.get("source")
                total = total or m.get("total_pages")
        out.append(f"- **Grade {g} Biology:** {src} (total {total} pages, extracted 1-10)")
    out.append("\n---\n")

    for g in sorted(by_grade.keys(), key=lambda x: int(x)):
        out.append(f"# Grade {g} Biology (First 10 Pages)\n")
        for p in by_grade[g]:
            out.append(f"\n## Page {p['page']}\n")
            out.append(f"*Method: `{p.get('method')}` | Quality: {p.get('quality_score')} | Chars: {p.get('char_count')}*\n")
            for w in p.get("warnings") or []:
                out.append(f"> Warning: {w}\n")
            out.append((p.get("markdown") or "*(No extractable text on this page)*").rstrip() + "\n")
        out.append("\n---\n")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    report = {
        "status": "ok",
        "grades": sorted(by_grade.keys(), key=lambda x: int(x)),
        "page_count": len(ordered),
        "pages": [{"grade": p["grade"], "page": p["page"], "method": p.get("method"), "quality_score": p.get("quality_score"), "char_count": p.get("char_count"), "warnings": p.get("warnings", [])} for p in ordered],
        "inputs": [str(f) for f in files],
        "canon": str(out_path),
    }
    rep_path = Path(args.report_out)
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "pages": len(ordered), "canon": str(out_path), "report": str(rep_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
