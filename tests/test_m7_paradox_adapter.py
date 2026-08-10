"""M7 · Paradox 本地化 YML —— 第二种资源格式的端到端验证。

这个文件同时承担两件事：
1. Paradox Adapter 自身的契约测试；
2. **M7 验收**：证明核心不依赖 WOT / 俄语 / Gettext。第二个项目源语言是英语、
   格式不是 Gettext、输出路径与源路径不同，整条 scan→翻译→QA→构建→制品
   链路必须跑通，而且过程中不需要改内核。

语料是手写的仿真文件（tests/fixtures/paradox/），不复制任何真实 mod 文本，
也不解包任何游戏资源。
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.resources import build_adapter
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import ProjectRunner
from localizer.config.models import ProjectConfig
from localizer.ports.provider import ProviderResponse
from localizer.rules.placeholder import PlaceholderRule

FIXTURES = ROOT / "tests" / "fixtures" / "paradox"
ENGLISH = FIXTURES / "localisation" / "english"
# 故意做坏的样本放在 localisation/ 之外，
# 免得被端到端语料的 **/*.yml 扫进去。
INVALID = FIXTURES / "invalid"


def _adapter(**overrides):
    kwargs = dict(
        project_id="hoi4-demo",
        source_root=FIXTURES,
        source_locale="en",
        target_locale="zh-Hans",
    )
    kwargs.update(overrides)
    return build_adapter("paradox_yml", **kwargs)


class ProbeTests(unittest.TestCase):
    def test_probe_reads_the_header_not_the_suffix(self) -> None:
        adapter = _adapter()
        # .yml 后缀被无数格式共用，判据必须是首行的 l_<language>: header。
        self.assertGreater(adapter.probe(ENGLISH / "demo_focus_l_english.yml"), 0.9)
        self.assertEqual(0.0, adapter.probe(FIXTURES / "descriptor.mod"))

    def test_other_source_languages_are_not_claimed(self) -> None:
        adapter = _adapter()
        french = FIXTURES / "localisation" / "french" / "demo_focus_l_french.yml"
        self.assertEqual(0.0, adapter.probe(french))

    def test_missing_file_probes_zero(self) -> None:
        self.assertEqual(0.0, _adapter().probe(ENGLISH / "nope.yml"))


class ParsingTests(unittest.TestCase):
    def _by_key(self, name: str) -> dict:
        return {u.logical_key: u for u in _adapter().extract(ENGLISH / name)}

    def test_key_is_independent_of_source_text(self) -> None:
        # Gettext 把 msgid 当 logical_key，源文一改身份就变；Paradox 的 key 独立。
        # 这正好验证 stable_identity 不含 source_text 这条设计。
        units = _adapter().extract(ENGLISH / "demo_focus_l_english.yml")
        original = units[0]
        edited = dataclasses.replace(original, source_text="Totally rewritten")
        self.assertEqual(original.stable_identity, edited.stable_identity)
        self.assertNotEqual(original.source_fingerprint, edited.source_fingerprint)

    def test_version_suffix_is_preserved_in_metadata(self) -> None:
        units = self._by_key("demo_focus_l_english.yml")
        self.assertEqual("0", units["DEMO_focus_rearmament"].metadata["paradox_version"])
        self.assertEqual(
            "1", units["DEMO_focus_rearmament_desc"].metadata["paradox_version"]
        )

    def test_entries_without_a_version_are_supported(self) -> None:
        units = self._by_key("demo_ui_l_english.yml")
        self.assertEqual("", units["UI_MENU_NEW_GAME"].metadata["paradox_version"])
        self.assertEqual("New Game", units["UI_MENU_NEW_GAME"].source_text)

    def test_lexer_survives_the_usual_traps(self) -> None:
        units = self._by_key("demo_tricky_l_english.yml")
        # 值内冒号——标准 YAML 解析器会在这里解错
        self.assertEqual(
            "Ratio 3:1 — see: the field manual", units["TRICKY_ratio"].source_text
        )
        # 值内 # ——尾注释剥离只能在引号之外生效
        self.assertEqual("Use #1 priority slot", units["TRICKY_hash"].source_text)
        # key 与值之间用 TAB
        self.assertEqual(
            "Tab separated from the key", units["TRICKY_tab"].source_text
        )
        # 以转义引号结尾——贪婪匹配的正确性
        self.assertEqual('He said "go"', units["TRICKY_quote_end"].source_text)

    def test_trailing_comment_is_not_part_of_the_value(self) -> None:
        units = self._by_key("demo_focus_l_english.yml")
        self.assertEqual(
            "Completed $PROGRESS|%1$ of the plan",
            units["DEMO_progress_tooltip"].source_text,
        )

    def test_sidecar_translations_are_backfilled(self) -> None:
        units = self._by_key("demo_focus_l_english.yml")
        self.assertEqual("重整军备计划", units["DEMO_focus_rearmament"].translation)
        # sidecar 里的空串不算已译，必须落回待翻译
        self.assertIsNone(units["DEMO_focus_industry"].translation)

    def test_duplicate_keys_are_rejected(self) -> None:
        # logical_key 每文件唯一是 Adapter 契约：stable_identity 由它构成，
        # 重复会让按身份索引的下游结构（placeholder_maps 等）互相覆盖。
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dup_l_english.yml"
            path.write_bytes(
                "﻿l_english:\n K:0 \"a\"\n K:0 \"b\"\n".encode("utf-8")
            )
            with self.assertRaises(ValueError):
                _adapter(source_root=Path(temp)).extract(path)


class PlaceholderTests(unittest.TestCase):
    def test_all_five_paradox_syntaxes_are_recognised(self) -> None:
        units = {
            u.logical_key: u
            for u in _adapter().extract(ENGLISH / "demo_focus_l_english.yml")
        }
        rule = PlaceholderRule.for_adapter("paradox_yml")
        masked = rule.mask(units["DEMO_focus_rearmament_desc"].source_text)
        values = set(masked.token_to_value.values())
        for expected in ("§Y", "§!", "$COUNTRY_ADJ$", "[Root.GetNameDef]",
                         "£political_power£", "$VALUE|*0$"):
            self.assertIn(expected, values)
        self.assertEqual((), rule.find_unmasked_candidates(masked.masked_text))

    def test_generic_and_paradox_syntax_coexist(self) -> None:
        units = {
            u.logical_key: u
            for u in _adapter().extract(ENGLISH / "demo_ui_l_english.yml")
        }
        rule = PlaceholderRule.for_adapter("paradox_yml")
        masked = rule.mask(units["UI_STOCKPILE"].source_text)
        values = list(masked.token_to_value.values())
        self.assertIn("£manpower£", values)   # Paradox 预设
        self.assertIn("%d", values)            # generic printf
        self.assertTrue(rule.round_trip(units["UI_STOCKPILE"].source_text))

    def test_round_trip_holds_for_every_fixture_entry(self) -> None:
        rule = PlaceholderRule.for_adapter("paradox_yml")
        for name in ("demo_focus_l_english.yml", "demo_ui_l_english.yml",
                     "demo_tricky_l_english.yml"):
            for unit in _adapter().extract(ENGLISH / name):
                with self.subTest(name=name, key=unit.logical_key):
                    self.assertTrue(rule.round_trip(unit.source_text))


class DestinationTests(unittest.TestCase):
    def test_language_is_rewritten_in_both_directory_and_filename(self) -> None:
        adapter = _adapter()
        planned = adapter.plan_destination(
            ENGLISH / "demo_focus_l_english.yml", Path("OUT")
        )
        self.assertEqual(
            "OUT/localisation/simp_chinese/demo_focus_l_simp_chinese.yml",
            planned.as_posix(),
        )

    def test_kernel_honours_plan_destination(self) -> None:
        from localizer.ports.resource import resolve_destination

        adapter = _adapter()
        resolved = resolve_destination(
            adapter, ENGLISH / "demo_focus_l_english.yml", Path("OUT")
        )
        self.assertIn("simp_chinese", resolved.as_posix())

    def test_adapters_without_a_planner_fall_back_to_the_source_layout(self) -> None:
        from localizer.ports.resource import resolve_destination

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "messages.po"
            source.write_text('msgid ""\nmsgstr ""\n', encoding="utf-8")
            gettext = build_adapter(
                "gettext", project_id="p", source_root=Path(temp),
                source_locale="ru-RU", target_locale="zh-Hans",
            )
            self.assertEqual(
                "OUT/messages.po",
                resolve_destination(gettext, source, Path("OUT")).as_posix(),
            )


class RenderTests(unittest.TestCase):
    def test_untranslated_round_trip_is_semantically_equal(self) -> None:
        adapter = _adapter()
        source = ENGLISH / "demo_focus_l_english.yml"
        units = [
            dataclasses.replace(u, translation=None) for u in adapter.extract(source)
        ]
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "rt_l_simp_chinese.yml"
            result = adapter.render(units, source, out)
            self.assertTrue(result.validation.valid, result.validation.errors)
            back = _adapter(source_root=Path(temp)).extract(out)
        self.assertEqual(
            [(u.logical_key, u.source_text) for u in adapter.extract(source)],
            [(u.logical_key, u.source_text) for u in back],
        )

    def test_output_keeps_the_bom_and_rewrites_the_header(self) -> None:
        adapter = _adapter()
        source = ENGLISH / "demo_focus_l_english.yml"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "x_l_simp_chinese.yml"
            adapter.render(adapter.extract(source), source, out)
            raw = out.read_bytes()
        # Paradox 要求 UTF-8 带 BOM，否则游戏读不出非 ASCII。
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw[3:].startswith(b"l_simp_chinese:"))

    def test_malformed_lines_are_preserved_not_dropped(self) -> None:
        adapter = _adapter()
        source = INVALID / "demo_broken_l_english.yml"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "b_l_simp_chinese.yml"
            adapter.render(adapter.extract(source), source, out)
            text = out.read_text(encoding="utf-8-sig")
        # 丢掉不认识的行等于静默损坏源数据。
        self.assertIn("BROKEN_missing_quote:0 Rearmament Program", text)


class ValidateTests(unittest.TestCase):
    def test_missing_bom_is_reported(self) -> None:
        result = _adapter().validate(INVALID / "demo_nobom_l_english.yml")
        self.assertFalse(result.valid)
        self.assertTrue(any("BOM" in e for e in result.errors))

    def test_unparsable_entry_is_reported_with_its_line(self) -> None:
        result = _adapter().validate(INVALID / "demo_broken_l_english.yml")
        self.assertFalse(result.valid)
        self.assertTrue(any("line 2" in e for e in result.errors))

    def test_good_file_validates(self) -> None:
        self.assertTrue(_adapter().validate(ENGLISH / "demo_focus_l_english.yml").valid)


class SecondProjectEndToEndTests(unittest.TestCase):
    """M7 验收：英语源 + 非 Gettext 格式，跑通完整 release 流水线。"""

    class Provider:
        """按占位符原样保留的规则产出译文。

        对**没有实义文本**的条目（`"$VALUE$"` 这种纯占位符）原样返回 ——
        这是真实模型的正确行为，也是 `test_pure_placeholder_entry_does_not_block
        _the_release` 唯一能走到判定分支的方式。

        这一点最初写错了：假 Provider 对 `$VALUE$` 也伪造「中文译文N」，
        译文永远不等于源文，那条测试从来没走到 local_build 的相等分支，
        绿灯是假的。而当时 local_build 确实缺少守卫 —— 假测试正好掩盖了真缺陷。
        """

        def __init__(self) -> None:
            self.calls = 0

        def translate(self, prompt, units):
            from localizer.rules.placeholder import has_meaningful_text

            self.calls += 1
            lines = []
            for index, unit in enumerate(units, start=1):
                if not has_meaningful_text(unit.source_text):
                    lines.append(f"[{index}] {unit.source_text}")
                    continue
                # 保留全部占位符 token，其余替换成中文。
                import re

                tokens = re.findall(r"\[PH_[0-9a-f]{8}_\d+\]", unit.source_text)
                lines.append(f"[{index}] 中文译文{index}{''.join(tokens)}")
            return ProviderResponse("\n".join([*lines, "---END---"]))

    def _config(self, root: Path) -> ProjectConfig:
        for name, body in (
            ("prompt.md", "Translate the game text."),
            ("glossary.yaml", "schema_version: 1\nterms: []\n"),
            ("rules.yaml", "schema_version: 1\ncyrillic:\n  exact_allowlist: []\n"),
        ):
            (root / name).write_text(body, encoding="utf-8")
        data = {
            "schema_version": 1,
            "project": {
                "id": "hoi4-demo",
                "name": "Paradox demo",
                "game_version": "1.14",
            },
            "paths": {
                "source": FIXTURES,
                "workspace": root / "var",
                "output": root / "out",
            },
            # 源语言是英语 —— M7 要求「至少一个不同源语言项目」。
            "languages": {"source": "en", "target": "zh-Hans"},
            "resources": {
                "adapters": [
                    {
                        "type": "paradox_yml",
                        "include": ["**/*.yml"],
                        # invalid/ 是给单测用的故意做坏的样本，不属于语料。
                        "exclude": ["**/invalid/**"],
                    }
                ]
            },
            "prompt": {"template": root / "prompt.md"},
            "glossary": {"file": root / "glossary.yaml"},
            "rules": {"file": root / "rules.yaml"},
            "provider": {
                "base_url": "https://provider.invalid/v1",
                "api_key_env": "M7_TEST_KEY",
                "model": "fake",
            },
            "tm": {"database": root / "tm.sqlite3"},
        }
        return (
            ProjectConfig.model_validate(data)
            if hasattr(ProjectConfig, "model_validate")
            else ProjectConfig.parse_obj(data)
        )

    def test_release_pipeline_runs_on_a_non_gettext_english_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = self.Provider()
            result = ProjectRunner(self._config(root), provider=provider).run(
                mode=BuildMode.RELEASE, run_id="m7-001"
            )

            self.assertGreater(result.extracted_units, 5)
            self.assertEqual(0, result.failed_units)
            self.assertIsNotNone(result.build.bundle, "release 必须产出正式制品")

            # 输出路径按 Adapter 的规划改写，而不是照抄源相对路径。
            rendered = [p.as_posix() for p in result.build.rendered]
            self.assertTrue(
                any("simp_chinese" in p for p in rendered),
                msg=f"输出未按语言改写目录: {rendered}",
            )
            self.assertFalse(any("/english/" in p for p in rendered))

            # 产物仍是合法的 Paradox 文件。
            adapter = _adapter()
            for path in result.build.rendered:
                with self.subTest(path=path.name):
                    self.assertTrue(adapter.validate(path).valid)

            manifest = json.loads(
                result.build.bundle.manifest.read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["quality_gate_passed"])

    def test_placeholders_survive_the_whole_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ProjectRunner(self._config(root), provider=self.Provider()).run(
                mode=BuildMode.RELEASE, run_id="m7-002"
            )
            produced = list((root / "out" / "release" / "m7-002").rglob("*.yml"))
            self.assertTrue(produced)
            adapter = _adapter(source_root=root / "out" / "release" / "m7-002")
            found = {
                unit.logical_key: unit.source_text
                for path in produced
                for unit in adapter.extract(path)
            }
            # 占位符必须原封不动地穿过 mask → 翻译 → restore → 回写。
            self.assertIn("$COUNTRY_ADJ$", found["DEMO_focus_rearmament_desc"])
            self.assertIn("£political_power£", found["DEMO_focus_rearmament_desc"])
            self.assertIn("[Root.GetNameDef]", found["DEMO_focus_rearmament_desc"])
            self.assertIn("§Y", found["DEMO_focus_rearmament_desc"])

    def test_pure_placeholder_entry_does_not_block_the_release(self) -> None:
        # DEMO_placeholder_only 的值就是 "$VALUE$"，译文必然与源文相同。
        # 判 untranslated 会直接阻断 release —— 任何键值型格式都有这类条目。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = ProjectRunner(self._config(root), provider=self.Provider()).run(
                mode=BuildMode.RELEASE, run_id="m7-003"
            )
            report = json.loads(result.build.qa_json.read_text(encoding="utf-8"))
            offending = [
                issue
                for issue in report["issues"]
                if issue["code"] == "untranslated"
            ]
            self.assertEqual([], offending)


if __name__ == "__main__":
    unittest.main()
