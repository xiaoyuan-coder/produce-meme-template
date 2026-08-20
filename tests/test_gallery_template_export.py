from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_gallery_templates import ExportError, export_gallery_templates


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class GalleryTemplateExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.records = [
            load(ROOT / "fixtures/contracts/latest-gallery-samples/heart.expected.json"),
            load(ROOT / "fixtures/contracts/latest-gallery-samples/wedding.expected.json"),
        ]
        self.source = self.root / "gallery-template.json"
        self.source.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exports_one_formal_object_per_key_named_file(self) -> None:
        output = self.root / "单模板JSON"
        manifest_path = self.root / "交付清单.json"

        manifest = export_gallery_templates(
            self.source,
            output,
            manifest_path=manifest_path,
        )

        expected_names = {f"{record['key']}.json" for record in self.records}
        self.assertEqual(expected_names, {path.name for path in output.iterdir()})
        self.assertEqual(2, manifest["recordCount"])
        self.assertEqual([record["key"] for record in self.records], manifest["keys"])
        self.assertTrue(manifest_path.is_file())
        for record in self.records:
            self.assertEqual(record, load(output / f"{record['key']}.json"))

        repeated = export_gallery_templates(
            self.source,
            output,
            manifest_path=manifest_path,
        )
        self.assertEqual(manifest, repeated)

    def test_rejects_duplicate_keys_before_writing(self) -> None:
        self.source.write_text(
            json.dumps([self.records[0], self.records[0]], ensure_ascii=False),
            encoding="utf-8",
        )
        output = self.root / "单模板JSON"

        with self.assertRaisesRegex(ExportError, "重复 key"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "duplicate-manifest.json",
            )

        self.assertFalse(output.exists())

    def test_rejects_invalid_record_and_conflicting_existing_file(self) -> None:
        invalid = dict(self.records[0])
        invalid["cover"] = "file:///tmp/cover.png"
        self.source.write_text(
            json.dumps(invalid, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ExportError, "未通过当前正式 Gallery 合同"):
            export_gallery_templates(
                self.source,
                self.root / "invalid",
                manifest_path=self.root / "invalid-manifest.json",
            )

        self.source.write_text(
            json.dumps(self.records[0], ensure_ascii=False),
            encoding="utf-8",
        )
        output = self.root / "单模板JSON"
        output.mkdir()
        target = output / f"{self.records[0]['key']}.json"
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "已有不同内容"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "conflict-manifest.json",
            )

    def test_requires_a_manifest_outside_the_data_directory(self) -> None:
        with self.assertRaises(TypeError):
            export_gallery_templates(self.source, self.root / "missing-manifest")

    def test_data_directory_rejects_manifest_and_unexpected_files(self) -> None:
        output = self.root / "单模板JSON"
        output.mkdir()
        (output / "notes.txt").write_text("sidecar", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "交付范围外"):
            export_gallery_templates(
                self.source,
                output,
                manifest_path=self.root / "notes-manifest.json",
            )

        hidden_output = self.root / "hidden"
        hidden_output.mkdir()
        (hidden_output / ".DS_Store").write_text("sidecar", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "交付范围外"):
            export_gallery_templates(
                self.source,
                hidden_output,
                manifest_path=self.root / "hidden-manifest.json",
            )

        clean_output = self.root / "clean"
        with self.assertRaisesRegex(ExportError, "数据目录之外"):
            export_gallery_templates(
                self.source,
                clean_output,
                manifest_path=clean_output / "manifest.json",
            )


if __name__ == "__main__":
    unittest.main()
