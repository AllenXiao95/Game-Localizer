"""面板内的即时重校验不得制造绿灯（W5）。

它算的是「这条编辑本身还有没有问题」，**不是**「release 能不能发」。
所以每一份结果都强制携带 `authoritative=False` 与 `not_evaluated`。

最关键的一条是 `test_recheck_is_a_subset_of_a_full_build`：面板说某个 code
消解了，整轮 build 在同一 (sid, code) 上也必须没有记录。否则面板就是在
凭空发绿灯。
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
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from localizer.application.review_index import INDEX_FILENAME, ReviewIndex
from localizer.application.review_recheck import NOT_EVALUATED, ReviewRechecker
from test_qa_judgement_extraction import _Corpus


class _Fixture:
    def __init__(self, temp: str) -> None:
        self.corpus = _Corpus()
        self.pipeline = self.corpus.pipeline()
        self.root = Path(temp)
        self.result = self.corpus.build(self.pipeline, self.root)
        self.index = ReviewIndex.load(self.result.qa_json.parent / INDEX_FILENAME)
        self.rechecker = ReviewRechecker(self.index, self.pipeline)

    def identity(self, key: str) -> str:
        return next(u.stable_identity for u in self.corpus.units if u.logical_key == key)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.fx = _Fixture(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()


class NeverAuthoritativeTests(_Case):
    def test_result_is_never_authoritative(self) -> None:
        for edits in ({}, {self.fx.identity("empty"): "补上的译文"}):
            with self.subTest(edits=bool(edits)):
                result = self.fx.rechecker.check(edits)
                self.assertFalse(result.authoritative)
                self.assertEqual(NOT_EVALUATED, result.not_evaluated)
                payload = result.as_dict()
                self.assertFalse(payload["authoritative"])
                self.assertNotIn("passed", payload)

    def test_not_evaluated_names_the_three_things_it_cannot_know(self) -> None:
        self.assertEqual(
            ("quality_gate", "failed_unit_count", "legacy_debt_baseline"),
            NOT_EVALUATED,
        )


class SingleJudgementTests(_Case):
    def test_recheck_goes_through_inspect_unit(self) -> None:
        """判据只有一份 —— 打坏 inspect_unit，check() 必须也跟着坏。"""

        def exploding(*args, **kwargs):
            raise AssertionError("inspect_unit 被绕过了")

        self.fx.pipeline.inspect_unit = exploding
        with self.assertRaises(AssertionError):
            self.fx.rechecker.check({self.fx.identity("empty"): "译文"})

    def test_recheck_is_a_subset_of_a_full_build(self) -> None:
        """面板说消解了的，整轮 build 也必须真的消解。

        这是防假绿灯的核心断言：把编辑套进语料重跑一次完整 build，
        逐条比对 (sid, code)。
        """
        edits = {
            self.fx.identity("empty"): "补上的译文",
            self.fx.identity("ph"): "总共 %(count)s",
            self.fx.identity("gloss"): "战斗获得的银币",
            self.fx.identity("nul"): "干净译文",
        }
        result = self.fx.rechecker.check(edits)

        corpus = _Corpus()
        corpus.translations.update(edits)
        with tempfile.TemporaryDirectory() as temp:
            built = corpus.build(corpus.pipeline(), Path(temp))
            report = json.loads(built.qa_json.read_text(encoding="utf-8"))
        from_build = {
            (issue["stable_identity"], issue["code"]) for issue in report["issues"]
        }
        for verdict in result.verdicts:
            for code in verdict.fixed:
                with self.subTest(sid=verdict.stable_identity, code=code):
                    self.assertNotIn(
                        (verdict.stable_identity, code),
                        from_build,
                        "面板说这条已消解，但整轮 build 仍然报它 —— 假绿灯",
                    )
            for code in verdict.remaining:
                with self.subTest(sid=verdict.stable_identity, code=code):
                    self.assertIn((verdict.stable_identity, code), from_build)


class VerdictTests(_Case):
    def test_fixing_an_issue_is_reported_as_fixed(self) -> None:
        identity = self.fx.identity("empty")
        verdict = self.fx.rechecker.check({identity: "补上的译文"}).verdicts[0]
        self.assertIn("empty_translation", verdict.fixed)
        self.assertEqual((), verdict.introduced)

    def test_introducing_an_error_is_reported(self) -> None:
        # 把一条已收录的译文改坏。索引只收「有 QA 记录的」与「同源组成员」——
        # 完全干净的词条不可编辑，这是刻意的：审查视图只处理 QA 已识别的问题。
        identity = self.fx.identity("gloss")
        verdict = self.fx.rechecker.check({identity: "含\x00空字符"}).verdicts[0]
        self.assertIn("invalid_control_character", verdict.introduced)
        self.assertIn(
            (identity, "invalid_control_character"),
            self.fx.rechecker.check({identity: "含\x00空字符"}).introduced_errors,
        )

    def test_unknown_identity_is_reported_not_silently_dropped(self) -> None:
        result = self.fx.rechecker.check({"not-in-this-run": "译文"})
        self.assertEqual(("not-in-this-run",), result.unknown_identities)
        self.assertEqual((), result.verdicts)


class ConsistencyDeltaTests(_Case):
    def test_unifying_a_group_is_reported_as_resolved(self) -> None:
        # 把「译法乙」改成「译法甲」，这一组就不再分歧。
        result = self.fx.rechecker.check({self.fx.identity("g2"): "译法甲"})
        self.assertEqual(1, len(result.consistency.resolved_groups))
        self.assertEqual((), result.consistency.introduced_groups)

    def test_creating_a_new_divergence_is_reported(self) -> None:
        """最容易被忽略的回归：你把 A 统一了，顺手改的 B 制造了新的分歧。

        需要一对「都被索引、且当前同译」的词条。golden 语料里 h1/h2 虽然同源同译，
        但它们完全干净、不进索引（审查视图只处理 QA 已识别的问题），所以这条
        单独造夹具：两条同源词条各自带一个术语违规，译文相同 —— 分歧为 0；
        改其中一条就产生新的 same_source_inconsistency。
        只有覆盖**全量**的 source_buckets 才看得见这件事。
        """
        from localizer.adapters.storage.glossary import GlossaryTerm
        from localizer.application.local_build import BuildMode, LocalBuildPipeline, ResourceBuild
        from localizer.domain.translation_unit import TranslationUnit
        from test_qa_judgement_extraction import _FakeAdapter

        def unit(rel, key):
            return TranslationUnit(
                project_id="p", adapter_id="gettext", relative_path=rel,
                logical_key=key, source_text="Серебро за бой",
                source_locale="ru-RU", target_locale="zh-Hans",
            )

        units = (unit("a.mo", "x"), unit("b.mo", "y"))
        terms = (GlossaryTerm(source="Серебро", target="银币",
                              status="reviewed", provenance="human"),)
        pipeline = LocalBuildPipeline(glossary_terms=terms)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = []
            for u in units:
                src = root / u.relative_path
                src.write_bytes(b"")
                resources.append(ResourceBuild(_FakeAdapter(), src, (u,)))
            built = pipeline.build(
                resources,
                {u.stable_identity: "战斗获得的钱" for u in units},
                mode=BuildMode.PREVIEW, project_id="p", run_id="r",
                output_root=root / "out",
            )
            index = ReviewIndex.load(built.qa_json.parent / INDEX_FILENAME)

        self.assertEqual(2, len(index.units), "两条都该被索引（各有一个术语违规）")
        rechecker = ReviewRechecker(index, pipeline)
        result = rechecker.check({units[0].stable_identity: "战斗获得的银币"})
        self.assertEqual(1, len(result.consistency.introduced_groups))
        self.assertEqual((), result.consistency.resolved_groups)

    def test_empty_translation_does_not_count_as_a_variant(self) -> None:
        # 与 QA 记录口径一致：空译文不算一个译法。
        result = self.fx.rechecker.check({self.fx.identity("g3"): ""})
        self.assertEqual((), result.consistency.introduced_groups)


class ClusterScopeTests(_Case):
    def test_scope_limits_the_units_that_get_judged(self) -> None:
        """改一个术语只重判该 cluster 覆盖的词条。

        对全量单元重判实测 9.5 秒，43 个术语决策就是 7 分钟纯卡顿。
        """
        cluster = self.fx.index.glossary_clusters[0]
        calls = []
        original = self.fx.pipeline.inspect_unit

        def counting(unit, text, provenance="unknown"):
            calls.append(unit.stable_identity)
            return original(unit, text, provenance)

        self.fx.pipeline.inspect_unit = counting
        edits = {identity: "任意译文" for identity in self.fx.index.units}
        self.fx.rechecker.check(edits, scope=f"cluster:{cluster['cluster_id']}")
        self.assertEqual(
            sorted(cluster["violation_identities"]), sorted(calls)
        )
        self.assertLess(len(calls), len(edits), "scope 没有起到收窄作用")

    def test_unknown_scope_is_refused(self) -> None:
        for scope in ("cluster:nope", "everything"):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    self.fx.rechecker.check({}, scope=scope)


if __name__ == "__main__":
    unittest.main()
