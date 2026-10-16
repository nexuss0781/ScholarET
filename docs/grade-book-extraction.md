# Grade 1–12 book extraction

This is a document-extraction workflow, **not model training**. It reuses the
Nexuss 19-worker pattern as a fixed parallel job matrix; every worker runs the
existing ScholarET PDF extractor against a disjoint set of books.

## Inventory snapshot

The checked-in inventory covers all **94 PDF books** under
`ethcocoders/Digital-Library/Source/System/Grade 1` through `Grade 12` at source
commit [`b118434680c19a9b8bd1832eafac79e74f65de5f`](https://github.com/ethcocoders/Digital-Library/commit/b118434680c19a9b8bd1832eafac79e74f65de5f).
The PDFs are Git LFS objects; the inventory records each book name, exact source
path, LFS SHA-256, byte size, grade, and worker assignment.

| Grade | Books |
|---:|---:|
| 1 | 5 |
| 2 | 6 |
| 3 | 5 |
| 4 | 5 |
| 5 | 5 |
| 6 | 5 |
| 7 | 9 |
| 8 | 8 |
| 9 | 12 |
| 10 | 12 |
| 11 | 11 |
| 12 | 11 |
| **Total** | **94** |

Inventory files:

- [`data/source_book_inventory.csv`](../data/source_book_inventory.csv) — readable
  book-name/location list with worker assignment.
- [`data/source_book_inventory.json`](../data/source_book_inventory.json) —
  machine-readable inventory and integrity metadata used by Actions.

The inventory is pinned to the source commit and verified against the Git LFS
object SHA-256 and exact byte size. If the library changes, regenerate both
files from the new commit before dispatching; stale or modified source objects
fail closed rather than being silently extracted.

## Worker assignment and workflow

Run **Extract Grade 1-12 Books (19 Workers)** from the ScholarET repository's
GitHub Actions tab using `workflow_dispatch`. The workflow runs unit tests,
builds ScholarET and MuPDF once, then launches exactly 19 parallel extraction
jobs (`00`–`18`, `max-parallel: 19`). The assignment uses deterministic
largest-processing-time-first balancing by each PDF's LFS byte size; ties are
resolved by source path and then worker ID. Each worker downloads only its own
books and verifies each download before invoking ScholarET.

The default `worker_ids` input runs all 19 workers. For a selective retry, pass
a JSON string array of missing IDs (for example `[`"`11`"`,`"`13`"`,`"`17`"`]`)
and set `reuse_run_id` to a prior run that still has its successful worker ZIPs.
The verification job combines those prior artifacts with the new worker ZIPs
and validates the full 19-archive set without re-extracting completed workers.
Worker jobs have a 360-minute cap; logs show each book before the compiled
ScholarET extractor starts and emit a heartbeat while extraction continues.

Each worker produces one archive, for exactly 19 ZIPs in a successful run. The
archive contains the extractor's Markdown and image assets in this structure:

```text
Grade 1/Amharic/Amharic.md
Grade 1/Amharic/assets/fig_*.png
Grade 2/English/English.md
...
manifest.json                 # ZIP-root worker/source manifest
```

The generated files are uploaded as individual, 14-day GitHub Actions artifacts
named `scholaret-grade-worker-00` through `scholaret-grade-worker-18`. The
inventory is uploaded separately as `scholaret-grade-book-inventory`. A final
verification job checks all 19 archives, their assigned Markdown files, and
worker manifests, then writes a complete per-worker report to the run summary.

No model, training dataset, optimizer, aggregation, or model-training workflow
is invoked.
