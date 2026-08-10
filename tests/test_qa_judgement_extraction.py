"""判据抽取的验收是**逐字节相同**，不是「测试没变少」（W1）。

`inspect_unit` / `group_by_source` 是审查视图的地基：面板要在毫秒级复判一条编辑，
就不能有第二份判据实现 —— 两份一旦漂移，面板会说「我这里是绿的」而构建期照样
阻断，那是这个项目最熟悉的一种假绿灯。

所以这组测试守两件事：
1. 抽取前后 `qa-report.json` **逐字节相同**（golden 文件进版本库）；
2. 判据只有一份 —— monkeypatch 掉 `inspect_unit` 之后 `build()` 必须也跟着坏。
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

from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    ResourceBuild,
    UnitInspection,
)
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult
from localizer.rules.loader import load_validation_rule

GOLDEN = ROOT / "tests" / "fixtures" / "qa-report-golden.json"
RU_RULES = ROOT / "tests" / "fixtures" / "ru-rules.yaml"


class _FakeAdapter:
    adapter_id = "gettext"

    def scan(self, path):
        return ResourceDescriptor("gettext", Path(path), Path(path).name, 0, 1.0)

    def plan_destination(self, source, output_root):
        return Path(output_root) / Path(source).name

    def extract(self, path):
        return ()

    def probe(self, path):
        return 1.0

    def validate(self, path):
        return ValidationResult(True)

    def render(self, units, source, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text("ok", encoding="utf-8")
        return RenderResult(Path(destination), len(units), ValidationResult(True))


def _unit(relative_path: str, key: str, source: str) -> TranslationUnit:
    return TranslationUnit(
        project_id="wot-ru-zh",
        adapter_id="gettext",
        relative_path=relative_path,
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
    )


# 刻意覆盖全部 8 类判据 + 同源多译（含一个 QA 记录看不见的空译文成员）。
CASES = (
    ("menu.mo", "ok", "Танк", "坦克"),
    ("menu.mo", "empty", "Пустой", ""),
    ("menu.mo", "residue", "Остаток", "仍有 Кириллица 残留"),
    ("menu.mo", "ph", "Всего %(count)s", "总共"),
    ("menu.mo", "same", "Rearmament", "Rearmament"),
    ("menu.mo", "nul", "Нуль", "含\x00空字符"),
    ("menu.mo", "token", "Токен", "残留 [PH_a1b2c3d4_0] 这里"),
    ("menu.mo", "gloss", "Серебро за бой", "战斗获得的钱"),
    ("a.mo", "g1", "Общий текст", "译法甲"),
    ("b.mo", "g2", "Общий текст", "译法乙"),
    ("c.mo", "g3", "Общий текст", ""),
    ("a.mo", "h1", "Ещё текст", "同一译文"),
    ("b.mo", "h2", "Ещё текст", "同一译文"),
)

TERMS = (
    GlossaryTerm(source="Серебро", target="银币", status="reviewed", provenance="human"),
)

PROVENANCES = ("machine", "legacy_coordinate_exact", "unknown", "embedded")


class _Corpus:
    def __init__(self) -> None:
        self.units = tuple(_unit(rel, key, src) for rel, key, src, _ in CASES)
        self.translations = {
            unit.stable_identity: text
            for unit, (_, _, _, text) in zip(self.units, CASES)
        }
        self.provenance = {
            unit.stable_identity: PROVENANCES[index % len(PROVENANCES)]
            for index, unit in enumerate(self.units)
        }

    def _by_file(self):
        by_file = {}
        for unit in self.units:
            by_file.setdefault(unit.relative_path, []).append(unit)
        return by_file

    def units_in_build_order(self):
        """build() 的遍历顺序：资源按文件名排序，组内保持原顺序。"""
        for _relative_path, group in sorted(self._by_file().items()):
            yield from group

    def pipeline(self) -> LocalBuildPipeline:
        rule = load_validation_rule(RU_RULES, source_locale="ru-RU")
        return LocalBuildPipeline(validation_rule=rule, glossary_terms=TERMS)

    def build(self, pipeline: LocalBuildPipeline, root: Path):
        resources = []
        for relative_path, group in sorted(self._by_file().items()):
            source = root / relative_path
            source.write_bytes(b"")
            resources.append(ResourceBuild(_FakeAdapter(), source, tuple(group)))
        return pipeline.build(
            resources,
            self.translations,
            mode=BuildMode.PREVIEW,
            project_id="wot-ru-zh",
            run_id="golden",
            output_root=root / "out",
            failed_unit_identities=[self.units[1].stable_identity],
            unit_provenance=self.provenance,
        )


class GoldenReportTests(unittest.TestCase):
    def test_report_is_byte_identical_to_the_golden_file(self) -> None:
        corpus = _Corpus()
        with tempfile.TemporaryDirectory() as temp:
            result = corpus.build(corpus.pipeline(), Path(temp))
            produced = result.qa_json.read_bytes()
        expected = GOLDEN.read_bytes()
        self.assertEqual(
            hashlib.sha256(expected).hexdigest(),
            hashlib.sha256(produced).hexdigest(),
            "判据抽取改变了 QA 报告。任何形状变化都会让已登记的存量债基线失配 —— "
            "确认是有意为之的话，重新生成 tests/fixtures/qa-report-golden.json",
        )
        self.assertEqual(expected, produced)

    def test_golden_covers_every_code(self) -> None:
        # golden 只在覆盖全部判据时才有验收价值。
        payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
        codes = {issue["code"] for issue in payload["issues"]}
        self.assertEqual(
            {
                "empty_translation",
                "glossary_violation",
                "invalid_control_character",
                "placeholder_mismatch",
                "placeholder_variant_residue",
                "same_source_inconsistency",
                "source_language_residue",
                "untranslated",
            },
            codes,
        )


class SingleJudgementSourceTests(unittest.TestCase):
    def test_inspect_unit_records_match_build_exactly(self) -> None:
        corpus = _Corpus()
        pipeline = corpus.pipeline()
        with tempfile.TemporaryDirectory() as temp:
            result = corpus.build(pipeline, Path(temp))
            from_build = json.loads(result.qa_json.read_text(encoding="utf-8"))
        # 逐条重跑 inspect_unit，与 build 产出的非跨条目记录比对（**含顺序**）。
        # 顺序必须按 build 的遍历顺序（资源按文件名排序，组内按原顺序），
        # 否则比的就不是同一件事。
        direct = []
        for unit in corpus.units_in_build_order():
            inspection = pipeline.inspect_unit(
                unit,
                corpus.translations[unit.stable_identity],
                corpus.provenance[unit.stable_identity],
            )
            # 走一遍 JSON：报告是 JSON 往返过的，tuple 会变成 list，
            # 那是序列化差异不是判据差异。
            direct.extend(
                json.loads(json.dumps(record.to_dict(), ensure_ascii=False))
                for record in inspection.records
            )
        per_unit_from_build = [
            issue
            for issue in from_build["issues"]
            if issue["code"] != "same_source_inconsistency"
        ]
        self.assertEqual(per_unit_from_build, direct)

    def test_build_has_no_second_judgement_implementation(self) -> None:
        """把 inspect_unit 打坏，build() 必须也跟着坏。

        如果 build() 还能正常产出报告，说明判据在别处又实现了一份 —— 那正是
        面板与构建期给出不同结论的成因。
        """
        corpus = _Corpus()
        pipeline = corpus.pipeline()

        def exploding(*args, **kwargs):
            raise AssertionError("inspect_unit 被绕过了")

        pipeline.inspect_unit = exploding
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AssertionError):
                corpus.build(pipeline, Path(temp))

    def test_inspection_text_is_what_gets_rendered(self) -> None:
        # rules.yaml 的 mappings 可能改写译文；分组与产物必须用同一份文本。
        corpus = _Corpus()
        pipeline = corpus.pipeline()
        unit = corpus.units[0]
        inspection = pipeline.inspect_unit(unit, "坦克", "machine")
        self.assertIsInstance(inspection, UnitInspection)
        self.assertEqual("坦克", inspection.text)


class GroupBySourceTests(unittest.TestCase):
    def _prepared(self):
        corpus = _Corpus()
        pipeline = corpus.pipeline()
        translated = []
        for unit in corpus.units:
            inspection = pipeline.inspect_unit(
                unit,
                corpus.translations[unit.stable_identity],
                corpus.provenance[unit.stable_identity],
            )
            translated.append(
                unit.__class__(
                    **{
                        **{
                            field: getattr(unit, field)
                            for field in unit.__dataclass_fields__
                        },
                        "translation": inspection.text,
                    }
                )
            )
        return [(None, tuple(translated))]

    def test_include_empty_adds_the_members_qa_cannot_see(self) -> None:
        prepared = self._prepared()
        strict = LocalBuildPipeline.group_by_source(prepared, include_empty=False)
        loose = LocalBuildPipeline.group_by_source(prepared, include_empty=True)
        source = "Общий текст"
        # QA 只看到两个译法、两个成员；真实分组是三个成员。
        self.assertEqual(2, len(strict[source]))
        self.assertEqual(2, sum(len(v) for v in strict[source].values()))
        self.assertEqual(3, sum(len(v) for v in loose[source].values()))
        self.assertIn("", loose[source])

    def test_strict_grouping_reproduces_the_qa_records(self) -> None:
        prepared = self._prepared()
        strict = LocalBuildPipeline.group_by_source(prepared, include_empty=False)
        divergent = {src for src, variants in strict.items() if len(variants) > 1}
        records = LocalBuildPipeline._consistency_issues(prepared)
        self.assertEqual(
            divergent, {record.details["source"] for record in records}
        )


if __name__ == "__main__":
    unittest.main()
