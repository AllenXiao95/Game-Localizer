from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.migrations.tm_seed import (
    TMSeedImporter,
    TMSeedImportRefused,
    TMSeedLoader,
)
from localizer.rules.loader import load_validation_rule
from localizer.cli.main import app
from typer.testing import CliRunner


class TMSeedImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.loader = TMSeedLoader(
            project_id="example",
            source_locale="en-US",
            target_locale="zh-Hans",
            adapter_ids=("gettext",),
        )
        self.importer = TMSeedImporter(
            self.root / "tm.sqlite3",
            validation_rule=load_validation_rule(
                ROOT / "projects" / "example" / "rules.yaml",
                source_locale="en-US",
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_seed(self, name: str, entries: list, *, defaults=None) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    **({"defaults": defaults} if defaults else {}),
                    "entries": entries,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_single_file_defaults_are_expanded_to_stable_coordinates(self) -> None:
        seed = self.write_seed(
            "single.json",
            [
                {
                    "logical_key": "menu.start",
                    "source_text": "Start Game",
                    "translation": "开始游戏",
                }
            ],
            defaults={"adapter_id": "gettext", "relative_path": "ui/menu.po"},
        )
        units = self.loader.load(seed)
        report, entries = self.importer.analyze(units, source_files=(seed,))
        self.assertEqual((1, 1, 0), (report.total, report.accepted, report.rejected))
        self.assertEqual("ui/menu.po", entries[0].relative_path)
        self.assertEqual("menu.start", entries[0].logical_key)

    def test_multiple_seed_files_are_combined_and_duplicates_fail_validation(self) -> None:
        defaults = {"adapter_id": "gettext", "relative_path": "ui/menu.po"}
        first = self.write_seed(
            "first.json",
            [{"logical_key": "start", "source_text": "Start", "translation": "开始"}],
            defaults=defaults,
        )
        second = self.write_seed(
            "second.json",
            [{"logical_key": "start", "source_text": "Start", "translation": "启动"}],
            defaults=defaults,
        )
        units = self.loader.load_many((first, second))
        report, _entries = self.importer.analyze(units, source_files=(first, second))
        self.assertEqual(2, report.total)
        self.assertEqual(1, report.rejected)
        self.assertEqual(1, report.issue_counts["duplicate_coordinate"])

    def test_apply_requires_attestation_and_creates_formal_reviewed_tm(self) -> None:
        seed = self.write_seed(
            "apply.json",
            [{"logical_key": "exit", "source_text": "Exit", "translation": "退出"}],
            defaults={"adapter_id": "gettext", "relative_path": "ui/menu.po"},
        )
        units = self.loader.load(seed)
        with self.assertRaisesRegex(TMSeedImportRefused, "accepted-by"):
            self.importer.apply(units, accepted_by="", source_files=(seed,))
        report = self.importer.apply(
            units,
            accepted_by="owner",
            source_files=(seed,),
            report_path=self.root / "report.json",
        )
        self.assertTrue(report.applied)
        self.assertEqual("owner", report.accepted_by)
        with SQLiteTranslationMemory(self.root / "tm.sqlite3", read_only=True) as tm:
            entry = tm.lookup(
                units[0].stable_identity,
                source_fingerprint=units[0].source_fingerprint,
            )
        self.assertIsNotNone(entry)
        self.assertTrue(entry.is_formal)
        self.assertTrue(entry.human_authored)
        self.assertEqual("reviewed", entry.review_state)

    def test_invalid_placeholder_refuses_all_apply(self) -> None:
        seed = self.write_seed(
            "invalid.json",
            [
                {
                    "logical_key": "greeting",
                    "source_text": "Hello {name}",
                    "translation": "你好",
                }
            ],
            defaults={"adapter_id": "gettext", "relative_path": "ui/menu.po"},
        )
        units = self.loader.load(seed)
        report, _entries = self.importer.analyze(units)
        self.assertEqual(1, report.issue_counts["placeholder_mismatch"])
        with self.assertRaisesRegex(TMSeedImportRefused, "failed validation"):
            self.importer.apply(units, accepted_by="owner")
        self.assertFalse((self.root / "tm.sqlite3").exists())

    def test_seed_rejects_unknown_fields_and_unconfigured_adapter(self) -> None:
        unknown = self.write_seed(
            "unknown.json",
            [
                {
                    "adapter_id": "gettext",
                    "relative_path": "ui/menu.po",
                    "logical_key": "start",
                    "source_text": "Start",
                    "translation": "开始",
                    "surprise": True,
                }
            ],
        )
        with self.assertRaisesRegex(TMSeedImportRefused, "unknown fields"):
            self.loader.load(unknown)
        wrong = self.write_seed(
            "wrong.json",
            [
                {
                    "adapter_id": "paratranz_json",
                    "relative_path": "ui/menu.json",
                    "logical_key": "start",
                    "source_text": "Start",
                    "translation": "开始",
                }
            ],
        )
        with self.assertRaisesRegex(TMSeedImportRefused, "not configured"):
            self.loader.load(wrong)

    def test_cli_accepts_multiple_seed_files_in_one_dry_run(self) -> None:
        examples = ROOT / "docs" / "examples" / "tm-seed-multi"
        report = self.root / "cli-report.json"
        result = CliRunner().invoke(
            app,
            [
                "tm-import-seed",
                str(ROOT / "projects" / "example" / "project.yaml"),
                str(examples / "ui-menu.json"),
                str(examples / "item-names.json"),
                "--report",
                str(report),
            ],
        )
        self.assertEqual(0, result.exit_code, result.stdout)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(4, payload["total"])
        self.assertEqual(0, payload["rejected"])
        self.assertFalse(payload["applied"])


if __name__ == "__main__":
    unittest.main()
