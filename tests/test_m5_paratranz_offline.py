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

from localizer.application.paratranz_sync import (
    ConflictReportWriter,
    ParaTranzItem,
    ParaTranzStagePolicy,
    ThreeWayParaTranzMerger,
    load_resolutions,
)
from localizer.domain.translation_unit import TranslationUnit


def item(key: str, translation: str, stage: int, *, origin: str = "remote"):
    return ParaTranzItem(key, f"source-{key}", translation, stage, origin)


class ParaTranzOfflineTests(unittest.TestCase):
    def test_p07_stage_import_trust_is_explicit(self) -> None:
        policy = ParaTranzStagePolicy()
        self.assertFalse(policy.can_use_as_automatic_tm(0))
        self.assertFalse(policy.can_use_as_automatic_tm(1))
        self.assertFalse(policy.can_use_as_automatic_tm(2))
        self.assertTrue(policy.can_use_as_automatic_tm(3))
        self.assertTrue(policy.can_use_as_automatic_tm(5))
        self.assertTrue(policy.can_use_as_automatic_tm(9))
        self.assertFalse(policy.can_use_as_automatic_tm(-1))
        self.assertTrue(policy.meaning(-1).hidden)

    def test_p07_maps_only_checked_reviewed_and_locked_to_formal_tm(self) -> None:
        policy = ParaTranzStagePolicy()
        for stage in (-1, 0, 1, 2, 3, 5, 9):
            with self.subTest(stage=stage):
                unit = TranslationUnit(
                    project_id="game",
                    adapter_id="paratranz_json",
                    relative_path="file.json",
                    logical_key=f"key-{stage}",
                    source_text="source",
                    translation="译文",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                    metadata={"stage": stage},
                )
                entry = policy.to_tm_entry(unit)
                self.assertEqual(stage in {3, 5, 9}, entry.is_formal)
                self.assertEqual(stage, entry.stage)

    def test_stage_zero_accepts_machine_candidate_only_as_stage_one(self) -> None:
        result = ThreeWayParaTranzMerger().merge(
            [item("a", "", 0)],
            [item("a", "机器候选", 0, origin="machine")],
            [item("a", "", 0)],
        )
        self.assertEqual(1, result.uploads[0].stage)
        self.assertEqual("机器候选", result.uploads[0].translation)

    def test_high_stage_and_hidden_remote_are_never_machine_overwritten(self) -> None:
        for stage in (2, 3, 5, 9, -1):
            with self.subTest(stage=stage):
                result = ThreeWayParaTranzMerger().merge(
                    [item("a", "旧远端", stage)],
                    [item("a", "机器新译", 0, origin="machine")],
                    [item("a", "人工远端", stage)],
                )
                self.assertEqual("人工远端", result.merged[0].translation)
                self.assertEqual((), result.uploads)

    def test_stage_one_nonempty_remote_is_preserved(self) -> None:
        result = ThreeWayParaTranzMerger().merge(
            [item("a", "旧", 1)],
            [item("a", "机器", 0, origin="machine")],
            [item("a", "远端未审核", 1)],
        )
        self.assertEqual("远端未审核", result.merged[0].translation)
        self.assertEqual((), result.uploads)

    def test_simultaneous_non_machine_changes_emit_json_and_csv_conflict(self) -> None:
        result = ThreeWayParaTranzMerger().merge(
            [item("a", "基线", 0)],
            [item("a", "本地决策", 0, origin="resolution")],
            [item("a", "远端变化", 0)],
        )
        self.assertEqual(1, len(result.conflicts))
        with tempfile.TemporaryDirectory() as temp:
            json_path, csv_path = ConflictReportWriter.write(Path(temp), result.conflicts)
            self.assertEqual("both_sides_changed", json.loads(json_path.read_text("utf-8"))[0]["reason"])
            self.assertIn("both_sides_changed", csv_path.read_text("utf-8"))



class ResolutionFileVersioningTests(unittest.TestCase):
    """裁决文件的版本协商必须真的存在（评估 R11）。

    `framework-implementation.md` 把「版本化 resolution 文件读取」列为 M5 已实现，
    但原实现从头到尾不读 `schema_version`，且全仓零调用方零测试 —— 单点假声明。
    这个文件决定「本地译文还是远端译文进制品」，格式一旦演进，旧读法会把新语义
    静默读错，产出看起来正常的制品。
    """

    def _write(self, temp: str, text: str) -> Path:
        path = Path(temp) / "resolutions.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_v1_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                temp,
                "schema_version: 1\nresolutions:\n  sid-1: local\n  sid-2: remote\n",
            )
            self.assertEqual(
                {"sid-1": "local", "sid-2": "remote"}, dict(load_resolutions(path))
            )

    def test_missing_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, "resolutions:\n  sid-1: local\n")
            with self.assertRaises(ValueError) as ctx:
                load_resolutions(path)
            self.assertIn("schema_version", str(ctx.exception))

    def test_future_version_is_refused_and_lists_what_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, "schema_version: 99\nresolutions: {}\n")
            with self.assertRaises(ValueError) as ctx:
                load_resolutions(path)
            self.assertIn("supported: 1", str(ctx.exception))

    def test_non_numeric_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, "schema_version: not-a-number\nresolutions: {}\n")
            with self.assertRaises(ValueError):
                load_resolutions(path)

    def test_flat_mapping_no_longer_silently_accepted(self) -> None:
        # 原来这里会把版本号本身当成一条裁决，抛出完全误导的
        # 「resolution values must be local or remote」。
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, "schema_version: 1\nsid-1: local\n")
            with self.assertRaises(ValueError) as ctx:
                load_resolutions(path)
            self.assertIn("resolutions", str(ctx.exception))

    def test_bad_resolution_value_names_the_offender(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, "schema_version: 1\nresolutions:\n  sid-1: mine\n")
            with self.assertRaises(ValueError) as ctx:
                load_resolutions(path)
            self.assertIn("mine", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
