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
from localizer.application.local_build import ResourceBuild
from localizer.application.translation_plan import TranslationPlanner
from localizer.domain.translation_unit import TranslationUnit
from localizer.migrations.legacy_tm import LegacyTMSynchronizer
from localizer.rules.validation import ValidationRule


def unit(path: str, key: str, source: str) -> TranslationUnit:
    return TranslationUnit(
        project_id="game",
        adapter_id="gettext",
        relative_path=path,
        logical_file=path,
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
        metadata={"msgid": key},
    )


class TranslationPlannerTests(unittest.TestCase):
    def test_source_conflict_does_not_invalidate_original_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_file = root / "a" / "messages.mo"
            source_file.parent.mkdir()
            source_file.write_bytes(b"resource")
            legacy = root / "history_tm.json"
            legacy.write_text(
                json.dumps(
                    {
                        "messages.mo": {
                            "menu/hello": {"ru": "Привет", "zh": "你好"}
                        },
                        "b/messages.mo": {
                            "menu/hello": {"ru": "Привет", "zh": "您好"}
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            current = unit("a/messages.mo", "menu/hello", "Привет")
            resource = ResourceBuild(object(), source_file, (current,))
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                report = LegacyTMSynchronizer(
                    tm,
                    project_id="game",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).sync(legacy)
                self.assertEqual(2, report.classifications["legacy_suspect"])
                plan = TranslationPlanner(
                    tm,
                    validation_rule=ValidationRule(),
                    global_exact_match="reviewed_or_legacy_converged",
                ).build((resource,))
            self.assertEqual(0, len(plan.pending))
            self.assertEqual("你好", plan.translations[current.stable_identity])
            self.assertEqual(1, plan.by_match_scope["legacy_coordinate_exact"])

    def test_legacy_convergence_is_used_only_after_coordinate_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_file = root / "new.mo"
            source_file.write_bytes(b"resource")
            legacy = root / "history_tm.json"
            legacy.write_text(
                json.dumps(
                    {
                        "a.mo": {"a": {"ru": "Победа", "zh": "胜利"}},
                        "b.mo": {"b": {"ru": "Победа", "zh": "胜利"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            current = unit("new.mo", "new", "Победа")
            resource = ResourceBuild(object(), source_file, (current,))
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                LegacyTMSynchronizer(
                    tm,
                    project_id="game",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).sync(legacy)
                plan = TranslationPlanner(
                    tm,
                    validation_rule=ValidationRule(),
                    global_exact_match="reviewed_or_legacy_converged",
                ).build((resource,))
            self.assertEqual("胜利", plan.translations[current.stable_identity])
            self.assertEqual(1, plan.by_match_scope["legacy_source_converged"])

    def test_plan_fingerprint_changes_when_source_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_file = root / "messages.po"
            source_file.write_bytes(b"revision-one")
            current = unit("messages.po", "hello", "Привет")
            resource = ResourceBuild(object(), source_file, (current,))
            with SQLiteTranslationMemory(root / "tm.sqlite3", read_only=True) as tm:
                planner = TranslationPlanner(
                    tm,
                    validation_rule=ValidationRule(),
                    global_exact_match="disabled",
                )
                first = planner.build((resource,))
                source_file.write_bytes(b"revision-two")
                second = planner.build((resource,))
            self.assertNotEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
