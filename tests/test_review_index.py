"""审查索引（sidecar）：补齐 QARecord 缺的东西，且不碰 QARecord（W2）。

为什么是 sidecar：`debt_key` 是 `sid::code::sha256(canonical_json(details))[:12]`，
动 `details` 的形状会让**已登记的存量债基线 100% 失配**。索引与报告平行落盘。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from localizer.application.review_index import (
    INDEX_FILENAME,
    SCHEMA_VERSION,
    ReviewIndex,
    normalize_variant,
)
from test_qa_judgement_extraction import _Corpus

GOLDEN = ROOT / "tests" / "fixtures" / "qa-report-golden.json"


class _Built:
    def __init__(self, temp: str) -> None:
        corpus = _Corpus()
        self.result = corpus.build(corpus.pipeline(), Path(temp))
        self.reports = self.result.qa_json.parent
        self.index = ReviewIndex.load(self.reports / INDEX_FILENAME)
        self.corpus = corpus


class SidecarDoesNotTouchTheReportTests(unittest.TestCase):
    def test_qa_report_is_still_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = _Built(temp)
            produced = built.result.qa_json.read_bytes()
        self.assertEqual(
            hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
            hashlib.sha256(produced).hexdigest(),
        )

    def test_issue_key_set_is_frozen(self) -> None:
        # qa-report.json 的 issue 形状是 qa-accept-debt、棘轮与外部脚本的契约面。
        payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for issue in payload["issues"]:
            self.assertEqual(
                {
                    "code",
                    "severity",
                    "message",
                    "stable_identity",
                    "relative_path",
                    "details",
                    "provenance",
                },
                set(issue),
            )


class SidecarContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.built = _Built(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_schema_version_is_enforced(self) -> None:
        path = self.built.reports / INDEX_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = SCHEMA_VERSION + 99
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            ReviewIndex.load(path)
        self.assertIn("schema_version", str(ctx.exception))

    def test_every_qa_record_identity_is_in_units(self) -> None:
        report = json.loads(self.built.result.qa_json.read_text(encoding="utf-8"))
        for issue in report["issues"]:
            identity = issue["stable_identity"]
            if identity:
                self.assertIn(
                    identity,
                    self.built.index.units,
                    f"{issue['code']} 的词条不在索引里，审查视图就看不到它的源文",
                )

    def test_units_carry_source_and_translation(self) -> None:
        # 这正是 QARecord 缺的两样东西。
        for identity, unit in self.built.index.units.items():
            self.assertIn("source_text", unit)
            self.assertIn("translation", unit)
            self.assertIn("context", unit)
            self.assertTrue(unit["source_text"])

    def test_group_members_include_the_empty_translation(self) -> None:
        """QA 记录看不到空译文成员，索引必须看得到。

        `_consistency_issues` 有 `if not unit.translation: continue`。漏掉这些成员，
        「一键统一」就只会统一一半 —— 剩下的下一次运行会被模型重译出第 N 种译法，
        警告复活而操作者以为已经处理完了。
        """
        group = next(
            g for g in self.built.index.same_source_groups if g["source"] == "Общий текст"
        )
        self.assertEqual(2, group["variant_count"], "QA 只看到两个译法")
        self.assertEqual(3, group["member_count"], "真实分组是三个成员")
        self.assertTrue(group["has_empty_members"])
        self.assertIn("", [m["translation"] for m in group["members"]])

        # 反证：QA 记录里确实只有两个译法。
        report = json.loads(self.built.result.qa_json.read_text(encoding="utf-8"))
        record = next(
            i for i in report["issues"] if i["code"] == "same_source_inconsistency"
        )
        self.assertEqual(2, len(record["details"]["translations"]))

    def test_group_members_carry_coordinate_context_fields(self) -> None:
        group = next(
            g for g in self.built.index.same_source_groups if g["source"] == "Общий текст"
        )
        for member in group["members"]:
            unit = self.built.index.units[member["stable_identity"]]
            self.assertEqual(unit["logical_key"], member["logical_key"])
            self.assertIn("context", member)
            self.assertEqual(unit["context"], member["context"])

    def test_source_buckets_cover_every_unit(self) -> None:
        # 桶是「这次编辑刚造出一条新分歧」的唯一判据，必须覆盖全量而不只是问题词条。
        total = sum(b["n"] for b in self.built.index.source_buckets.values())
        self.assertEqual(self.built.index.payload["unit_total"], total)
        self.assertGreater(total, len(self.built.index.units))

    def test_glossary_violations_collapse_into_clusters(self) -> None:
        report = json.loads(self.built.result.qa_json.read_text(encoding="utf-8"))
        violations = [
            i for i in report["issues"] if i["code"] == "glossary_violation"
        ]
        pairs = {
            (i["details"]["source_term"], i["details"]["required_target"])
            for i in violations
        }
        self.assertEqual(len(pairs), len(self.built.index.glossary_clusters))
        cluster = self.built.index.glossary_clusters[0]
        self.assertEqual(
            len(violations), sum(c["violation_count"] for c in self.built.index.glossary_clusters)
        )
        # human + reviewed 的术语受 G01 绝对保护，UI 要据此禁用「直接改」。
        self.assertTrue(cluster["protected"])

    def test_lookup_helpers(self) -> None:
        group = self.built.index.same_source_groups[0]
        self.assertEqual(group, self.built.index.group_for(group["group_id"]))
        self.assertIsNone(self.built.index.group_for("nope"))
        cluster = self.built.index.glossary_clusters[0]
        self.assertEqual(cluster, self.built.index.cluster_for(cluster["cluster_id"]))


class NormalizeVariantTests(unittest.TestCase):
    def test_only_conservative_normalisation(self) -> None:
        self.assertEqual("坦克", normalize_variant(" 坦克。"))
        self.assertEqual("坦克", normalize_variant("坦克"))
        self.assertEqual("A B", normalize_variant("Ａ Ｂ"))
        # 不做「去掉全部标点空白」—— 「A、B」与「AB」在 UI 上是两个决策。
        self.assertNotEqual(normalize_variant("A、B"), normalize_variant("AB"))


class SidecarIsNotListedAsALogFileTests(unittest.TestCase):
    def test_hidden_from_run_files(self) -> None:
        """索引真机可达数 MB，而 read_text_file 是尾读 256 KB。

        列在「文件与日志」里，点开只会得到一段没有开头没有表头的 JSON 碎片。
        """
        from localizer.web.collector import _HIDDEN_RUN_FILES

        self.assertIn(INDEX_FILENAME, _HIDDEN_RUN_FILES)


if __name__ == "__main__":
    unittest.main()
