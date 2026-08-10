"""多语言集成：源语言残留检测不再硬编码西里尔。

问题：`CYRILLIC_RE = [\\u0400-\\u052f]` 只对俄译中成立。换成英译中项目时这条 QA
**恒真通过** —— 中文译文里本来就不会出现西里尔字母。M7 验收要求「至少一个不同
源语言项目完成扫描、翻译和 QA」，光换源语言标签而不换检测器，拿到的是空头保证。

这组测试守两件事：
1. 每种源语言的检测器都真的会红也会绿（不是恒真也不是恒假）；
2. 抽象没做偏 —— 拉丁系需要的「先剥离占位符」「单词放过、连续词才报」两条，
   俄语系从来不需要，正是这两条决定抽象对不对。
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

from localizer.rules.language_profile import (
    BUILTIN_PROFILES,
    LanguageProfile,
    available_profiles,
    build_profile,
    for_locale,
    normalize_locale,
)
from localizer.rules.loader import RulesLoadError, load_validation_rule
from localizer.rules.placeholder import register_placeholder_syntax
from localizer.rules.validation import ValidationRule


class LocaleResolutionTests(unittest.TestCase):
    def test_normalizes_common_locale_shapes(self) -> None:
        for raw, expected in (
            ("ru-RU", "ru"), ("en_US", "en"), ("zh-Hans", "zh"),
            ("ko", "ko"), ("EN", "en"), ("", ""),
        ):
            with self.subTest(locale=raw):
                self.assertEqual(expected, normalize_locale(raw))

    def test_target_side_languages_have_no_profile(self) -> None:
        # 目标语言是中文时，对中文做「源语言残留」检测没有意义。
        self.assertIsNone(for_locale("zh-Hans"))

    def test_unknown_language_is_none_not_a_crash(self) -> None:
        self.assertIsNone(for_locale("xx-YY"))

    def test_builtin_profiles_cover_the_common_source_languages(self) -> None:
        for code in ("ru", "en", "ko", "ja"):
            self.assertIn(code, available_profiles())


class ResidueDetectionTests(unittest.TestCase):
    def _residue(self, rule: ValidationRule, text: str) -> tuple:
        summary = rule.validate_text(text, adapter_id="gettext", relative_path="ui.mo")
        for issue in summary.issues:
            if issue.code == "source_language_residue":
                return tuple(issue.details["fragments"])
        return ()

    def test_cyrillic_profile_behaves_as_before(self) -> None:
        rule = ValidationRule(profile=BUILTIN_PROFILES["ru"])
        self.assertEqual((), self._residue(rule, "重型坦克"))
        self.assertTrue(self._residue(rule, "重型坦克 Тяжёлый"))

    def test_latin_profile_lets_single_words_through(self) -> None:
        # 中文译文里合法地含专名、缩写、型号。单个拉丁词一律放过，
        # 否则英译中项目的报告会被误报淹没。
        rule = ValidationRule(profile=BUILTIN_PROFILES["en"])
        for text in ("重型坦克 Panzer", "剩余 HP", "IS-3 坦克", "获得 XP 奖励"):
            with self.subTest(text=text):
                self.assertEqual((), self._residue(rule, text))

    def test_latin_profile_reports_consecutive_words(self) -> None:
        rule = ValidationRule(profile=BUILTIN_PROFILES["en"])
        for text in ("Rearmament Program", "重整军备 Expand the Works"):
            with self.subTest(text=text):
                self.assertTrue(self._residue(rule, text), msg=f"{text} 应被判残留")

    def test_latin_profile_is_not_vacuously_true(self) -> None:
        # 这是整个改动的要害：换掉硬编码之前，英译中项目上这条检查恒真通过。
        rule = ValidationRule(profile=BUILTIN_PROFILES["en"])
        clean = self._residue(rule, "完全翻译好的中文")
        dirty = self._residue(rule, "Our forces are in shambles")
        self.assertEqual((), clean)
        self.assertTrue(dirty)

    def test_placeholders_are_stripped_before_latin_detection(self) -> None:
        # 拉丁系画像的硬需求。$COUNTRY$ / [Root.GetName] / %(count)d 全是纯 ASCII，
        # 不先掩码就会把每一个占位符都报成源语言残留。
        register_placeholder_syntax(
            "profile_test_fmt",
            (r"\$[^$\s]*\$", r"£[^£\s]*£", r"§.", r"\[[A-Za-z][^\]\s]*\]"),
        )
        rule = ValidationRule(profile=BUILTIN_PROFILES["en"])
        summary = rule.validate_text(
            "重整军备 §Y$COUNTRY_ADJ$§! 需要 £political_power£ [Root.GetNameDef]",
            adapter_id="profile_test_fmt",
            relative_path="ui.yml",
        )
        self.assertEqual(
            [], [i.code for i in summary.issues if i.code == "source_language_residue"]
        )

    def test_cyrillic_profile_never_needed_the_placeholder_stripping(self) -> None:
        # 对照组：俄语画像下占位符不含西里尔，剥不剥离结果都一样。
        rule = ValidationRule(profile=BUILTIN_PROFILES["ru"])
        self.assertEqual(
            (), self._residue(rule, "剩余 %(count)d 天 {value} <b>x</b>")
        )

    def test_hangul_and_kana_profiles_report_a_single_occurrence(self) -> None:
        for code, dirty in (("ko", "获得 아이템"), ("ja", "获得 アイテム")):
            with self.subTest(code=code):
                rule = ValidationRule(profile=BUILTIN_PROFILES[code])
                self.assertTrue(self._residue(rule, dirty))
                self.assertEqual((), self._residue(rule, "获得道具"))

    def test_allowlist_still_matches_whole_tokens(self) -> None:
        # H3 修过的语义必须保住：整词匹配，不做子串剥离。
        rule = ValidationRule(
            profile=BUILTIN_PROFILES["ru"], residue_exact_allowlist=("КВ-1",)
        )
        self.assertEqual((), self._residue(rule, "重型坦克 КВ-1"))
        self.assertEqual((), self._residue(rule, "重型坦克 (КВ-1)"))
        self.assertTrue(self._residue(rule, "重型坦克 КВ-1234"))
        self.assertTrue(self._residue(rule, "重型坦克 xКВ-1x"))

    def test_message_names_the_actual_script(self) -> None:
        rule = ValidationRule(profile=BUILTIN_PROFILES["en"])
        summary = rule.validate_text(
            "Rearmament Program", adapter_id="gettext", relative_path="ui.mo"
        )
        message = next(
            i.message for i in summary.issues if i.code == "source_language_residue"
        )
        self.assertIn("Latin", message)
        self.assertNotIn("Cyrillic", message)


class ProfileConstructionTests(unittest.TestCase):
    def test_custom_pattern_overrides_the_builtin(self) -> None:
        profile = build_profile(code="en", residue_pattern=r"XYZ", min_run=1)
        self.assertEqual("XYZ", profile.residue_pattern)

    def test_min_run_override_keeps_the_builtin_pattern(self) -> None:
        profile = build_profile(source_locale="en-US", min_run=1)
        self.assertEqual(BUILTIN_PROFILES["en"].residue_pattern, profile.residue_pattern)
        self.assertEqual(1, profile.min_run)
        # min_run=1 之后单个拉丁词也会被报 —— 项目可以按需收紧。
        rule = ValidationRule(profile=profile)
        summary = rule.validate_text(
            "重型坦克 Panzer", adapter_id="gettext", relative_path="ui.mo"
        )
        self.assertTrue(
            [i for i in summary.issues if i.code == "source_language_residue"]
        )

    def test_bad_pattern_fails_at_construction(self) -> None:
        import re

        with self.assertRaises(re.error):
            LanguageProfile("x", r"[unclosed", 1)

    def test_min_run_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            LanguageProfile("x", r"a", 0)


class RulesFileTests(unittest.TestCase):
    def _write(self, temp: Path, body: str) -> Path:
        path = temp / "rules.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_legacy_cyrillic_section_still_loads(self) -> None:
        # 现网项目的 rules.yaml 都还在用旧段名，不能一刀切。
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "schema_version: 1\ncyrillic:\n  exact_allowlist: ['КВ-1']\n",
            )
            rule = load_validation_rule(path, source_locale="ru-RU")
        self.assertEqual(("КВ-1",), rule.residue_exact_allowlist)
        self.assertEqual("ru", rule.profile.code)

    def test_language_section_selects_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "schema_version: 1\nlanguage:\n  source_profile: en\n"
                "  residue:\n    exact_allowlist: ['HOI4']\n    min_run: 3\n",
            )
            rule = load_validation_rule(path)
        self.assertEqual("en", rule.profile.code)
        self.assertEqual(3, rule.profile.min_run)
        self.assertEqual(("HOI4",), rule.residue_exact_allowlist)

    def test_source_locale_from_project_config_picks_the_profile(self) -> None:
        # rules.yaml 没写 language 段时，用项目的 languages.source 兜底。
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(Path(temp), "schema_version: 1\n")
            rule = load_validation_rule(path, source_locale="ko-KR")
        self.assertEqual("ko", rule.profile.code)

    def test_both_section_names_at_once_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "schema_version: 1\ncyrillic:\n  exact_allowlist: []\n"
                "language:\n  source_profile: en\n",
            )
            with self.assertRaises(RulesLoadError):
                load_validation_rule(path)

    def test_unknown_profile_names_the_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp), "schema_version: 1\nlanguage:\n  source_profile: klingon\n"
            )
            with self.assertRaises(RulesLoadError) as ctx:
                load_validation_rule(path)
        self.assertIn("ru", str(ctx.exception))

    def test_custom_residue_pattern_from_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "schema_version: 1\nlanguage:\n  source_profile: en\n"
                "  residue_pattern: 'ZZZ+'\n",
            )
            rule = load_validation_rule(path)
        self.assertEqual("ZZZ+", rule.profile.residue_pattern)

    def test_bad_min_run_is_a_rules_load_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "schema_version: 1\nlanguage:\n  source_profile: en\n"
                "  residue:\n    min_run: 0\n",
            )
            with self.assertRaises(RulesLoadError):
                load_validation_rule(path)

    def test_repository_rules_file_still_loads(self) -> None:
        rule = load_validation_rule(
            ROOT / "tests" / "fixtures" / "ru-rules.yaml", source_locale="ru-RU"
        )
        self.assertEqual("ru", rule.profile.code)


if __name__ == "__main__":
    unittest.main()
