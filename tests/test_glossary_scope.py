"""多义术语必须能反向排除语境（评估 R09）。

`scope` 是「只在匹配处检查」，表达不了「除了这几处之外都检查」。坦克世界的
Серебро/Золото 既是货币（银币/金币）又是天梯战段位名（白银/黄金），而货币语境
散落在几十个文件里、段位语境集中在少数几个文件里 —— 只有反向排除能表达。

实测依据（history_tm.json 81709 条）：
  - vehicle_customization / dogtags / badge / comp7 / achievements* 六个文件里
    Серебро/Золото 共 188 处，**0 处是货币语境**；
  - 其中 20 处已经被机械套用术语表改成「达到『银币』段位」这种真实错译；
  - 排除后 851 条 glossary_violation 降到 684 条（-167）。

历史维护曾把 `tips.mo` 加入 Серебро/Золото 的排除范围；本测试只保留验证该行为
所需的最小术语夹具。未排除的货币文件仍必须继续检查。

同时钉死一条反向结论：**Чемпион 不排除**。它在 vehicle_customization.mo 里
110 条已正确译作「勇士」、29 条译作「冠军」——那 29 条是真实不一致，不是误报。
按原方案一起排除会同时丢掉 110 条有效检查并掩盖 29 条真实问题。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.glossary import GlossaryRepository, GlossaryTerm
from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    ResourceBuild,
)
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.resource import (
    RenderResult,
    ResourceDescriptor,
    ValidationResult,
)

SCOPE_GLOSSARY = ROOT / "tests" / "fixtures" / "scope-glossary.yaml"


class AppliesToTests(unittest.TestCase):
    def test_exclude_scope_turns_the_term_off_for_matching_paths(self) -> None:
        term = GlossaryTerm(
            source="Серебро",
            target="银币",
            exclude_scope=("vehicle_customization.mo", "achievements*.mo"),
        )
        self.assertFalse(term.applies_to("vehicle_customization.mo"))
        self.assertFalse(term.applies_to("achievements_page.mo"))
        self.assertTrue(term.applies_to("menu.mo"))
        self.assertTrue(term.applies_to("tips.mo"))

    def test_scope_and_exclude_scope_compose(self) -> None:
        term = GlossaryTerm(
            source="X", target="Y", scope="ui/*.mo", exclude_scope=("ui/debug.mo",)
        )
        self.assertTrue(term.applies_to("ui/menu.mo"))
        self.assertFalse(term.applies_to("ui/debug.mo"))   # 排除优先
        self.assertFalse(term.applies_to("other/menu.mo"))  # scope 没命中

    def test_no_scope_fields_means_everywhere(self) -> None:
        term = GlossaryTerm(source="X", target="Y")
        self.assertTrue(term.applies_to("anything.mo"))


class GlossaryRoundTripTests(unittest.TestCase):
    def test_exclude_scope_survives_yaml_and_json_round_trip(self) -> None:
        term = GlossaryTerm(
            source="Серебро",
            target="银币",
            status="reviewed",
            provenance="human",
            exclude_scope=("badge.mo", "comp7.mo"),
        )
        for suffix in (".yaml", ".json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / f"glossary{suffix}"
                repo = GlossaryRepository(path)
                repo._write((term,))
                loaded = repo.load()
                self.assertEqual(1, len(loaded))
                self.assertEqual(("badge.mo", "comp7.mo"), loaded[0].exclude_scope)

    def test_unknown_term_field_is_still_rejected(self) -> None:
        # 加了新字段不代表 Schema 变松：拼错的字段必须照旧报错。
        with self.assertRaises(ValueError):
            GlossaryRepository._term_from_mapping(
                {"source": "A", "target": "B", "exclude_scopes": ["x.mo"]}
            )


class _FakeAdapter:
    adapter_id = "gettext"

    def scan(self, path):
        return ResourceDescriptor("gettext", Path(path), "x.mo", 0, 1.0)

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


class WotRankVersusCurrencyTests(unittest.TestCase):
    """用真实 WOT 术语表跑真实语料的两种语境。"""

    def setUp(self) -> None:
        self.terms = GlossaryRepository(SCOPE_GLOSSARY).load()
        self.by_source = {term.source: term for term in self.terms}

    def _build(self, relative_path: str, source_text: str, translation: str):
        unit = TranslationUnit(
            project_id="wot-ru-zh",
            adapter_id="gettext",
            relative_path=relative_path,
            logical_key="k",
            source_text=source_text,
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        pipeline = LocalBuildPipeline(glossary_terms=self.terms)
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "x.mo"
            src.write_bytes(b"")
            result = pipeline.build(
                [ResourceBuild(_FakeAdapter(), src, (unit,))],
                {unit.stable_identity: translation},
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=Path(temp) / "out",
            )
            import json

            issues = json.loads(result.qa_json.read_text(encoding="utf-8"))["issues"]
        return [i for i in issues if i["code"] == "glossary_violation"]

    def _violations_for(self, relative_path, source_text, translation, term):
        """只看指定术语 —— 真实语料的句子往往同时命中多条术语（例如
        「Огненный волк」的命名漂移债），那是另一码事，不该混进这条断言。"""
        return [
            issue
            for issue in self._build(relative_path, source_text, translation)
            if issue["details"]["source_term"] == term
        ]

    def test_rank_context_no_longer_blocks_the_release(self) -> None:
        # dogtags 611/612 那两条本次机器新译：译文完全正确，却因 provenance=machine
        # 走零容忍通道硬阻断 release。运维只能二选一 —— 把正确译文改成「银币」
        # 制造真实错译，或者把术语降级。两条路都是拿正确性换绿灯。
        self.assertEqual(
            [],
            self._violations_for(
                "dogtags.mo",
                "За достижение ранга «Серебро» в событии «Огненный волк»",
                "在天梯战的火狼事件中达到白银段位",
                "Серебро",
            ),
        )
        self.assertEqual(
            [],
            self._violations_for(
                "vehicle_customization.mo",
                "Достичь ранга «Золото» в событии «Грозовой медведь».",
                "在天梯战模式的“雷熊”活动中达到黄金段位。",
                "Золото",
            ),
        )

    def test_currency_context_is_still_checked(self) -> None:
        # 排除的是文件不是术语：未排除文件中的货币语境仍然必须拦截。
        violations = self._build(
            "gui_lootboxes.gui_lootboxes.mo", "Серебро", "贷款"
        )
        self.assertEqual(1, len(violations))
        self.assertEqual("Серебро", violations[0]["details"]["source_term"])
        self.assertEqual(1, len(self._build("resource_well.mo", "Золото", "金子")))

    def test_audited_tips_exclusion_is_applied(self) -> None:
        self.assertEqual([], self._build("tips.mo", "Золото", "金子"))
        self.assertEqual([], self._build("tips.mo", "Серебро", "贷款"))

    def test_champion_is_deliberately_not_excluded(self) -> None:
        """Чемпион 的 29 条违规是真实不一致，必须继续报。

        vehicle_customization.mo 里 110 条已正确译作「勇士」、29 条译作「冠军」。
        把它一起排除会同时丢掉那 110 条有效检查 —— 评估报告的原方案在这一点上
        是错的，这条测试就是防止它被「顺手补齐三个词」重新引入。
        """
        self.assertEqual((), self.by_source["Чемпион"].exclude_scope)
        self.assertEqual(
            1, len(self._build("vehicle_customization.mo", "Чемпион", "冠军"))
        )
        self.assertEqual(
            [], self._build("vehicle_customization.mo", "Чемпион", "勇士")
        )


class MaintenanceTimestampCollisionTests(unittest.TestCase):
    """同一秒内的两次维护操作不得互相覆盖备份与 diff（W0a）。

    实测修复前：连续两次 `destructive_replace_all` 之后 `*.bak` 与
    `glossary-diff.*.json` **各只剩 1 份**（audit.jsonl 因为是追加所以幸存 2 行，
    反而让人以为两次都留痕了）。术语视图一旦支持批量操作，同秒两次是常态而非边角。
    """

    def _repo(self, root: Path) -> GlossaryRepository:
        repo = GlossaryRepository(root / "glossary.yaml")
        repo._write(
            tuple(GlossaryTerm(source=f"t{i}", target=f"译{i}") for i in range(5))
        )
        return repo

    def test_two_operations_in_the_same_second_keep_both_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo(root)
            for round_index in range(2):
                repo.destructive_replace_all(
                    tuple(
                        GlossaryTerm(source=f"t{i}", target=f"新{round_index}-{i}")
                        for i in range(5)
                    ),
                    destructive=True,
                    reason=f"第 {round_index} 次",
                )
            maintenance = root / "glossary_maintenance"
            self.assertEqual(2, len(list(maintenance.glob("*.bak"))))
            self.assertEqual(2, len(list(maintenance.glob("glossary-diff.*.json"))))
            self.assertEqual(
                2,
                len(
                    (maintenance / "audit.jsonl")
                    .read_text("utf-8")
                    .strip()
                    .splitlines()
                ),
            )

    def test_concurrent_maintenance_loses_no_audit_event(self) -> None:
        # audit.jsonl 是「读全文 + 拼接 + 整体重写」，并发下会丢事件。
        import threading

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repo(root)
            errors = []

            def worker(index: int) -> None:
                try:
                    repo.destructive_replace_all(
                        tuple(
                            GlossaryTerm(source=f"t{i}", target=f"w{index}-{i}")
                            for i in range(5)
                        ),
                        destructive=True,
                        reason=f"worker {index}",
                    )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            maintenance = root / "glossary_maintenance"
            lines = (
                (maintenance / "audit.jsonl").read_text("utf-8").strip().splitlines()
            )
            self.assertEqual(6, len(lines))
            self.assertEqual(6, len(list(maintenance.glob("*.bak"))))


if __name__ == "__main__":
    unittest.main()
