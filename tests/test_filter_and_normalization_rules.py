"""FilterRule 与 NormalizationRule 的契约测试（R12）。

loader 对未知顶层键报错，防止未支持或拼错的规则被静默忽略。

两类规则的风险方向相反，测试也按这个分：

- FilterRule 是**减法**，风险是「静默少译一片」。所以测 reason 必填、
  无条件规则被拒、被跳过的词条既不送模型也不进 QA、且在计划里单独计数。
- NormalizationRule 是**改写**，风险是不收敛和「从产物反算 QA」失真。
  所以测不动点、不收敛报 error、以及 `mappings_empty` 必须把它算进去。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    ResourceBuild,
)
from localizer.application.review_index import INDEX_FILENAME
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult
from localizer.rules.filtering import (
    FilterRule,
    FilterRuleSet,
    NormalizationRule,
    NormalizationRuleSet,
    RuleDefinitionError,
)
from localizer.rules.loader import (
    RulesLoadError,
    load_filter_rules,
    load_normalization_rules,
    load_validation_rule,
)


def _rules_file(root: Path, body: dict) -> Path:
    path = root / "rules.yaml"
    path.write_text(
        yaml.safe_dump({"schema_version": 1, **body}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _unit(key: str, source: str, path: str = "menu.mo") -> TranslationUnit:
    return TranslationUnit(
        project_id="wot-ru-zh",
        adapter_id="gettext",
        relative_path=path,
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
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


class FilterRuleDefinitionTests(unittest.TestCase):
    def test_a_rule_with_no_condition_is_refused(self) -> None:
        """一条什么都不写的规则会匹配全部词条，把整个项目静默跳过。"""
        with self.assertRaises(RuleDefinitionError) as ctx:
            FilterRule(id="oops", reason="因为")
        self.assertIn("every unit", str(ctx.exception))

    def test_reason_is_mandatory(self) -> None:
        with self.assertRaises(RuleDefinitionError):
            FilterRule(id="x", reason="", path_glob="*.mo")
        with self.assertRaises(RuleDefinitionError):
            FilterRule(id="x", reason="   ", path_glob="*.mo")

    def test_conditions_are_conjunctive(self) -> None:
        rule = FilterRule(
            id="debug",
            reason="调试串不入库",
            path_glob="debug/*.mo",
            key_pattern=r"dbg_.*",
        )
        self.assertTrue(
            rule.matches(
                adapter_id="gettext",
                relative_path="debug/a.mo",
                logical_key="dbg_1",
                source_text="x",
            )
        )
        # 路径命中但 key 不命中 —— 不跳过。
        self.assertFalse(
            rule.matches(
                adapter_id="gettext",
                relative_path="debug/a.mo",
                logical_key="real_1",
                source_text="x",
            )
        )

    def test_patterns_are_fullmatch_not_search(self) -> None:
        rule = FilterRule(id="k", reason="r", key_pattern="id")
        self.assertTrue(
            rule.matches(
                adapter_id="g", relative_path="a.mo", logical_key="id", source_text=""
            )
        )
        self.assertFalse(
            rule.matches(
                adapter_id="g",
                relative_path="a.mo",
                logical_key="identity",
                source_text="",
            )
        )

    def test_source_pattern_spans_newlines(self) -> None:
        # 多行源文是常态；. 不匹配换行会让「纯数字块」这类规则莫名漏掉一半。
        rule = FilterRule(id="n", reason="r", source_pattern=r"\d+.*")
        self.assertTrue(
            rule.matches(
                adapter_id="g",
                relative_path="a.mo",
                logical_key="k",
                source_text="12\n34",
            )
        )

    def test_duplicate_ids_are_refused(self) -> None:
        with self.assertRaises(RuleDefinitionError):
            FilterRuleSet(
                [
                    FilterRule(id="dup", reason="a", path_glob="*.mo"),
                    FilterRule(id="dup", reason="b", path_glob="*.po"),
                ]
            )

    def test_bad_regex_fails_at_definition_time(self) -> None:
        with self.assertRaises(RuleDefinitionError):
            FilterRule(id="x", reason="r", key_pattern="(unclosed")


class FilterRuleLoaderTests(unittest.TestCase):
    def test_the_section_is_now_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(
                Path(temp),
                {
                    "filter_rules": [
                        {"id": "tech", "reason": "纯技术键", "key_pattern": r"tech_.*"}
                    ]
                },
            )
            rules = load_filter_rules(path)
            self.assertEqual(1, len(rules.rules))
            self.assertEqual("tech", rules.rules[0].id)

    def test_a_misspelled_field_is_an_error_not_a_silent_drop(self) -> None:
        """整个 loader 的既定立场：不认识的键报错。半生效的规则最危险。"""
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(
                Path(temp),
                {"filter_rules": [{"id": "x", "reason": "r", "path_globs": "*.mo"}]},
            )
            with self.assertRaises(RulesLoadError) as ctx:
                load_filter_rules(path)
            self.assertIn("path_globs", str(ctx.exception))

    def test_definition_errors_surface_as_load_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(Path(temp), {"filter_rules": [{"id": "x", "reason": "r"}]})
            with self.assertRaises(RulesLoadError):
                load_filter_rules(path)

    def test_a_non_string_value_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(
                Path(temp),
                {"filter_rules": [{"id": "x", "reason": "r", "key_pattern": 12}]},
            )
            with self.assertRaises(RulesLoadError):
                load_filter_rules(path)

    def test_the_real_wot_rules_file_still_loads(self) -> None:
        # 现网文件没有这两段；加了新段之后它必须照旧能读。
        rules = load_filter_rules(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        self.assertFalse(rules)


class FilteredUnitsAreOutOfScopeTests(unittest.TestCase):
    """被跳过的词条必须真的消失：不送模型、不进 QA、不进同源分组。"""

    def _build(self, filtered):
        units = (
            _unit("keep", "Танк"),
            _unit("skip", "Всего %(count)s"),
        )
        translations = {units[0].stable_identity: "坦克"}
        pipeline = LocalBuildPipeline()
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
                filtered_identities=[units[1].stable_identity] if filtered else [],
            )
            issues = json.loads(result.qa_json.read_text("utf-8"))["issues"]
            sidecar = json.loads(
                (result.qa_json.parent / INDEX_FILENAME).read_text("utf-8")
            )
        return issues, sidecar, units

    def test_without_the_filter_the_unit_reports_empty_translation(self) -> None:
        # 对照组：证明这条词条**确实**会产生 QA 记录。
        issues, _sidecar, units = self._build(False)
        self.assertIn(
            units[1].stable_identity, {i["stable_identity"] for i in issues}
        )

    def test_a_filtered_unit_produces_no_qa_record(self) -> None:
        issues, _sidecar, units = self._build(True)
        self.assertNotIn(
            units[1].stable_identity, {i["stable_identity"] for i in issues}
        )

    def test_a_filtered_unit_is_not_handed_to_the_adapter(self) -> None:
        """跳过的词条不进 render 的单元集。

        两个真实 Adapter 的 `by_key.get(...) is None` 分支就是"保留源文件里的
        值"；把它传进去（哪怕 translation 原样）在 keyed_source 布局下仍会命中
        `entry.msgstr = unit.translation`。产物层面的断言见
        FilteredUnitsKeepTheirSourceTextTests —— 这里只钉住单元集本身，
        因为它是那条产物不变量的机制。
        """
        units = (_unit("skip", "Всего %(count)s"),)
        pipeline = LocalBuildPipeline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            handed = []

            class _Recording(_FakeAdapter):
                def render(self, units, source, destination):
                    handed.extend(units)
                    return super().render(units, source, destination)

            pipeline.build(
                [ResourceBuild(_Recording(), source, units)],
                {},
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=root / "out",
                filtered_identities=[units[0].stable_identity],
            )
        self.assertEqual([], handed)

    def test_a_filtered_unit_does_not_create_a_same_source_group(self) -> None:
        """否则「跳过」只是把噪声从翻译搬到审查台。"""
        units = (
            _unit("a", "Общий текст", "a.mo"),
            _unit("b", "Общий текст", "b.mo"),
        )
        translations = {
            units[0].stable_identity: "译法甲",
            units[1].stable_identity: "译法乙",
        }
        pipeline = LocalBuildPipeline()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "x.mo"
            source.write_bytes(b"")
            result = pipeline.build(
                [ResourceBuild(_FakeAdapter(), source, units)],
                translations,
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=root / "out",
                filtered_identities=[units[1].stable_identity],
            )
            issues = json.loads(result.qa_json.read_text("utf-8"))["issues"]
            sidecar = json.loads(
                (result.qa_json.parent / INDEX_FILENAME).read_text("utf-8")
            )
        self.assertEqual(
            [], [i for i in issues if i["code"] == "same_source_inconsistency"]
        )
        self.assertEqual([], sidecar["same_source_groups"])
        # source_buckets 也不能含它 —— 面板用桶判断「这次编辑新造了分歧」，
        # 掺进跳过项会报出构建期永远不会出现的告警。
        self.assertEqual(1, sidecar["unit_total"])


class FilteredUnitsKeepTheirSourceTextTests(unittest.TestCase):
    """跳过 != 清空。这一组必须用**真实 Adapter**。

    对抗性审查实测：`_FakeAdapter.render` 无条件写字符串 "ok"，从不看 units，
    所以"渲染了几个单元"这类断言对产物内容一无所知。真实链路上
    `build()` 曾经把被跳过的词条按 `unit.translation or ""` 压成空串再交给
    render，gettext 的 `entry.msgstr = unit.translation` 于是把**源文抹掉**：
    生产配置 layout=keyed_source 下源文就存在 msgstr 里。而 filtered 同时被
    排除出全部 QA 判据，qa-report 里 error_count=0、QualityGate 放行，
    release 包照发 —— 一个把俄文原文清空的包，全绿。
    """

    def _render(self, *, filtered):
        import polib

        from localizer.adapters.resources.gettext import GettextAdapter

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = polib.POFile()
            catalog.append(polib.POEntry(msgid="tech_debug", msgstr="Отладка"))
            catalog.append(polib.POEntry(msgid="menu_play", msgstr="Играть"))
            source = root / "menu.mo"
            catalog.save_as_mofile(str(source))
            adapter = GettextAdapter(
                project_id="p",
                source_root=root,
                source_locale="ru-RU",
                target_locale="zh-Hans",
                options={"layout": "keyed_source"},
            )
            units = tuple(adapter.extract(source))
            skipped = next(u for u in units if u.logical_key == "tech_debug")
            play = next(u for u in units if u.logical_key == "menu_play")
            result = LocalBuildPipeline().build(
                [ResourceBuild(adapter, source, units)],
                {play.stable_identity: "开始游戏"},
                mode=BuildMode.PREVIEW,
                project_id="p",
                run_id="r1",
                output_root=root / "out",
                filtered_identities=[skipped.stable_identity] if filtered else [],
            )
            rendered = polib.mofile(str(next((root / "out").rglob("menu.mo"))))
            return {entry.msgid: entry.msgstr for entry in rendered}, result

    def test_a_filtered_entry_keeps_the_source_text_in_the_artifact(self) -> None:
        got, _result = self._render(filtered=True)
        self.assertEqual("Отладка", got["tech_debug"])

    def test_the_unfiltered_entry_is_still_translated(self) -> None:
        """对照组：证明这条链路真的会写译文，上面那条不是因为全都没写。"""
        got, _result = self._render(filtered=True)
        self.assertEqual("开始游戏", got["menu_play"])

    def test_the_difference_from_a_plain_untranslated_entry_is_visibility(self) -> None:
        """无 filter 时 keyed_source 同样把 msgstr 写空 —— 但那条是**响的**。

        它报 empty_translation（error），QualityGate 会拦下 release。
        filter 那条修复前是**哑的**：源文照样被抹掉，而 QA 零 error、闸门放行。
        两者的差别从来不是"会不会写空"，而是"有没有人知道"。
        """
        loud, loud_result = self._render(filtered=False)
        quiet, quiet_result = self._render(filtered=True)
        self.assertEqual("", loud["tech_debug"])
        self.assertFalse(loud_result.quality_gate.passed)
        self.assertEqual("Отладка", quiet["tech_debug"])
        self.assertTrue(quiet_result.quality_gate.passed)

    def test_a_green_gate_never_ships_a_wiped_source(self) -> None:
        """闸门放行和产物正确必须同时成立 —— 这正是当初漏掉的那个组合。"""
        got, result = self._render(filtered=True)
        self.assertTrue(result.quality_gate.passed)
        self.assertTrue(all(value for value in got.values()))


class PlannerFilteringTests(unittest.TestCase):
    def test_filtered_units_are_counted_separately_from_hits_and_pending(self) -> None:
        from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
        from localizer.application.translation_plan import TranslationPlanner
        from localizer.rules.validation import ValidationRule

        units = (_unit("keep", "Танк"), _unit("tech_x", "%(n)s"))
        rules = FilterRuleSet([FilterRule(id="t", reason="技术键", key_pattern=r"tech_.*")])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                plan = TranslationPlanner(
                    tm,
                    validation_rule=ValidationRule(),
                    global_exact_match="off",
                    filter_rules=rules,
                ).build([ResourceBuild(_FakeAdapter(), source, units)])
        self.assertEqual(1, len(plan.pending))
        self.assertEqual("keep", plan.pending[0].logical_key)
        self.assertEqual({units[1].stable_identity: "t"}, dict(plan.filtered))
        self.assertEqual(0, plan.tm_hits)
        self.assertEqual(1, plan.as_dict()["filtered_units"])
        self.assertEqual(1, plan.files[0].as_dict()["filtered_units"])

    def test_filtering_changes_the_plan_fingerprint(self) -> None:
        """跳过一批词条是计划的实质变化，父运行必须判为不兼容。"""
        from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
        from localizer.application.translation_plan import TranslationPlanner
        from localizer.rules.validation import ValidationRule

        units = (_unit("keep", "Танк"), _unit("tech_x", "%(n)s"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            prints = []
            for rules in (
                FilterRuleSet(),
                FilterRuleSet(
                    [FilterRule(id="t", reason="技术键", key_pattern=r"tech_.*")]
                ),
            ):
                with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                    prints.append(
                        TranslationPlanner(
                            tm,
                            validation_rule=ValidationRule(),
                            global_exact_match="off",
                            filter_rules=rules,
                        )
                        .build([ResourceBuild(_FakeAdapter(), source, units)])
                        .fingerprint
                    )
        self.assertNotEqual(prints[0], prints[1])


class NormalizationRuleTests(unittest.TestCase):
    def test_a_rule_is_applied_before_every_judgement(self) -> None:
        rule = NormalizationRuleSet(
            [NormalizationRule(id="nbsp", pattern=" ", replacement=" ")]
        )
        text, converged = rule.apply(
            "银 币", adapter_id="gettext", relative_path="a.mo"
        )
        self.assertEqual("银 币", text)
        self.assertTrue(converged)

    def test_normalized_text_is_what_gets_written_and_checked(self) -> None:
        """规则改写之后的文本才是判据的输入，也是写进产物的那一份。"""
        from localizer.rules.validation import ValidationRule

        rule = ValidationRule(
            normalization=NormalizationRuleSet(
                [NormalizationRule(id="nbsp", pattern=" ", replacement=" ")]
            )
        )
        summary = rule.validate_text(
            "银 币", adapter_id="gettext", relative_path="a.mo"
        )
        self.assertEqual("银 币", summary.text)
        self.assertEqual((), summary.issues)

    def test_an_empty_matching_pattern_is_refused(self) -> None:
        """`re.sub` 对空匹配会在每个字符间隙插入替换文本。"""
        with self.assertRaises(RuleDefinitionError) as ctx:
            NormalizationRule(id="bad", pattern="x*", replacement="-")
        self.assertIn("empty string", str(ctx.exception))

    def test_a_non_convergent_rule_set_is_reported_not_silently_truncated(self) -> None:
        """不收敛时取中间结果 = preview 和 release 产出不同的文本。"""
        rule = NormalizationRuleSet(
            [NormalizationRule(id="grow", pattern="a", replacement="aa")]
        )
        text, converged = rule.apply(
            "a", adapter_id="gettext", relative_path="x.mo"
        )
        self.assertFalse(converged)
        # 返回的必须是**原文**：中间态既不是操作者写的那句，也不是规则想要的
        # 那句，写进产物意味着 preview 与 release 可能拿到不同的文本。
        self.assertEqual("a", text)

    def test_convergence_does_not_depend_on_how_long_the_input_is(self) -> None:
        """判据曾经是「4 次以内到不动点没有」而不是「会不会收敛」。

        实测：同一套 `collapse-spaces` 规则、同一次运行，8 个空格的译文绿灯、
        9 个空格红灯（每轮减半，9 个需要 4 次以上），而 `normalization_unstable`
        是 error、直接阻断 release。同一条规则对不同长度的译文给出不同结论，
        操作者无从理解也无从修。
        """
        rules = NormalizationRuleSet(
            [NormalizationRule(id="collapse", pattern="  ", replacement=" ")]
        )
        for spaces in (1, 2, 3, 8, 9, 17, 64, 200):
            with self.subTest(spaces=spaces):
                text, converged = rules.apply(
                    "坦克" + " " * spaces + "已就绪",
                    adapter_id="gettext",
                    relative_path="a.mo",
                )
                self.assertTrue(converged)
                self.assertEqual("坦克 已就绪", text)

    def test_the_returned_text_is_always_the_real_fixed_point(self) -> None:
        """收敛时返回的必须是真不动点，再跑一轮不会变。"""
        rules = NormalizationRuleSet(
            [NormalizationRule(id="collapse", pattern="  ", replacement=" ")]
        )
        source = "a" + " " * 33 + "b"
        text, converged = rules.apply(
            source, adapter_id="gettext", relative_path="a.mo"
        )
        self.assertTrue(converged)
        again, _ = rules.apply(text, adapter_id="gettext", relative_path="a.mo")
        self.assertEqual(text, again)

    def test_a_permutation_rule_that_needs_many_passes_still_converges(self) -> None:
        """置换型规则既不增长也不收缩，靠固定轮数判断必然误报。"""
        rules = NormalizationRuleSet(
            [NormalizationRule(id="bubble", pattern="ba", replacement="ab")]
        )
        text, converged = rules.apply(
            "b" * 20 + "a" * 20, adapter_id="gettext", relative_path="a.mo"
        )
        self.assertTrue(converged)
        self.assertEqual("a" * 20 + "b" * 20, text)

    def test_chained_rules_still_converge(self) -> None:
        rules = NormalizationRuleSet(
            [
                NormalizationRule(id="one", pattern="A", replacement="B"),
                NormalizationRule(id="two", pattern="B", replacement="C"),
            ]
        )
        text, converged = rules.apply("A", adapter_id="g", relative_path="x.mo")
        self.assertEqual("C", text)
        self.assertTrue(converged)

    def test_scoping_by_path_and_adapter(self) -> None:
        rule = NormalizationRule(
            id="s", pattern="x", replacement="y", path_glob="ui/*.mo"
        )
        self.assertTrue(rule.applies_to("gettext", "ui/menu.mo"))
        self.assertFalse(rule.applies_to("gettext", "other/menu.mo"))

    def test_validation_rule_reports_non_convergence_as_an_error(self) -> None:
        from localizer.rules.validation import ValidationRule

        rule = ValidationRule(
            normalization=NormalizationRuleSet(
                [NormalizationRule(id="grow", pattern="坦", replacement="坦坦")]
            )
        )
        summary = rule.validate_text(
            "坦克", adapter_id="gettext", relative_path="a.mo"
        )
        self.assertIn(
            "normalization_unstable", [issue.code for issue in summary.issues]
        )
        self.assertTrue(summary.failed)

    def test_rewrites_text_drives_mappings_empty(self) -> None:
        """`mappings_empty` 是「能不能从产物反算 QA」的开关。

        加了会改写译文的规则却不把它算进去，面板的即时复核就会**二次应用**
        规则，给出与真实构建不同的结论 —— 而两边都自称通过了同一套判据。
        """
        from localizer.rules.validation import ValidationRule

        self.assertFalse(ValidationRule().rewrites_text)
        self.assertTrue(
            ValidationRule(residue_mappings={"a": "b"}).rewrites_text
        )
        self.assertTrue(
            ValidationRule(
                normalization=NormalizationRuleSet(
                    [NormalizationRule(id="n", pattern="a", replacement="b")]
                )
            ).rewrites_text
        )

    def test_build_marks_mappings_non_empty_when_normalization_exists(self) -> None:
        from localizer.rules.validation import ValidationRule

        units = (_unit("a", "Танк"),)
        pipeline = LocalBuildPipeline(
            validation_rule=ValidationRule(
                normalization=NormalizationRuleSet(
                    [NormalizationRule(id="n", pattern="！", replacement="!")]
                )
            )
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "menu.mo"
            source.write_bytes(b"")
            result = pipeline.build(
                [ResourceBuild(_FakeAdapter(), source, units)],
                {units[0].stable_identity: "坦克！"},
                mode=BuildMode.PREVIEW,
                project_id="wot-ru-zh",
                run_id="r1",
                output_root=root / "out",
            )
            sidecar = json.loads(
                (result.qa_json.parent / INDEX_FILENAME).read_text("utf-8")
            )
        self.assertFalse(sidecar["mappings_empty"])


class NormalizationLoaderTests(unittest.TestCase):
    def test_the_section_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(
                Path(temp),
                {
                    "normalization_rules": [
                        {"id": "nbsp", "pattern": " ", "replacement": " "}
                    ]
                },
            )
            rules = load_normalization_rules(path)
            self.assertEqual(1, len(rules.rules))
            # load_validation_rule 也必须把它带上，否则规则只在文件里存在。
            validation = load_validation_rule(path, source_locale="ru-RU")
            self.assertTrue(validation.rewrites_text)

    def test_missing_required_fields_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(
                Path(temp), {"normalization_rules": [{"id": "x", "pattern": "a"}]}
            )
            with self.assertRaises(RulesLoadError) as ctx:
                load_normalization_rules(path)
            self.assertIn("replacement", str(ctx.exception))

    def test_unknown_top_level_sections_are_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _rules_file(Path(temp), {"placeholder_rules": []})
            with self.assertRaises(RulesLoadError) as ctx:
                load_validation_rule(path)
            self.assertIn("placeholder_rules", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class PlannerAndBuildAgreeTests(unittest.TestCase):
    """规划期与构建期必须用**同一份**判据（对抗性审查 HIGH）。

    `TranslationPlanner._translation_is_valid` 原本自己拼了一遍顺序：先用
    **未归一化**的原文比占位符，之后才调 `validate_text`（NormalizationRule
    在它里面跑）。而 `inspect_unit` 是反过来的 —— 先归一化、再用**归一化后**的
    文本比占位符。

    两处口径相反的后果：一条会被 NormalizationRule 改动占位符形态的 TM 译文，
    规划期判「合法」原样进 `plan.translations`，构建期判 `placeholder_mismatch`
    (error)。它是 TM 命中不是 pending，模型不会被调用，`rebuild-from-run` 也
    只重试 pending —— 这条 error **没有任何带内自愈路径**。
    """

    def _rule(self):
        from localizer.rules.validation import ValidationRule

        # 把字面 \n 转成真实换行。paradox_yml 把 \n 注册为占位符。
        return ValidationRule(
            normalization=NormalizationRuleSet(
                [NormalizationRule(id="literal-nl", pattern=r"\\n", replacement="\n")]
            )
        )

    def _unit_and_translation(self):
        unit = TranslationUnit(
            project_id="p",
            adapter_id="paradox_yml",
            relative_path="a.yml",
            logical_key="k",
            source_text="Ready\\nSet",
            source_locale="en",
            target_locale="zh-Hans",
        )
        return unit, "就绪\\n开始"

    def test_the_planner_no_longer_accepts_what_build_will_reject(self) -> None:
        from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
        from localizer.application.translation_plan import TranslationPlanner

        unit, translation = self._unit_and_translation()
        rule = self._rule()
        pipeline = LocalBuildPipeline(validation_rule=rule)

        # 构建期怎么判？
        inspection = pipeline.inspect_unit(unit, translation)
        build_errors = sorted(
            r.code for r in inspection.records if r.severity == "error"
        )
        self.assertIn("placeholder_mismatch", build_errors)

        # 规划期必须给出同一个结论。
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                planner = TranslationPlanner(
                    tm, validation_rule=rule, global_exact_match="off"
                )
                self.assertFalse(planner._translation_is_valid(unit, translation))

    def test_a_clean_translation_is_still_accepted_by_both(self) -> None:
        """对照组：别把「口径一致」做成「一律拒绝」。"""
        from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
        from localizer.application.translation_plan import TranslationPlanner
        from localizer.rules.validation import ValidationRule

        unit = _unit("k", "Всего %(count)s")
        clean = "总共 %(count)s"
        rule = ValidationRule()
        pipeline = LocalBuildPipeline(validation_rule=rule)
        self.assertEqual(
            [], [r for r in pipeline.inspect_unit(unit, clean).records
                 if r.severity == "error"]
        )
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                planner = TranslationPlanner(
                    tm, validation_rule=rule, global_exact_match="off"
                )
                self.assertTrue(planner._translation_is_valid(unit, clean))

    def test_the_planner_now_also_rejects_build_blocking_junk(self) -> None:
        """这三条本来就会在构建期阻断整包，在规划期拦下只是把判据提前到
        「还能重译」的时刻。"""
        from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
        from localizer.application.translation_plan import TranslationPlanner
        from localizer.rules.validation import ValidationRule

        cases = {
            "untranslated": ("Танк", "Танк"),
            "invalid_control_character": ("Танк", "坦克\x00"),
            "placeholder_variant_residue": ("Танк", "残留 [PH_a1b2c3d4_0]"),
        }
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                planner = TranslationPlanner(
                    tm, validation_rule=ValidationRule(), global_exact_match="off"
                )
                for code, (source, translation) in cases.items():
                    with self.subTest(code=code):
                        unit = _unit("k", source)
                        self.assertFalse(
                            planner._translation_is_valid(unit, translation)
                        )


class RulesReachProductionTests(unittest.TestCase):
    """从 `rules.yaml` 一路到产物的**接线**必须有测试守着。

    对抗性审查点名了这一条，而它当场就抓到一个真的：`ProjectRunner.plan()` 里
    `filter_rules=load_filter_rules(...)` 曾被改成 `filter_rules=None` 并就那样
    提交了 —— R12 的 FilterRule 在生产路径上整个是死的，`load_filter_rules`
    只被 import、从不被调用，而 30 项单元测试全绿。

    单元测试测的是「零件对不对」，这一组测的是「零件有没有装上去」。
    """

    def _project(self, root: Path, rules_body: dict):
        import yaml

        from tests.test_rebuild_from_run import _Project

        project = _Project(root)
        config_path = project.config_path
        data = yaml.safe_load(config_path.read_text("utf-8"))
        rules_path = root / "rules.yaml"
        rules_path.write_text(
            yaml.safe_dump({"schema_version": 1, **rules_body}, allow_unicode=True),
            encoding="utf-8",
        )
        data["rules"]["file"] = str(rules_path)
        config_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return project

    def test_a_filter_rule_in_rules_yaml_actually_skips_the_unit(self) -> None:
        from tests.test_rebuild_from_run import _CountingProvider

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # _Project 的词条 logical_key 是 a/b/c/d。跳过 b。
            project = self._project(
                root,
                {
                    "filter_rules": [
                        {"id": "skip-b", "reason": "接线测试", "key_pattern": "b"}
                    ]
                },
            )
            provider = _CountingProvider()
            plan = project.runner(provider).plan()

        self.assertEqual(1, len(plan.filtered), "rules.yaml 里的 FilterRule 没生效")
        self.assertEqual({"skip-b"}, set(plan.filtered.values()))
        self.assertNotIn(
            "b", [unit.logical_key for unit in plan.pending]
        )

    def test_without_the_rule_the_unit_is_planned_normally(self) -> None:
        """对照组：证明上面那条不是因为词条本来就不在计划里。"""
        from tests.test_rebuild_from_run import _CountingProvider

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = self._project(root, {})
            plan = project.runner(_CountingProvider()).plan()
        self.assertEqual({}, dict(plan.filtered))
        self.assertIn("b", [unit.logical_key for unit in plan.pending])

    def test_a_filtered_unit_is_never_sent_to_the_provider(self) -> None:
        from localizer.application.local_build import BuildMode
        from tests.test_rebuild_from_run import _CountingProvider

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = self._project(
                root,
                {
                    "filter_rules": [
                        {"id": "skip-b", "reason": "接线测试", "key_pattern": "b"}
                    ]
                },
            )
            provider = _CountingProvider()
            result = project.runner(provider).run(
                mode=BuildMode.PREVIEW, run_id="r1"
            )
            skipped = project.identity("b")
            self.assertNotIn(skipped, provider.requested)
            # 也不该出现在 QA 报告里。
            issues = json.loads(result.build.qa_json.read_text("utf-8"))["issues"]
            self.assertNotIn(skipped, {i["stable_identity"] for i in issues})

    def test_a_normalization_rule_in_rules_yaml_reaches_the_judgement(self) -> None:
        """NormalizationRule 走的是 `load_validation_rule`，同样要有接线证据。"""
        from tests.test_rebuild_from_run import _CountingProvider

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = self._project(
                root,
                {
                    "normalization_rules": [
                        {"id": "nbsp", "pattern": "\u00a0", "replacement": " "}
                    ]
                },
            )
            runner = project.runner(_CountingProvider())
            # 先跑一次真实规划：多了 normalization_rules 这一段之后，loader
            # 不该因为未知顶层键而炸。
            runner.plan()
            # 再拿**规划自己会用的**那份 ValidationRule。直接调 loader 只能证明
            # 「loader 会读」—— 那正是 `filter_rules=None` 那次回归躲过去的方式。
            loaded = runner._validation_rule_for_test()
            self.assertTrue(loaded.rewrites_text)
            self.assertEqual(
                ("坦克 克", True),
                loaded.normalization.apply(
                    "坦克 克",
                    adapter_id="gettext",
                    relative_path="menu.mo",
                ),
            )
