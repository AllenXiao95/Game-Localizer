"""空译文的 QA 短路与根因关联（R03）。

2026-08-04 真机：一个文件里 98 个词条没翻出来，报告里却是 **212 个 error** ——
98 条 `empty_translation`、83 条 `glossary_violation`、31 条 `placeholder_mismatch`。
后两类全是派生噪声：空串当然不含术语要求的译名，空串当然不含源文的占位符。
审查台因此把同一件事排了三遍队，而真正的信息（模型为什么没译出来）被整个丢掉了。

两条修复分别在两个地方，边界必须钉死：

- **减法**落在 `qa-report.json`：只报主错误，不跑依赖非空译文的下游判据。
  纯减法是刻意的 —— `debt_key` 含 `details` 摘要，动任何既有记录的 details
  都会让 853 条已登记存量债 100% 失配；棘轮只查 unaccepted，集合变小天然安全。
- **加法**落在边车 `qa-review-index.json`：根因（Provider 报错 / 内容 QA 判据）
  和「短路掉了哪些判据」放这里。这也是为什么报告的字节形状可以保持不变。
"""
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

from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    ResourceBuild,
)
from localizer.application.quality_gate import QARecord
from localizer.application.review_index import INDEX_FILENAME
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult
from localizer.rules.loader import load_validation_rule

RU_RULES = ROOT / "tests" / "fixtures" / "ru-rules.yaml"

# 真机那 98 条的形状：源文同时带术语和占位符，译文为空。
SOURCE_WITH_TERM_AND_PLACEHOLDER = "Серебро: %(count)s"

TERMS = (
    GlossaryTerm(
        source="Серебро", target="银币", status="reviewed", provenance="human"
    ),
)


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


def _unit(key: str, source: str) -> TranslationUnit:
    return TranslationUnit(
        project_id="wot-ru-zh",
        adapter_id="gettext",
        relative_path="menu.mo",
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
    )


class _Harness(unittest.TestCase):
    def build(self, cases, *, root_causes=None):
        """cases: {logical_key: (源文, 译文)}。返回 (issues, sidecar)。"""
        units = tuple(_unit(key, src) for key, (src, _) in cases.items())
        translations = {
            unit.stable_identity: text
            for unit, (_, text) in zip(units, cases.values())
        }
        pipeline = LocalBuildPipeline(
            validation_rule=load_validation_rule(RU_RULES, source_locale="ru-RU"),
            glossary_terms=TERMS,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            causes = {
                units[list(cases).index(key)].stable_identity: value
                for key, value in (root_causes or {}).items()
            }
            result = pipeline.build(
                [ResourceBuild(_FakeAdapter(), source, units)],
                translations,
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=root / "out",
                unit_root_causes=causes,
            )
            issues = json.loads(result.qa_json.read_text("utf-8"))["issues"]
            sidecar = json.loads(
                (result.qa_json.parent / INDEX_FILENAME).read_text("utf-8")
            )
        identities = {key: unit.stable_identity for key, unit in zip(cases, units)}
        return issues, sidecar, identities

    def codes_for(self, issues, identity):
        return sorted(
            issue["code"] for issue in issues if issue["stable_identity"] == identity
        )


class ShortCircuitTests(_Harness):
    def test_empty_translation_reports_only_the_primary_error(self) -> None:
        """这一条就是 212 → 98 的全部。"""
        issues, _sidecar, ids = self.build(
            {"empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")}
        )
        self.assertEqual(["empty_translation"], self.codes_for(issues, ids["empty"]))

    def test_whitespace_only_translation_counts_as_empty(self) -> None:
        # 修复前 "   " 同样会派生术语与占位符违规；它和 "" 是同一件事。
        issues, _sidecar, ids = self.build(
            {"blank": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "   \t \n ")}
        )
        self.assertEqual(["empty_translation"], self.codes_for(issues, ids["blank"]))

    def test_non_empty_translation_still_gets_every_check(self) -> None:
        """短路只对空译文成立。这条是防止「顺手把检查关掉」的对照组。"""
        issues, _sidecar, ids = self.build(
            {"bad": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "金币若干")}
        )
        self.assertEqual(
            ["glossary_violation", "placeholder_mismatch"],
            self.codes_for(issues, ids["bad"]),
        )

    def test_a_single_space_of_real_content_flips_it_back_on(self) -> None:
        # 边界：strip() 之后还剩东西就不算空。
        issues, _sidecar, ids = self.build(
            {"tiny": (SOURCE_WITH_TERM_AND_PLACEHOLDER, " 银 ")}
        )
        self.assertIn("placeholder_mismatch", self.codes_for(issues, ids["tiny"]))

    def test_the_real_98_shape_collapses_from_212_to_98(self) -> None:
        """按真机比例复现：全空译，error 数必须等于词条数。"""
        cases = {
            f"k{index}": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")
            for index in range(98)
        }
        issues, _sidecar, _ids = self.build(cases)
        errors = [issue for issue in issues if issue["severity"] == "error"]
        self.assertEqual(98, len(errors))
        self.assertEqual({"empty_translation"}, {issue["code"] for issue in errors})


class RootCauseTests(_Harness):
    def test_root_cause_lands_in_the_sidecar_not_the_report(self) -> None:
        cause = {
            "state": "failed",
            "issues": [
                {
                    "code": "source_language_residue",
                    "severity": "error",
                    "message": "translation contains Russian text",
                }
            ],
        }
        issues, sidecar, ids = self.build(
            {"empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")},
            root_causes={"empty": cause},
        )
        entry = sidecar["units"][ids["empty"]]
        self.assertEqual(cause, entry["root_cause"])
        self.assertIn("glossary_violation", entry["suppressed_codes"])
        self.assertIn("placeholder_mismatch", entry["suppressed_codes"])
        # 报告一个字节都不带根因 —— details 参与 debt_key。
        for issue in issues:
            self.assertNotIn("root_cause", issue)
            self.assertNotIn("root_cause", issue.get("details") or {})

    def test_details_shape_of_the_primary_error_is_unchanged(self) -> None:
        """`empty_translation` 的 details 必须仍是 `{}`。

        它一旦变成 `{"root_cause": …}`，所有已登记的空译存量债 debt_key
        全部失配，棘轮会把它们当成**新增**坏账把发布拦死。
        """
        issues, _sidecar, ids = self.build(
            {"empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")},
            root_causes={"empty": {"state": "failed", "issues": []}},
        )
        record = next(i for i in issues if i["stable_identity"] == ids["empty"])
        self.assertEqual({}, record["details"])
        self.assertEqual(
            QARecord("empty_translation", "error", "x", ids["empty"], "menu.mo", {}).debt_key,
            QARecord(
                record["code"],
                record["severity"],
                record["message"],
                record["stable_identity"],
                record["relative_path"],
                record["details"],
            ).debt_key,
        )

    def test_a_failed_unit_with_a_carried_translation_still_gets_its_cause(
        self,
    ) -> None:
        """译文非空 → 没有 `empty_translation` → 该坐标本来不进索引。

        但它**确实**是本次翻译失败的坐标，根因必须有地方挂，否则审查页
        永远看不到「这条重试过、失败在哪」。
        """
        _issues, sidecar, ids = self.build(
            {"carried": ("Танк", "坦克")},
            root_causes={"carried": {"state": "failed", "issues": []}},
        )
        self.assertIn(ids["carried"], sidecar["units"])
        self.assertEqual("failed", sidecar["units"][ids["carried"]]["root_cause"]["state"])


class RatchetSafetyTests(_Harness):
    def test_suppression_only_removes_records(self) -> None:
        """短路对报告是**纯减法**：留下来的记录逐字节不变。

        这是「不会让已登记存量债失配」的直接证据 —— 棘轮查的是
        `unaccepted = carried - accepted`，被减掉的键只会让这个差集变小。
        """
        cases = {
            "empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, ""),
            "ok": ("Танк", "坦克"),
            "bad": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "金币若干"),
        }
        issues, _sidecar, ids = self.build(cases)
        survivors = {
            (i["stable_identity"], i["code"]): i for i in issues
        }
        # 空译那条只剩主错误；另外两条完全不受影响。
        self.assertEqual(["empty_translation"], self.codes_for(issues, ids["empty"]))
        self.assertEqual([], self.codes_for(issues, ids["ok"]))
        self.assertEqual(
            ["glossary_violation", "placeholder_mismatch"],
            self.codes_for(issues, ids["bad"]),
        )
        # 幸存记录的 details 没有被改写。
        bad = survivors[(ids["bad"], "placeholder_mismatch")]
        self.assertEqual(["%(count)s"], bad["details"]["source"])


class ConsistencyIsUnaffectedTests(_Harness):
    def test_empty_members_still_do_not_create_inconsistency_warnings(self) -> None:
        """同源多译本来就用 `include_empty=False`，R03 不许顺手改它的口径。"""
        cases = {
            "a": ("Общий текст", "译法甲"),
            "b": ("Общий текст", ""),
        }
        issues, sidecar, _ids = self.build(cases)
        self.assertEqual(
            [], [i for i in issues if i["code"] == "same_source_inconsistency"]
        )
        # 而边车里空成员仍然可见（一键统一要用）。
        group = next(
            g for g in sidecar["same_source_groups"] if g["source"] == "Общий текст"
        ) if sidecar["same_source_groups"] else None
        self.assertIsNone(group, "只有一种非空译法，不该成组")

    def test_a_real_divergence_with_an_empty_member_is_still_reported(self) -> None:
        cases = {
            "a": ("Общий текст", "译法甲"),
            "b": ("Общий текст", "译法乙"),
            "c": ("Общий текст", ""),
        }
        issues, sidecar, _ids = self.build(cases)
        warnings = [i for i in issues if i["code"] == "same_source_inconsistency"]
        self.assertEqual(1, len(warnings))
        self.assertEqual(["译法乙", "译法甲"], sorted(warnings[0]["details"]["translations"]))
        group = next(
            g for g in sidecar["same_source_groups"] if g["source"] == "Общий текст"
        )
        self.assertTrue(group["has_empty_members"])
        self.assertEqual(3, group["member_count"])


class EndToEndRootCauseTests(unittest.TestCase):
    """根因必须真的从编排层流到边车，不能只是 `build()` 认这个参数。

    这条走真 `ProjectRunner`：Provider 把俄文原样吐回来 → 内容 QA 判
    `source_language_residue` → 词条失败 → 译文为空 → 报告只剩主错误，
    而「失败在哪」在边车里。
    """

    def setUp(self) -> None:
        from tests.test_rebuild_from_run import _CountingProvider, _Project

        self._temp = tempfile.TemporaryDirectory()
        self._provider_cls = _CountingProvider
        self.project = _Project(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_provider_failure_reason_reaches_the_sidecar(self) -> None:
        provider = self._provider_cls(fail=("b",))
        result = self.project.runner(provider).run(
            mode=BuildMode.PREVIEW, run_id="r1"
        )
        reports = result.build.qa_json.parent
        issues = json.loads(result.build.qa_json.read_text("utf-8"))["issues"]
        sidecar = json.loads((reports / INDEX_FILENAME).read_text("utf-8"))

        identity = self.project.identity("b")
        self.assertEqual(1, result.failed_units)
        self.assertEqual(
            ["empty_translation"],
            sorted(i["code"] for i in issues if i["stable_identity"] == identity),
        )
        cause = sidecar["units"][identity]["root_cause"]
        self.assertEqual("failed", cause["state"])
        self.assertIn(
            "source_language_residue", [issue["code"] for issue in cause["issues"]]
        )
        # 这条源文（"Строка b"）既不含术语也不含占位符，所以空译文**什么都
        # 没压掉**，边车里不该出现 suppressed_codes。
        #
        # 早先这里断言的是「压掉了 glossary_violation」—— 那是因为当时用了一张
        # 静态清单，无条件宣称 6 条判据都被压掉。审查页拿这个字段当"这条编辑
        # 之前就有的问题"的基线，谎报会把真正新引入的问题掩盖成"本来就有"。
        self.assertNotIn("suppressed_codes", sidecar["units"][identity])

    def test_successful_units_carry_no_root_cause(self) -> None:
        provider = self._provider_cls(fail=("b",))
        result = self.project.runner(provider).run(
            mode=BuildMode.PREVIEW, run_id="r1"
        )
        sidecar = json.loads(
            (result.build.qa_json.parent / INDEX_FILENAME).read_text("utf-8")
        )
        for identity, entry in sidecar["units"].items():
            if identity != self.project.identity("b"):
                self.assertNotIn("root_cause", entry)


if __name__ == "__main__":
    unittest.main()


class SuppressedCodesAreAccurateTests(_Harness):
    """`suppressed_codes` 必须精确 —— 它是审查页复核的基线（对抗性审查 HIGH）。

    审查页的即时复核用 `introduced = now - was` 判断「这次编辑有没有引入新问题」，
    `was` 取自边车的 `codes`。R03 把空译成员的 `codes` 砍成只剩
    `empty_translation` 之后，把一个同源组统一到组内**既有**的译法时，那条既有的
    术语违规对空译成员就变成了「本次新引入」—— `commit()` 直接抛异常，而
    `unify()` 连 `accepted_debt` 参数都没有，操作者没有任何出口。

    修法不是把那张静态清单并进 `was`：清单里 6 条判据有 4 条在空串上根本不会
    命中，并进去反而会把真正新引入的问题掩盖成「本来就有」。两个方向都会出事，
    所以 `inspect_unit` 照常算完下游判据，只是不上报，把**真正会命中的** code
    记进 `suppressed_codes`。
    """

    def test_only_the_codes_that_would_actually_fire_are_listed(self) -> None:
        _issues, sidecar, ids = self.build(
            {"empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")}
        )
        entry = sidecar["units"][ids["empty"]]
        # 源文同时带术语和占位符 → 这两条确实会命中。
        self.assertEqual(
            ["glossary_violation", "placeholder_mismatch"],
            sorted(entry["suppressed_codes"]),
        )
        # 而这些在空串上永远不会命中，不许出现在清单里。
        for never in (
            "untranslated",
            "invalid_control_character",
            "placeholder_variant_residue",
            "source_language_residue",
        ):
            self.assertNotIn(never, entry["suppressed_codes"])

    def test_a_source_with_nothing_to_violate_suppresses_nothing(self) -> None:
        _issues, sidecar, ids = self.build({"plain": ("Танк", "")})
        self.assertNotIn("suppressed_codes", sidecar["units"][ids["plain"]])

    def test_the_report_still_only_carries_the_primary_error(self) -> None:
        """算了不等于报了 —— qa-report.json 必须仍然只有一条。"""
        issues, _sidecar, ids = self.build(
            {"empty": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "")}
        )
        self.assertEqual(["empty_translation"], self.codes_for(issues, ids["empty"]))


class UnifyingToAnExistingVariantIsNotBlockedTests(unittest.TestCase):
    """端到端复现那条回归：统一到组内既有译法不该被判成「新引入 error」。"""

    def _recheck(self, edits):
        from localizer.application.review_index import INDEX_FILENAME, ReviewIndex
        from localizer.application.review_recheck import ReviewRechecker

        cases = {
            "a": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "金币 %(count)s"),
            "b": (SOURCE_WITH_TERM_AND_PLACEHOLDER, "铜币 %(count)s"),
            "c": (SOURCE_WITH_TERM_AND_PLACEHOLDER, ""),
        }
        units = tuple(_unit(key, src) for key, (src, _) in cases.items())
        translations = {
            unit.stable_identity: text
            for unit, (_, text) in zip(units, cases.values())
        }
        pipeline = LocalBuildPipeline(
            validation_rule=load_validation_rule(RU_RULES, source_locale="ru-RU"),
            glossary_terms=TERMS,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            result = pipeline.build(
                [ResourceBuild(_FakeAdapter(), source, units)],
                translations,
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=root / "out",
            )
            index = ReviewIndex.load(result.qa_json.parent / INDEX_FILENAME)
            ids = {key: unit.stable_identity for key, unit in zip(cases, units)}
            verdicts = ReviewRechecker(index, pipeline).check(
                {ids[key]: text for key, text in edits.items()}
            )
        return verdicts, ids

    def test_the_empty_member_does_not_report_a_new_error(self) -> None:
        """把空译成员统一成组内既有的 "金币 %(count)s"。

        那条 glossary_violation 是这一组**本来就有**的（成员 a 也带着它），
        不是这次编辑造出来的。修复前它会被记进 introduced，commit 硬阻断。
        """
        result, ids = self._recheck({"c": "金币 %(count)s"})
        verdict = next(v for v in result.verdicts if v.stable_identity == ids["c"])
        self.assertEqual((), verdict.introduced)
        self.assertIn("glossary_violation", verdict.remaining)

    def test_a_genuinely_new_error_is_still_reported(self) -> None:
        """对照组：真的引入新问题时必须照报，否则这个修复就成了消音。"""
        result, ids = self._recheck({"c": "金币"})  # 丢了占位符
        verdict = next(v for v in result.verdicts if v.stable_identity == ids["c"])
        self.assertEqual((), verdict.introduced)
        # placeholder_mismatch 本来就在被压掉的清单里，所以是 remaining；
        # 真正的新引入要用一个空译时不会命中的判据来验。
        result2, ids2 = self._recheck({"c": "金币 %(count)s\x00"})
        verdict2 = next(v for v in result2.verdicts if v.stable_identity == ids2["c"])
        self.assertIn("invalid_control_character", verdict2.introduced)

    def test_leaving_it_empty_keeps_the_codes_as_remaining_not_fixed(self) -> None:
        """编辑之后仍然是空 → 那些判据依旧成立，不能算「已修复」。"""
        result, ids = self._recheck({"c": "   "})
        verdict = next(v for v in result.verdicts if v.stable_identity == ids["c"])
        self.assertEqual((), verdict.fixed)
        self.assertIn("glossary_violation", verdict.remaining)
