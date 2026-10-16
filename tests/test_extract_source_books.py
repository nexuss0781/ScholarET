from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import extract_source_books as books  # noqa: E402


class ExtractSourceBooksTests(unittest.TestCase):
    def _make_source_tree(self, root: Path) -> tuple[Path, bytes]:
        payload = b"%PDF-synthetic-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        source = root / "Digital-Library"
        for index in range(books.WORKER_COUNT):
            grade = index % 12 + 1
            pdf = source / "Source" / "System" / f"Grade {grade}" / f"Book-{index:02d}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{digest}\n"
                f"size {len(payload)}\n",
                encoding="ascii",
            )
        return source, payload

    def _make_fake_extractor(self, root: Path) -> Path:
        extractor = root / "fake-extract"
        extractor.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "source = Path(args[args.index('--in') + 1])\n"
            "out = Path(args[args.index('--out') + 1])\n"
            "md = args[args.index('--md') + 1]\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / md).write_text('# Synthetic extracted book\\n\\n![figure](assets/figure.png)\\n', encoding='utf-8')\n"
            "assets = out / 'assets'\n"
            "assets.mkdir(exist_ok=True)\n"
            "(assets / 'figure.png').write_bytes(b'not-a-real-png-test-fixture')\n"
            "print(f'Input: {source.name}')\n"
            "print('OK: yes')\n",
            encoding="utf-8",
        )
        extractor.chmod(0o755)
        return extractor

    def _write_assignment_map(
        self,
        path: Path,
        source_books: list[dict],
        reverse: bool = True,
    ) -> None:
        workers = []
        total_bytes = 0
        for worker_id in range(1, books.WORKER_COUNT + 1):
            index = books.WORKER_COUNT - worker_id if reverse else worker_id - 1
            book = source_books[index]
            size = book["source_bytes"]
            workers.append(
                {
                    "worker_id": f"worker_{worker_id:02d}",
                    "file_count": 1,
                    "total_size_bytes": size,
                    "files": [
                        {"path": book["source_path"], "size_bytes": size}
                    ],
                }
            )
            total_bytes += size
        document = {
            "repository": f"https://github.com/{books.SOURCE_REPOSITORY}",
            "source_directory": "Source/",
            "size_basis": "Git LFS pointer object size (bytes)",
            "balancing_method": "test-only reversed assignment",
            "worker_count": books.WORKER_COUNT,
            "pdf_count": len(source_books),
            "total_size_bytes": total_bytes,
            "average_size_bytes_per_worker": total_bytes / books.WORKER_COUNT,
            "workers": workers,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def test_inventory_and_lpt_assignments_are_complete_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, _ = self._make_source_tree(root)
            revision = "b" * 40
            document = books.build_inventory(source_root, revision)
            self.assertEqual(document["book_count"], books.WORKER_COUNT)
            self.assertEqual(document["worker_count"], books.WORKER_COUNT)
            self.assertEqual(
                {entry["worker_id"] for entry in document["books"]},
                set(range(1, books.WORKER_COUNT + 1)),
            )
            again = books.build_inventory(source_root, revision)
            self.assertEqual(document, again)

            json_path = root / "inventory.json"
            csv_path = root / "inventory.csv"
            books.write_inventory(document, json_path, csv_path)
            loaded = books.load_inventory(json_path)
            self.assertEqual(loaded, document)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), books.WORKER_COUNT)
            self.assertEqual(rows[0]["source_path"].split("/")[-1], "Book-00.pdf")
            self.assertEqual(rows[0]["worker_id"], "1")

    def test_assignment_map_is_authoritative_and_hash_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, _ = self._make_source_tree(root)
            revision = "e" * 40
            base = books.build_inventory(source_root, revision)
            map_path = root / "data" / "pdf_worker_assignments.json"
            self._write_assignment_map(map_path, base["books"], reverse=True)

            document = books.build_inventory(source_root, revision, map_path)
            assignments, sizes, _, _ = books._load_worker_assignment_map(map_path)
            self.assertEqual(document["assignment_map_file"], map_path.name)
            self.assertEqual(
                document["assignment_map_sha256"],
                hashlib.sha256(map_path.read_bytes()).hexdigest(),
            )
            for book in document["books"]:
                self.assertEqual(book["worker_id"], assignments[book["source_path"]])
                self.assertEqual(book["source_bytes"], sizes[book["source_path"]])

            inventory_path = map_path.parent / "source_book_inventory.json"
            books.write_inventory(
                document,
                inventory_path,
                map_path.parent / "source_book_inventory.csv",
                map_path,
            )
            self.assertEqual(books.load_inventory(inventory_path), document)

            modified = json.loads(map_path.read_text(encoding="utf-8"))
            modified["balancing_method"] += " changed"
            map_path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                books.load_inventory(inventory_path)

    def test_checked_in_inventory_matches_the_supplied_94_pdf_map(self) -> None:
        inventory_path = ROOT / "data" / "source_book_inventory.json"
        map_path = ROOT / "data" / "pdf_worker_assignments.json"
        document = books.load_inventory(inventory_path)
        assignments, sizes, loads, _ = books._load_worker_assignment_map(map_path)
        self.assertEqual(document["book_count"], 94)
        self.assertEqual(len(assignments), 94)
        self.assertEqual(document["worker_source_bytes"], loads)
        for book in document["books"]:
            self.assertEqual(book["worker_id"], assignments[book["source_path"]])
            self.assertEqual(book["source_bytes"], sizes[book["source_path"]])

    def test_workers_write_one_grade_structured_zip_each_and_validator_accepts_all(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, payload = self._make_source_tree(root)
            revision = "c" * 40
            document = books.build_inventory(source_root, revision)
            inventory_path = root / "inventory.json"
            books.write_inventory(document, inventory_path, root / "inventory.csv")
            extractor = self._make_fake_extractor(root)
            output_dir = root / "worker-zips"

            def fake_download(book: dict, source_revision: str, destination: Path) -> None:
                self.assertEqual(source_revision, revision)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)

            with mock.patch.object(books, "download_source_pdf", side_effect=fake_download):
                archives = [
                    books.run_worker(inventory_path, worker_id, extractor, output_dir)
                    for worker_id in range(1, books.WORKER_COUNT + 1)
                ]

            self.assertEqual(len(archives), books.WORKER_COUNT)
            self.assertEqual(len(list(output_dir.glob("scholaret-worker-*.zip"))), books.WORKER_COUNT)
            summary = books.validate_archives(inventory_path, output_dir)
            self.assertIn("**19**", summary)
            self.assertIn("Books: **19**", summary)

            import zipfile

            for worker_id, archive_path in zip(
                range(1, books.WORKER_COUNT + 1), archives
            ):
                with zipfile.ZipFile(archive_path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["worker_id"], worker_id)
                    record = next(
                        item for item in document["books"] if item["worker_id"] == worker_id
                    )
                    markdown = (
                        f"Grade {record['grade']}/{record['book_name']}/"
                        f"{record['book_name']}.md"
                    )
                    self.assertIn(markdown, archive.namelist())
                    self.assertIn(
                        f"Grade {record['grade']}/{record['book_name']}/assets/figure.png",
                        archive.namelist(),
                    )

    def test_inventory_rejects_worker_count_other_than_nineteen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_root, _ = self._make_source_tree(Path(temporary))
            document = books.build_inventory(source_root, "d" * 40)
            document["worker_count"] = 18
            with self.assertRaisesRegex(ValueError, "exactly 19"):
                books.validate_inventory(document)


if __name__ == "__main__":
    unittest.main()
