#!/usr/bin/env python3
"""Extract Digital-Library Grade 1–12 books with 19 independent ScholarET workers.

The checked-in inventory stores Git LFS object hashes and sizes. Workers fetch only
assigned source PDFs from the pinned public Digital-Library commit, verify every
object, run ScholarET's existing `extract` executable, and package Markdown and
its referenced image assets under grade/subject paths.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

WORKER_COUNT = 19
SOURCE_REPOSITORY = "ethcocoders/Digital-Library"
INVENTORY_SCHEMA_VERSION = 1
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GRADE_BOOK_PATH_RE = re.compile(
    r"^Source/System/Grade (1[0-2]|[1-9])/([^/]+)\.pdf$"
)
LFS_HEADER = "version https://git-lfs.github.com/spec/v1"


def _parse_grade_and_name(source_path: str) -> tuple[int, str]:
    match = GRADE_BOOK_PATH_RE.fullmatch(source_path)
    if not match:
        raise ValueError(
            "expected a PDF at Source/System/Grade 1..12/<book name>.pdf; "
            f"found {source_path!r}"
        )
    grade = int(match.group(1))
    book_name = match.group(2)
    if book_name in {".", ".."} or not book_name.strip():
        raise ValueError(f"invalid book name in source path {source_path!r}")
    return grade, book_name


def _read_source_object_metadata(path: Path) -> tuple[int, str]:
    """Return the object size and SHA-256 from an LFS pointer or real PDF."""
    with path.open("rb") as source:
        prefix = source.read(256)
        if prefix.startswith(LFS_HEADER.encode("ascii")):
            try:
                text = prefix.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError(f"malformed Git LFS pointer: {path}") from exc
            oid_match = re.search(r"(?m)^oid sha256:([0-9a-f]{64})$", text)
            size_match = re.search(r"(?m)^size ([0-9]+)$", text)
            if not oid_match or not size_match:
                raise ValueError(f"malformed Git LFS pointer: {path}")
            return int(size_match.group(1)), oid_match.group(1)

        # Also support a source checkout where Git LFS has already materialized
        # the real PDF rather than leaving pointer text in the worktree.
        digest = hashlib.sha256(prefix)
        total = len(prefix)
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        if not prefix.startswith(b"%PDF"):
            raise ValueError(f"source file is neither a Git LFS pointer nor PDF: {path}")
        return total, digest.hexdigest()


def _lpt_assignments(books: Iterable[dict[str, Any]]) -> tuple[dict[str, int], list[int]]:
    """Assign largest files first to the currently lightest of 19 workers."""
    loads = [0] * WORKER_COUNT
    assignments: dict[str, int] = {}
    ordered = sorted(
        books,
        key=lambda book: (-int(book["source_bytes"]), str(book["source_path"])),
    )
    for book in ordered:
        worker_id = min(range(WORKER_COUNT), key=lambda idx: (loads[idx], idx))
        source_path = str(book["source_path"])
        if source_path in assignments:
            raise ValueError(f"duplicate source path: {source_path}")
        assignments[source_path] = worker_id
        loads[worker_id] += int(book["source_bytes"])
    return assignments, loads


def build_inventory(source_root: Path | str, source_revision: str) -> dict[str, Any]:
    """Build an inventory from Digital-Library's checked-out Source/ directory."""
    if not COMMIT_SHA_RE.fullmatch(source_revision):
        raise ValueError("source revision must be a full 40-character Git commit SHA")
    root = Path(source_root).resolve()
    source_dir = root / "Source" if (root / "Source").is_dir() else root
    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist under {root}")

    books: list[dict[str, Any]] = []
    for path in sorted(source_dir.rglob("*.pdf"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_dir).as_posix()
        source_path = f"Source/{relative}"
        grade, book_name = _parse_grade_and_name(source_path)
        source_bytes, source_sha256 = _read_source_object_metadata(path)
        if source_bytes <= 0:
            raise ValueError(f"empty source PDF: {source_path}")
        books.append(
            {
                "grade": grade,
                "book_name": book_name,
                "source_path": source_path,
                "source_sha256": source_sha256,
                "source_bytes": source_bytes,
            }
        )

    if not books:
        raise ValueError(f"no Grade 1–12 PDFs found under {source_dir}")
    assignments, loads = _lpt_assignments(books)
    for book in books:
        book["worker_id"] = assignments[book["source_path"]]
    books.sort(key=lambda book: (int(book["grade"]), str(book["source_path"])))
    grade_counts = Counter(str(book["grade"]) for book in books)

    document = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": source_revision,
        "worker_count": WORKER_COUNT,
        "partition_algorithm": "largest-processing-time-first by Git LFS object bytes; ties by source path then worker id",
        "book_count": len(books),
        "total_source_bytes": sum(int(book["source_bytes"]) for book in books),
        "worker_source_bytes": loads,
        "grade_counts": {grade: grade_counts[grade] for grade in sorted(grade_counts, key=int)},
        "books": books,
    }
    validate_inventory(document)
    return document


def validate_inventory(document: dict[str, Any]) -> None:
    """Fail closed if the inventory is incomplete, unsafe, or not canonical."""
    if document.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise ValueError("unsupported inventory schema_version")
    if document.get("source_repository") != SOURCE_REPOSITORY:
        raise ValueError(f"inventory source must be {SOURCE_REPOSITORY}")
    source_revision = document.get("source_revision")
    if not isinstance(source_revision, str) or not COMMIT_SHA_RE.fullmatch(source_revision):
        raise ValueError("inventory source_revision must be a full 40-character commit SHA")
    if document.get("worker_count") != WORKER_COUNT:
        raise ValueError(f"worker_count must be exactly {WORKER_COUNT}")
    books = document.get("books")
    if not isinstance(books, list) or not books:
        raise ValueError("inventory books must be a non-empty list")

    seen: set[str] = set()
    normalized_books: list[dict[str, Any]] = []
    for book in books:
        if not isinstance(book, dict):
            raise ValueError("each inventory entry must be an object")
        source_path = book.get("source_path")
        if not isinstance(source_path, str) or source_path in seen:
            raise ValueError(f"missing or duplicate source_path: {source_path!r}")
        pure_path = PurePosixPath(source_path)
        if pure_path.is_absolute() or ".." in pure_path.parts or "\\" in source_path:
            raise ValueError(f"unsafe source path: {source_path!r}")
        grade, book_name = _parse_grade_and_name(source_path)
        if book.get("grade") != grade or book.get("book_name") != book_name:
            raise ValueError(f"grade/book_name does not match {source_path!r}")
        digest = book.get("source_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256 for {source_path!r}")
        source_bytes = book.get("source_bytes")
        if type(source_bytes) is not int or source_bytes <= 0:
            raise ValueError(f"invalid source_bytes for {source_path!r}")
        worker_id = book.get("worker_id")
        if type(worker_id) is not int or not (0 <= worker_id < WORKER_COUNT):
            raise ValueError(f"invalid worker_id for {source_path!r}")
        seen.add(source_path)
        normalized_books.append(book)

    if document.get("book_count") != len(normalized_books):
        raise ValueError("inventory book_count does not match books list")
    total_bytes = sum(int(book["source_bytes"]) for book in normalized_books)
    if document.get("total_source_bytes") != total_bytes:
        raise ValueError("inventory total_source_bytes does not match books list")
    counts = Counter(str(book["grade"]) for book in normalized_books)
    expected_counts = {grade: counts[grade] for grade in sorted(counts, key=int)}
    if document.get("grade_counts") != expected_counts:
        raise ValueError("inventory grade_counts does not match books list")

    assignments, loads = _lpt_assignments(normalized_books)
    for book in normalized_books:
        if book["worker_id"] != assignments[book["source_path"]]:
            raise ValueError(
                f"non-canonical worker assignment for {book['source_path']!r}"
            )
    if document.get("worker_source_bytes") != loads:
        raise ValueError("inventory worker_source_bytes does not match canonical assignments")
    worker_book_counts = Counter(int(book["worker_id"]) for book in normalized_books)
    empty = [idx for idx in range(WORKER_COUNT) if worker_book_counts[idx] == 0]
    if empty:
        raise ValueError(f"inventory leaves worker(s) without books: {empty}")


def write_inventory(document: dict[str, Any], json_path: Path | str, csv_path: Path | str) -> None:
    validate_inventory(document)
    json_file = Path(json_path)
    csv_file = Path(csv_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with csv_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "worker_id",
                "grade",
                "book_name",
                "source_path",
                "source_sha256",
                "source_bytes",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for book in document["books"]:
            writer.writerow({key: book[key] for key in writer.fieldnames})


def load_inventory(path: Path | str) -> dict[str, Any]:
    inventory_path = Path(path)
    try:
        document = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read inventory {inventory_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("inventory root must be a JSON object")
    validate_inventory(document)
    return document


def download_source_pdf(book: dict[str, Any], source_revision: str, destination: Path) -> None:
    """Fetch one raw Git LFS object, retrying and verifying its immutable hash."""
    if not COMMIT_SHA_RE.fullmatch(source_revision):
        raise ValueError("source revision must be a full commit SHA")
    quoted_path = urllib.parse.quote(str(book["source_path"]), safe="/")
    url = (
        f"https://github.com/{SOURCE_REPOSITORY}/raw/"
        f"{source_revision}/{quoted_path}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ScholarET-GradeBook-Extractor/1.0",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        },
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(3):
        digest = hashlib.sha256()
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
            expected_size = int(book["source_bytes"])
            expected_sha = str(book["source_sha256"])
            actual_sha = digest.hexdigest()
            if total != expected_size or actual_sha != expected_sha:
                raise ValueError(
                    f"source integrity mismatch for {book['source_path']}: "
                    f"got {total} bytes/{actual_sha}, expected "
                    f"{expected_size} bytes/{expected_sha}"
                )
            return
        except Exception as exc:
            destination.unlink(missing_ok=True)
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"failed to download verified source {book['source_path']} after 3 attempts: "
        f"{last_error}"
    ) from last_error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_worker(
    inventory_path: Path | str,
    worker_id: int,
    extractor_path: Path | str,
    output_dir: Path | str,
) -> Path:
    """Process one deterministic shard and write exactly one worker ZIP."""
    inventory = load_inventory(inventory_path)
    if type(worker_id) is not int or not (0 <= worker_id < WORKER_COUNT):
        raise ValueError(f"worker_id must be between 0 and {WORKER_COUNT - 1}")
    assigned = [book for book in inventory["books"] if book["worker_id"] == worker_id]
    if not assigned:
        raise ValueError(f"worker {worker_id:02d} has no assigned books")

    extractor = Path(extractor_path).resolve()
    if not extractor.is_file():
        raise ValueError(f"ScholarET extractor executable not found: {extractor}")
    extractor.chmod(extractor.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    destination_dir = Path(output_dir).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive_path = destination_dir / f"scholaret-worker-{worker_id:02d}.zip"
    inventory_sha = _sha256_file(Path(inventory_path))

    with tempfile.TemporaryDirectory(
        prefix=f".scholaret-worker-{worker_id:02d}-", dir=str(destination_dir)
    ) as temporary:
        temporary_root = Path(temporary)
        source_dir = temporary_root / "source"
        package_dir = temporary_root / "package"
        extracted_books: list[dict[str, Any]] = []

        for book in assigned:
            source_relative = PurePosixPath(str(book["source_path"]))
            pdf_path = source_dir.joinpath(*source_relative.parts)
            book_dir = package_dir / f"Grade {book['grade']}" / str(book["book_name"])
            print(
                f"Worker {worker_id:02d}: fetching Grade {book['grade']} / "
                f"{book['book_name']} ({book['source_bytes']} bytes)"
            )
            download_source_pdf(book, inventory["source_revision"], pdf_path)
            book_dir.mkdir(parents=True, exist_ok=True)
            markdown_name = f"{book['book_name']}.md"
            command = [
                str(extractor),
                "--in",
                str(pdf_path),
                "--out",
                str(book_dir),
                "--md",
                markdown_name,
                "--threads",
                "0",
            ]
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.stdout:
                print(result.stdout[-4000:], end="" if result.stdout.endswith("\n") else "\n")
            if result.returncode != 0:
                raise RuntimeError(
                    f"ScholarET extraction failed for {book['source_path']} "
                    f"with exit code {result.returncode}"
                )

            markdown_path = book_dir / markdown_name
            if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
                raise RuntimeError(
                    f"ScholarET produced no Markdown for {book['source_path']}"
                )
            markdown_relative = markdown_path.relative_to(package_dir).as_posix()
            assets_dir = book_dir / "assets"
            asset_files = sorted(
                item.relative_to(package_dir).as_posix()
                for item in assets_dir.rglob("*")
                if item.is_file()
            ) if assets_dir.is_dir() else []
            extracted_books.append(
                {
                    "grade": int(book["grade"]),
                    "book_name": str(book["book_name"]),
                    "source_path": str(book["source_path"]),
                    "source_sha256": str(book["source_sha256"]),
                    "source_bytes": int(book["source_bytes"]),
                    "markdown_path": markdown_relative,
                    "markdown_sha256": _sha256_file(markdown_path),
                    "markdown_bytes": markdown_path.stat().st_size,
                    "asset_count": len(asset_files),
                }
            )
            pdf_path.unlink(missing_ok=True)

        archive_manifest = {
            "schema_version": 1,
            "source_repository": SOURCE_REPOSITORY,
            "source_revision": inventory["source_revision"],
            "source_inventory_sha256": inventory_sha,
            "worker_count": WORKER_COUNT,
            "worker_id": worker_id,
            "book_count": len(extracted_books),
            "books": extracted_books,
            "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        }
        package_manifest_path = package_dir / "manifest.json"
        package_manifest_path.write_text(
            json.dumps(archive_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as archive:
            for path in sorted(package_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(package_dir).as_posix())

    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise RuntimeError(f"worker archive was not created: {archive_path}")
    print(
        f"Worker {worker_id:02d}: packaged {len(assigned)} books to "
        f"{archive_path} ({archive_path.stat().st_size} bytes)"
    )
    return archive_path


def validate_archives(
    inventory_path: Path | str,
    archive_dir: Path | str,
    summary_path: Path | str | None = None,
) -> str:
    """Verify all 19 worker ZIPs contain exactly their assigned Markdown books."""
    inventory = load_inventory(inventory_path)
    folder = Path(archive_dir)
    archives = sorted(folder.glob("scholaret-worker-*.zip"))
    if len(archives) != WORKER_COUNT:
        raise ValueError(
            f"expected exactly {WORKER_COUNT} worker ZIPs in {folder}, found {len(archives)}"
        )

    expected_by_worker: dict[int, list[dict[str, Any]]] = {
        worker_id: [
            book for book in inventory["books"] if book["worker_id"] == worker_id
        ]
        for worker_id in range(WORKER_COUNT)
    }
    seen_workers: set[int] = set()
    seen_books: set[str] = set()
    worker_rows: list[tuple[int, int, int, str]] = []

    for archive_path in archives:
        match = re.fullmatch(r"scholaret-worker-(\d{2})\.zip", archive_path.name)
        if not match:
            raise ValueError(f"unexpected worker archive name: {archive_path.name}")
        worker_id = int(match.group(1))
        if worker_id in seen_workers or not (0 <= worker_id < WORKER_COUNT):
            raise ValueError(f"duplicate or invalid worker archive: {archive_path.name}")
        seen_workers.add(worker_id)
        assigned = expected_by_worker[worker_id]
        expected_by_path = {str(book["source_path"]): book for book in assigned}

        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = archive.namelist()
                if "manifest.json" not in names:
                    raise ValueError(f"{archive_path.name} has no root manifest.json")
                for name in names:
                    pure_name = PurePosixPath(name)
                    if pure_name.is_absolute() or ".." in pure_name.parts or "\\" in name:
                        raise ValueError(f"unsafe path in {archive_path.name}: {name!r}")
                try:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid manifest in {archive_path.name}") from exc
                if manifest.get("worker_id") != worker_id:
                    raise ValueError(f"worker ID mismatch in {archive_path.name}")
                if manifest.get("worker_count") != WORKER_COUNT:
                    raise ValueError(f"worker_count mismatch in {archive_path.name}")
                if manifest.get("source_revision") != inventory["source_revision"]:
                    raise ValueError(f"source revision mismatch in {archive_path.name}")
                if manifest.get("source_inventory_sha256") != _sha256_file(Path(inventory_path)):
                    raise ValueError(f"inventory hash mismatch in {archive_path.name}")
                if manifest.get("book_count") != len(assigned):
                    raise ValueError(f"book_count mismatch in {archive_path.name}")

                manifest_books = manifest.get("books")
                if not isinstance(manifest_books, list):
                    raise ValueError(f"invalid books list in {archive_path.name}")
                manifest_paths = [book.get("source_path") for book in manifest_books]
                if set(manifest_paths) != set(expected_by_path) or len(manifest_paths) != len(expected_by_path):
                    raise ValueError(f"assigned books mismatch in {archive_path.name}")

                files = [name for name in names if not name.endswith("/")]
                markdown_entries = {
                    name for name in files if name.lower().endswith(".md") and name != "manifest.json"
                }
                expected_markdown: set[str] = set()
                source_bytes = 0
                allowed_prefixes = tuple(
                    f"Grade {book['grade']}/{book['book_name']}/" for book in assigned
                )
                for entry in files:
                    if entry != "manifest.json" and not entry.startswith(allowed_prefixes):
                        raise ValueError(
                            f"unexpected file {entry!r} in worker archive {archive_path.name}"
                        )
                for book in assigned:
                    source_path = str(book["source_path"])
                    if source_path in seen_books:
                        raise ValueError(f"book appears in more than one archive: {source_path}")
                    seen_books.add(source_path)
                    grade = int(book["grade"])
                    name = str(book["book_name"])
                    expected_md = f"Grade {grade}/{name}/{name}.md"
                    expected_markdown.add(expected_md)
                    if expected_md not in names:
                        raise ValueError(f"missing Markdown {expected_md} in {archive_path.name}")
                    md_bytes = archive.read(expected_md)
                    if not md_bytes:
                        raise ValueError(f"empty Markdown {expected_md} in {archive_path.name}")
                    info = archive.getinfo(expected_md)
                    if info.file_size != len(md_bytes):
                        raise ValueError(f"Markdown size mismatch for {expected_md}")
                    manifest_book = next(
                        item for item in manifest_books if item.get("source_path") == source_path
                    )
                    if manifest_book.get("markdown_path") != expected_md:
                        raise ValueError(f"manifest Markdown path mismatch for {source_path}")
                    if manifest_book.get("markdown_bytes") != len(md_bytes):
                        raise ValueError(f"manifest Markdown size mismatch for {source_path}")
                    if manifest_book.get("markdown_sha256") != hashlib.sha256(md_bytes).hexdigest():
                        raise ValueError(f"manifest Markdown hash mismatch for {source_path}")
                    if manifest_book.get("source_sha256") != book["source_sha256"]:
                        raise ValueError(f"manifest source hash mismatch for {source_path}")
                    if manifest_book.get("source_bytes") != book["source_bytes"]:
                        raise ValueError(f"manifest source size mismatch for {source_path}")
                    asset_prefix = f"Grade {grade}/{name}/assets/"
                    actual_asset_count = sum(1 for entry in files if entry.startswith(asset_prefix))
                    if manifest_book.get("asset_count") != actual_asset_count:
                        raise ValueError(f"manifest asset count mismatch for {source_path}")
                    source_bytes += int(book["source_bytes"])
                if markdown_entries != expected_markdown:
                    raise ValueError(f"unexpected or missing Markdown in {archive_path.name}")
                worker_rows.append((worker_id, len(assigned), source_bytes, archive_path.name))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"invalid ZIP archive {archive_path}") from exc

    expected_workers = set(range(WORKER_COUNT))
    if seen_workers != expected_workers:
        raise ValueError(f"missing worker ZIPs for IDs: {sorted(expected_workers - seen_workers)}")
    if len(seen_books) != len(inventory["books"]):
        raise ValueError(
            f"validated {len(seen_books)} unique books but inventory has {len(inventory['books'])}"
        )

    worker_rows.sort()
    lines = [
        "## ScholarET Grade 1–12 extraction validation",
        "",
        f"- Source: `{SOURCE_REPOSITORY}` at `{inventory['source_revision']}`",
        f"- Books: **{len(inventory['books'])}** across Grades 1–12",
        f"- Workers: **{WORKER_COUNT}**; each worker produced one grade-organized ZIP",
        f"- Total source PDF bytes: **{inventory['total_source_bytes']}**",
        "",
        "| Worker | Books | Source MiB | ZIP artifact |",
        "|---:|---:|---:|---|",
    ]
    for worker_id, count, byte_count, archive_name in worker_rows:
        lines.append(
            f"| {worker_id:02d} | {count} | {byte_count / (1024 * 1024):.1f} | `{archive_name}` |"
        )
    summary = "\n".join(lines) + "\n"
    print(summary, end="")
    if summary_path:
        target = Path(summary_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(summary, encoding="utf-8")
    return summary


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="generate CSV and JSON inventories")
    inventory_parser.add_argument("--source-root", required=True, type=Path)
    inventory_parser.add_argument("--source-revision", required=True)
    inventory_parser.add_argument("--json-out", required=True, type=Path)
    inventory_parser.add_argument("--csv-out", required=True, type=Path)

    worker_parser = subparsers.add_parser("worker", help="run one of the 19 extraction workers")
    worker_parser.add_argument("--inventory", required=True, type=Path)
    worker_parser.add_argument("--worker-id", required=True, type=int)
    worker_parser.add_argument("--extractor", required=True, type=Path)
    worker_parser.add_argument("--output-dir", required=True, type=Path)

    validate_parser = subparsers.add_parser("validate", help="validate all 19 worker ZIPs")
    validate_parser.add_argument("--inventory", required=True, type=Path)
    validate_parser.add_argument("--archive-dir", required=True, type=Path)
    validate_parser.add_argument("--summary-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _create_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            document = build_inventory(args.source_root, args.source_revision)
            write_inventory(document, args.json_out, args.csv_out)
            print(
                f"Wrote {document['book_count']} books across "
                f"{len(document['grade_counts'])} grades; "
                f"{document['total_source_bytes']} source bytes; "
                f"{WORKER_COUNT} non-empty workers."
            )
        elif args.command == "worker":
            run_worker(args.inventory, args.worker_id, args.extractor, args.output_dir)
        elif args.command == "validate":
            validate_archives(args.inventory, args.archive_dir, args.summary_file)
        else:  # pragma: no cover - argparse enforces valid choices
            raise ValueError(f"unsupported command: {args.command}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
