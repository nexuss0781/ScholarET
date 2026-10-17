#!/usr/bin/env python3
"""Validate worker archives and publish their books as a Hugging Face dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import extract_source_books

DEFAULT_REPO_ID = "Nexuss0781/Ethiopian-Curriculum-Books"
WORKER_COUNT = 19
EXPECTED_BOOK_COUNT = 94
_CHUNK_SIZE = 1024 * 1024
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))", re.MULTILINE)
_HTML_LINK_RE = re.compile(r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(name: str, is_dir: bool) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    raw = name[:-1] if is_dir and name.endswith("/") else name
    parts = raw.split("/")
    if not raw or raw.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    if PurePosixPath(raw).is_absolute():
        raise ValueError(f"unsafe ZIP member path: {name!r}")
    return tuple(parts)


def _is_regular_or_directory(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted ZIP member is not allowed: {info.filename!r}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if info.is_dir():
        if kind not in (0, stat.S_IFDIR):
            raise ValueError(f"non-directory ZIP entry has directory name: {info.filename!r}")
    elif kind not in (0, stat.S_IFREG):
        raise ValueError(f"non-regular ZIP member is not allowed: {info.filename!r}")


def _consume_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, capture: bool = False) -> bytes:
    chunks: list[bytes] = []
    with archive.open(info, "r") as member:
        while True:
            chunk = member.read(_CHUNK_SIZE)
            if not chunk:
                break
            if capture:
                chunks.append(chunk)
    return b"".join(chunks)


def _asset_references(markdown: bytes) -> set[str]:
    text = markdown.decode("utf-8", errors="replace")
    candidates = [match.group(1) or match.group(2) for match in _MARKDOWN_LINK_RE.finditer(text)]
    candidates.extend(match.group(1) for match in _HTML_LINK_RE.finditer(text))
    found: set[str] = set()
    for candidate in candidates:
        target = unquote(candidate.strip())
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        normalized = posixpath.normpath(posixpath.join("book", parsed.path)).removeprefix("book/")
        if normalized == "assets" or normalized.startswith("assets/"):
            found.add(normalized)
    return found


def _check_asset_links(book_path: str, markdown: bytes, file_names: set[str]) -> None:
    book_dir = book_path.rsplit("/", 1)[0]
    for reference in _asset_references(markdown):
        resolved = posixpath.normpath(posixpath.join(book_dir, reference))
        if resolved not in file_names:
            raise ValueError(f"Markdown asset reference is missing: {book_path} -> {reference}")


def _validate_and_extract_members(
    inventory: dict[str, Any], archive_dir: Path, stage_root: Path
) -> tuple[list[Path], set[str], dict[str, int]]:
    archives = sorted(archive_dir.glob("scholaret-worker-*.zip"))
    if len(archives) != WORKER_COUNT:
        raise ValueError(f"expected exactly {WORKER_COUNT} worker ZIPs, found {len(archives)}")
    expected_by_worker = {
        worker_id: [book for book in inventory["books"] if book["worker_id"] == worker_id]
        for worker_id in range(1, WORKER_COUNT + 1)
    }
    all_file_names: set[str] = set()
    file_sizes: dict[str, int] = {}
    for worker_id, archive_path in enumerate(archives, start=1):
        if archive_path.name != f"scholaret-worker-{worker_id:02d}.zip":
            raise ValueError(f"unexpected or missing worker ZIP: {archive_path.name}")
        assigned = expected_by_worker[worker_id]
        expected_md = {
            f"Grade {b['grade']}/{b['book_name']}/{b['book_name']}.md" for b in assigned
        }
        allowed_prefixes = tuple(f"Grade {b['grade']}/{b['book_name']}/" for b in assigned)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                names: set[str] = set()
                files: dict[str, zipfile.ZipInfo] = {}
                directories: set[str] = set()
                for info in entries:
                    name = info.filename
                    if name in names:
                        raise ValueError(f"duplicate ZIP member name in {archive_path.name}: {name!r}")
                    names.add(name)
                    _is_regular_or_directory(info)
                    parts = _safe_member_name(name, info.is_dir())
                    normalized = "/".join(parts)
                    if info.is_dir():
                        directories.add(normalized)
                        continue
                    if normalized == "manifest.json":
                        files[normalized] = info
                    elif normalized in expected_md or (
                        normalized.startswith(allowed_prefixes)
                        and "/assets/" in normalized
                        and not normalized.endswith("/")
                    ):
                        files[normalized] = info
                    else:
                        raise ValueError(f"unexpected file {name!r} in {archive_path.name}")
                if "manifest.json" not in files:
                    raise ValueError(f"{archive_path.name} is missing root manifest.json")

                valid_dirs: set[str] = set()
                for filename in files:
                    if filename == "manifest.json":
                        continue
                    parent = PurePosixPath(filename).parent
                    while str(parent) != ".":
                        valid_dirs.add(parent.as_posix())
                        parent = parent.parent
                if not directories.issubset(valid_dirs):
                    extra = sorted(directories - valid_dirs)
                    raise ValueError(f"unexpected directory entry in {archive_path.name}: {extra[0]!r}")

                payloads: dict[str, bytes] = {}
                # Read every member, including the manifest, so zipfile checks each CRC.
                for info in entries:
                    capture = info.filename == "manifest.json" or info.filename in expected_md
                    payload = _consume_member(archive, info, capture=capture)
                    if capture and not info.is_dir():
                        payloads[info.filename] = payload
                present_md = {name for name in files if name.endswith(".md") and name != "manifest.json"}
                if present_md != expected_md:
                    raise ValueError(f"unexpected or missing Markdown in {archive_path.name}")
                for name in expected_md:
                    data = payloads[name]
                    if not data:
                        raise ValueError(f"empty Markdown {name} in {archive_path.name}")
                    _check_asset_links(name, data, set(files))
                manifest = json.loads(payloads["manifest.json"].decode("utf-8"))
                manifest_books = manifest.get("books")
                if not isinstance(manifest_books, list):
                    raise ValueError(f"invalid manifest books list in {archive_path.name}")
                for name, info in files.items():
                    if name == "manifest.json":
                        continue
                    if name in all_file_names:
                        raise ValueError(f"duplicate dataset path across archives: {name}")
                    all_file_names.add(name)
                    file_sizes[name] = info.file_size
        except (zipfile.BadZipFile, EOFError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot validate {archive_path.name}: {exc}") from exc

    if len(inventory["books"]) != EXPECTED_BOOK_COUNT:
        raise ValueError(f"expected {EXPECTED_BOOK_COUNT} inventory books, found {len(inventory['books'])}")

    # Extraction is a separate pass after every archive and every CRC has passed.
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            second_pass_names: set[str] = set()
            for info in archive.infolist():
                _is_regular_or_directory(info)
                _safe_member_name(info.filename, info.is_dir())
                if info.filename in second_pass_names:
                    raise ValueError(f"duplicate ZIP member appeared during extraction: {info.filename!r}")
                second_pass_names.add(info.filename)
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                if info.filename not in all_file_names:
                    raise ValueError(f"unexpected ZIP member appeared during extraction: {info.filename!r}")
                target = stage_root.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, _CHUNK_SIZE)
    return archives, all_file_names, file_sizes


def _dataset_card(inventory: dict[str, Any], repo_id: str) -> str:
    return f"""---
pretty_name: Ethiopian Curriculum Books
task_categories:
- text-retrieval
---

# Ethiopian Curriculum Books

A collection of **{inventory['book_count']}** extracted Ethiopian curriculum books, organized as `Grade N/Subject/Subject.md` with each book's local assets alongside its Markdown.

The files are provided as extracted from the source curriculum materials. See [`metadata/provenance.json`](metadata/provenance.json) and [`metadata/source_book_inventory.json`](metadata/source_book_inventory.json) for source revision, inventory, worker and run provenance. The worker ZIP archives are intentionally not included.

This dataset is published at [`{repo_id}`](https://huggingface.co/datasets/{repo_id}). Consult the upstream source and applicable rights information before reuse.
"""


def prepare_dataset(
    inventory_path: Path | str,
    archive_dir: Path | str,
    staging_dir: Path | str,
    repo_id: str = DEFAULT_REPO_ID,
    source_run_id: str | None = None,
    source_sha: str | None = None,
    remove_archives_after_extraction: bool = False,
) -> dict[str, Any]:
    """Validate all inputs, then produce a clean, byte-preserving dataset folder."""
    inventory_path = Path(inventory_path)
    archive_dir = Path(archive_dir)
    staging_dir = Path(staging_dir).absolute()
    # This existing validator checks all worker manifests, inventory hashes, and Markdown hashes.
    extract_source_books.validate_archives(inventory_path, archive_dir)
    inventory = extract_source_books.load_inventory(inventory_path)
    if inventory.get("book_count") != EXPECTED_BOOK_COUNT:
        raise ValueError(f"expected {EXPECTED_BOOK_COUNT} books, found {inventory.get('book_count')}")
    if staging_dir.is_symlink():
        raise ValueError("staging directory must not be a symlink")
    if staging_dir.exists() and (not staging_dir.is_dir() or any(staging_dir.iterdir())):
        raise ValueError(f"staging directory must be absent or empty: {staging_dir}")
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{staging_dir.name}.tmp-", dir=staging_dir.parent))
    try:
        archives, dataset_files, file_sizes = _validate_and_extract_members(inventory, archive_dir, temp_dir)
        metadata = temp_dir / "metadata"
        metadata.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inventory_path, metadata / "source_book_inventory.json")
        assignment_map = inventory.get("assignment_map_file")
        if assignment_map:
            shutil.copyfile(inventory_path.parent / assignment_map, metadata / assignment_map)
        manifest_dir = metadata / "worker_manifests"
        manifest_dir.mkdir()
        for archive_path in archives:
            with zipfile.ZipFile(archive_path) as archive:
                manifest_bytes = archive.read("manifest.json")
            (manifest_dir / f"{archive_path.stem}.json").write_bytes(manifest_bytes)

        source_run_id = source_run_id or os.environ.get("GITHUB_RUN_ID") or "unspecified"
        provenance = {
            "dataset_repo_id": repo_id,
            "source_run_id": source_run_id,
            "source_sha": source_sha,
            "source_repository": inventory["source_repository"],
            "source_revision": inventory["source_revision"],
            "inventory_sha256": _sha256_file(inventory_path),
            "book_count": inventory["book_count"],
            "worker_count": WORKER_COUNT,
            "worker_archives": [p.name for p in archives],
            "content_file_count": len(dataset_files),
            "content_bytes": sum(file_sizes.values()),
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (metadata / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (temp_dir / "README.md").write_text(_dataset_card(inventory, repo_id), encoding="utf-8")
        if staging_dir.exists():
            staging_dir.rmdir()
        temp_dir.replace(staging_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    if remove_archives_after_extraction:
        # All 19 archives, their manifests, member paths, payloads and CRCs have passed validation.
        for archive_path in archives:
            archive_path.unlink()
    return {
        "staging_dir": str(staging_dir),
        "book_count": inventory["book_count"],
        "content_file_count": len(dataset_files),
        "content_bytes": sum(file_sizes.values()),
        "content_files": sorted(dataset_files),
        "content_file_sizes": file_sizes,
    }


def _publish(staging_dir: Path, repo_id: str, expected: dict[str, Any]) -> None:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is required unless --prepare-only is used")
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import RepositoryNotFoundError
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is required for publishing; install it in the workflow") from exc

    api = HfApi(token=token)
    try:
        repo = api.repo_info(repo_id=repo_id, repo_type="dataset")
    except RepositoryNotFoundError:
        repo = None
    if repo is not None and bool(getattr(repo, "private", False)):
        raise ValueError(f"refusing to publish to pre-existing private dataset repository {repo_id}")
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)

    # Large-folder upload is resumable and chunks commits; README is finalized after content verification.
    api.upload_large_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=str(staging_dir),
        ignore_patterns=["README.md", ".cache/**"],
        num_workers=4,
    )
    expected_paths = set(expected["content_files"])
    for path in (staging_dir / "metadata").rglob("*"):
        if path.is_file():
            expected_paths.add(path.relative_to(staging_dir).as_posix())
    remote_paths = set(api.list_repo_files(repo_id=repo_id, repo_type="dataset"))
    missing = expected_paths - remote_paths
    managed_paths = {".gitattributes", "README.md"}
    unexpected = remote_paths - expected_paths - managed_paths
    if missing or unexpected:
        raise RuntimeError(
            f"remote content inventory mismatch before README: missing={len(missing)}, unexpected={len(unexpected)}"
        )
    ordered_paths = sorted(expected_paths)
    for offset in range(0, len(ordered_paths), 500):
        batch = ordered_paths[offset:offset + 500]
        details = api.get_paths_info(repo_id=repo_id, repo_type="dataset", paths=batch)
        remote_sizes = {item.path: item.size for item in details if getattr(item, "size", None) is not None}
        for path in batch:
            if remote_sizes.get(path) != (staging_dir / path).stat().st_size:
                raise RuntimeError(f"remote content size mismatch for {path}")
    api.upload_file(
        path_or_fileobj=str(staging_dir / "README.md"),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Finalize Ethiopian Curriculum Books dataset card",
    )
    final_paths = set(api.list_repo_files(repo_id=repo_id, repo_type="dataset"))
    if final_paths - {".gitattributes"} != expected_paths | {"README.md"}:
        raise RuntimeError("remote dataset inventory differs from the staged dataset")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--source-run-id", default=None)
    parser.add_argument("--source-sha", default=None)
    parser.add_argument("--prepare-only", action="store_true", help="validate and stage without Hugging Face upload")
    parser.add_argument("--remove-archives-after-extraction", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = prepare_dataset(
            args.inventory,
            args.archive_dir,
            args.staging_dir,
            args.repo_id,
            args.source_run_id,
            args.source_sha,
            args.remove_archives_after_extraction,
        )
        print(
            f"Prepared {result['book_count']} books and {result['content_file_count']} content files "
            f"({result['content_bytes']} bytes) at {result['staging_dir']}"
        )
        if not args.prepare_only:
            _publish(args.staging_dir, args.repo_id, result)
            print(f"Published and verified dataset {args.repo_id}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
