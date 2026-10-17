from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import warnings
import zipfile
from types import SimpleNamespace
from unittest import mock
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import extract_source_books as source_books  # noqa: E402
from scripts import publish_hf_dataset as publisher  # noqa: E402


class PublishHFDatasetTests(unittest.TestCase):
    def _make_fixture(self, root: Path) -> tuple[Path, Path, list[dict]]:
        books = []
        for idx in range(publisher.EXPECTED_BOOK_COUNT):
            grade = idx % 12 + 1
            subject = f"Subject-{idx:03d}"
            books.append(
                {
                    "grade": grade,
                    "book_name": subject,
                    "source_path": f"Source/System/Grade {grade}/{subject}.pdf",
                    "source_sha256": hashlib.sha256(f"source-{idx}".encode()).hexdigest(),
                    "source_bytes": idx + 1,
                }
            )
        assignments, loads = source_books._lpt_assignments(books)
        for book in books:
            book["worker_id"] = assignments[book["source_path"]]
        counts = Counter(str(book["grade"]) for book in books)
        inventory = {
            "schema_version": 1,
            "source_repository": source_books.SOURCE_REPOSITORY,
            "source_revision": "a" * 40,
            "worker_count": publisher.WORKER_COUNT,
            "partition_algorithm": "synthetic balanced fixture",
            "book_count": len(books),
            "total_source_bytes": sum(item["source_bytes"] for item in books),
            "worker_source_bytes": loads,
            "grade_counts": {grade: counts[grade] for grade in sorted(counts, key=int)},
            "books": books,
        }
        inventory_path = root / "inventory.json"
        inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        source_books.validate_inventory(inventory)

        archive_dir = root / "archives"
        archive_dir.mkdir()
        inventory_hash = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        for worker_id in range(1, publisher.WORKER_COUNT + 1):
            assigned = [book for book in books if book["worker_id"] == worker_id]
            archive_books = []
            content: dict[str, bytes] = {}
            for book in assigned:
                base = f"Grade {book['grade']}/{book['book_name']}"
                md_path = f"{base}/{book['book_name']}.md"
                asset_path = f"{base}/assets/figure.bin"
                md = b"# Synthetic\n\n![figure](assets/figure.bin)\n"
                asset = f"asset-{book['book_name']}".encode()
                content[md_path] = md
                content[asset_path] = asset
                archive_books.append(
                    {
                        "grade": book["grade"],
                        "book_name": book["book_name"],
                        "source_path": book["source_path"],
                        "source_sha256": book["source_sha256"],
                        "source_bytes": book["source_bytes"],
                        "markdown_path": md_path,
                        "markdown_sha256": hashlib.sha256(md).hexdigest(),
                        "markdown_bytes": len(md),
                        "asset_count": 1,
                    }
                )
            manifest = {
                "schema_version": 1,
                "source_repository": source_books.SOURCE_REPOSITORY,
                "source_revision": inventory["source_revision"],
                "source_inventory_sha256": inventory_hash,
                "worker_count": publisher.WORKER_COUNT,
                "worker_id": worker_id,
                "book_count": len(assigned),
                "books": archive_books,
                "github_run_id": "test-run",
            }
            archive_path = archive_dir / f"scholaret-worker-{worker_id:02d}.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
                for name, data in content.items():
                    archive.writestr(name, data)
        return inventory_path, archive_dir, books

    def _read_manifest_and_files(self, archive_path: Path) -> tuple[dict, dict[str, bytes]]:
        with zipfile.ZipFile(archive_path) as archive:
            return json.loads(archive.read("manifest.json")), {
                name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
            }

    def _rewrite_archive(self, archive_path: Path, manifest: dict, files: dict[str, bytes], extra=None) -> None:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest).encode())
            for name, data in files.items():
                archive.writestr(name, data)
            if extra:
                extra(archive)

    def test_stages_organized_files_card_and_provenance_without_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory, archives, books = self._make_fixture(root)
            stage = root / "dataset"
            result = publisher.prepare_dataset(
                inventory,
                archives,
                stage,
                "Nexuss0781/Ethiopian-Curriculum-Books",
                "run-123",
                "b" * 40,
            )
            self.assertEqual(result["book_count"], 94)
            self.assertEqual(result["content_file_count"], 188)
            self.assertEqual(len(list(stage.rglob("*.zip"))), 0)
            book = books[0]
            base = stage / f"Grade {book['grade']}" / book["book_name"]
            self.assertEqual((base / f"{book['book_name']}.md").read_bytes(), b"# Synthetic\n\n![figure](assets/figure.bin)\n")
            self.assertEqual((base / "assets" / "figure.bin").read_bytes(), f"asset-{book['book_name']}".encode())
            self.assertTrue((stage / "README.md").is_file())
            self.assertIn("94", (stage / "README.md").read_text(encoding="utf-8"))
            self.assertEqual((stage / "metadata" / "source_book_inventory.json").read_bytes(), inventory.read_bytes())
            provenance = json.loads((stage / "metadata" / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance["source_run_id"], "run-123")
            self.assertEqual(provenance["source_sha"], "b" * 40)
            self.assertEqual(provenance["content_file_count"], 188)
            self.assertEqual(len(list((stage / "metadata" / "worker_manifests").glob("*.json"))), 19)

    def test_rejects_duplicate_archive_member_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory, archives, _ = self._make_fixture(root)
            target = archives / "scholaret-worker-01.zip"
            manifest, files = self._read_manifest_and_files(target)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self._rewrite_archive(
                    target,
                    manifest,
                    files,
                    lambda archive: archive.writestr("manifest.json", json.dumps(manifest).encode()),
                )
            with self.assertRaisesRegex(ValueError, "duplicate ZIP member"):
                publisher.prepare_dataset(inventory, archives, root / "stage", source_run_id="test")

    def test_rejects_traversal_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory, archives, _ = self._make_fixture(root)
            target = archives / "scholaret-worker-01.zip"
            manifest, files = self._read_manifest_and_files(target)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self._rewrite_archive(target, manifest, files, lambda archive: archive.writestr("../escape.txt", b"bad"))
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                publisher.prepare_dataset(inventory, archives, root / "stage", source_run_id="test")

    def test_rejects_markdown_reference_to_missing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory, archives, _ = self._make_fixture(root)
            target = archives / "scholaret-worker-01.zip"
            manifest, files = self._read_manifest_and_files(target)
            asset = next(name for name in files if "/assets/" in name)
            del files[asset]
            manifest["books"][0]["asset_count"] = 0
            self._rewrite_archive(target, manifest, files)
            with self.assertRaisesRegex(ValueError, "asset reference is missing"):
                publisher.prepare_dataset(inventory, archives, root / "stage", source_run_id="test")

    def _fake_hf_api(self, stage: Path, bad_size: bool = False):
        class FakeAPI:
            finalized = False
            uploaded = False

            def repo_info(self, *, repo_id, repo_type):
                return SimpleNamespace(private=False)

            def create_repo(self, *, repo_id, repo_type, private, exist_ok):
                assert private is False

            # Matches the supported arguments of huggingface_hub 0.36.2.
            def upload_large_folder(self, repo_id, folder_path, *, repo_type, ignore_patterns, num_workers):
                self.uploaded = True
                assert 'README.md' in ignore_patterns

            def list_repo_files(self, *, repo_id, repo_type):
                return ['.gitattributes'] + [p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()]

            def get_paths_info(self, *, repo_id, repo_type, paths):
                assert len(paths) <= 500
                return [SimpleNamespace(path=p, size=(stage / p).stat().st_size + int(bad_size)) for p in paths]

            def upload_file(self, *, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
                assert path_in_repo == 'README.md'
                self.finalized = True

        return FakeAPI()

    def _run_mock_publish(self, stage: Path, api) -> None:
        modules = {
            'huggingface_hub': SimpleNamespace(HfApi=lambda token: api),
            'huggingface_hub.errors': SimpleNamespace(RepositoryNotFoundError=FileNotFoundError),
        }
        expected = {'content_files': ['Grade 1/English/English.md']}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict('os.environ', {'HF_TOKEN': 'test-only-token'}):
            publisher._publish(stage, publisher.DEFAULT_REPO_ID, expected)

    def test_publish_accepts_hf_managed_files_and_existing_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            content = stage / 'Grade 1/English/English.md'
            content.parent.mkdir(parents=True)
            content.write_text('# Book\n')
            (stage / 'metadata').mkdir()
            (stage / 'metadata/provenance.json').write_text('{}')
            (stage / 'README.md').write_text('# Dataset\n')
            api = self._fake_hf_api(stage)
            self._run_mock_publish(stage, api)
            self.assertTrue(api.uploaded)
            self.assertTrue(api.finalized)

    def test_publish_does_not_finalize_card_if_remote_size_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            content = stage / 'Grade 1/English/English.md'
            content.parent.mkdir(parents=True)
            content.write_text('# Book\n')
            (stage / 'metadata').mkdir()
            (stage / 'README.md').write_text('# Dataset\n')
            api = self._fake_hf_api(stage, bad_size=True)
            with self.assertRaisesRegex(RuntimeError, 'size mismatch'):
                self._run_mock_publish(stage, api)
            self.assertFalse(api.finalized)


if __name__ == "__main__":
    unittest.main()
