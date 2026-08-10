from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path

import polib


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.resources.gettext import GettextAdapter
from localizer.adapters.resources.paratranz_json import ParaTranzJsonAdapter
from localizer.application.response_parser import NumberingError, ResponseParser, TruncatedResponse
from localizer.ports.provider import ProviderResponse
from localizer.rules.loader import load_validation_rule
from localizer.rules.placeholder import PlaceholderRule
from localizer.rules.validation import RuleScope, ValidationRule


def build_mo(path: Path, message: str) -> None:
    catalog = polib.POFile()
    catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
    catalog.append(polib.POEntry(msgid="Закрыть", msgstr=message, msgctxt="button"))
    catalog.append(polib.POEntry(msgid="Осталось %(count)04d", msgstr="剩余 %(count)04d"))
    catalog.save_as_mofile(str(path))


class GettextAdapterTests(unittest.TestCase):
    def test_wot_source_filter_skips_non_russian_sentinels_and_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "wot.mo"
            catalog = polib.POFile()
            catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
            catalog.append(polib.POEntry(msgid="empty", msgstr="?empty?"))
            catalog.append(polib.POEntry(msgid="percent", msgstr="95%"))
            catalog.append(polib.POEntry(msgid="translated", msgstr="已经是中文"))
            catalog.append(polib.POEntry(msgid="russian", msgstr="Привет"))
            catalog.save_as_mofile(str(source))
            adapter = GettextAdapter(
                project_id="wot",
                source_root=root,
                source_locale="ru-RU",
                target_locale="zh-Hans",
                options={
                    "layout": "keyed_source",
                    "source_filter": "cyrillic_without_cjk",
                },
            )
            extracted = list(adapter.extract(source))
            self.assertEqual(["russian"], [unit.logical_key for unit in extracted])

    def test_keyed_source_layout_uses_msgid_as_key_and_msgstr_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "wot.mo"
            catalog = polib.POFile()
            catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
            catalog.append(
                polib.POEntry(msgid="bonusXp/description", msgstr="Привет %(name)s")
            )
            catalog.save_as_mofile(str(source))
            adapter = GettextAdapter(
                project_id="wot",
                source_root=root,
                source_locale="ru",
                target_locale="zh",
                options={"layout": "keyed_source", "empty_source": "skip"},
            )
            units = list(adapter.extract(source))
            self.assertEqual("bonusXp/description", units[0].logical_key)
            self.assertEqual("Привет %(name)s", units[0].source_text)
            self.assertEqual("", units[0].translation)
            units[0] = units[0].__class__(
                **{**units[0].__dict__, "translation": "你好 %(name)s"}
            )
            output = root / "out" / "wot.mo"
            adapter.render(units, source, output)
            self.assertEqual("你好 %(name)s", polib.mofile(str(output))[0].msgstr)

    def test_same_basename_keeps_relative_identity_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            first = root / "a" / "same.mo"
            second = root / "b" / "same.mo"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            build_mo(first, "关闭 A")
            build_mo(second, "关闭 B")
            adapter = GettextAdapter(
                project_id="wot",
                source_root=root,
                source_locale="ru",
                target_locale="zh",
            )
            first_units = adapter.extract(first)
            second_units = adapter.extract(second)
            self.assertNotEqual(first_units[0].stable_identity, second_units[0].stable_identity)

            output_root = Path(temp) / "output"
            first_output = output_root / "a" / "same.mo"
            second_output = output_root / "b" / "same.mo"
            adapter.render(first_units, first, first_output)
            adapter.render(second_units, second, second_output)
            self.assertTrue(adapter.validate(first_output).valid)
            self.assertTrue(adapter.validate(second_output).valid)
            self.assertEqual("关闭 A", polib.mofile(str(first_output))[0].msgstr)
            self.assertEqual("关闭 B", polib.mofile(str(second_output))[0].msgstr)
            self.assertEqual([], list(output_root.rglob("_tmp.po")))
            self.assertEqual([], list(output_root.rglob("*.tmp")))

    def test_render_updates_translation_and_keeps_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            source = root / "ui.mo"
            build_mo(source, "关闭")
            adapter = GettextAdapter(
                project_id="wot",
                source_root=root,
                source_locale="ru",
                target_locale="zh",
            )
            units = list(adapter.extract(source))
            units[0] = units[0].__class__(**{**units[0].__dict__, "translation": "退出"})
            destination = Path(temp) / "out" / "ui.mo"
            adapter.render(units, source, destination)
            rendered = polib.mofile(str(destination))
            self.assertEqual("退出", rendered[0].msgstr)
            self.assertEqual("button", rendered[0].msgctxt)


class ParaTranzJsonAdapterTests(unittest.TestCase):
    def test_round_trip_preserves_stage_and_updates_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pt.json"
            source.write_text(
                json.dumps(
                    [{"id": 7, "key": "close", "original": "Закрыть", "translation": "", "stage": 5}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            adapter = ParaTranzJsonAdapter(
                project_id="wot",
                source_root=root,
                source_locale="ru",
                target_locale="zh",
            )
            units = list(adapter.extract(source))
            self.assertEqual(5, units[0].metadata["stage"])
            units[0] = units[0].__class__(**{**units[0].__dict__, "translation": "关闭"})
            destination = root / "out" / "pt.json"
            adapter.render(units, source, destination)
            rendered = json.loads(destination.read_text("utf-8"))
            self.assertEqual("关闭", rendered[0]["translation"])
            self.assertEqual(5, rendered[0]["stage"])


class PlaceholderAndValidationTests(unittest.TestCase):
    def test_placeholder_round_trip_and_printf_variants(self) -> None:
        text = "%(name)s: %(count)04d / %05.2f %% {vehicle} <b>OK</b>\n"
        rule = PlaceholderRule()
        masked = rule.mask(text, namespace="unit-1")
        self.assertEqual(text, rule.restore(masked.masked_text, masked))
        self.assertEqual(
            ("%(name)s", "%(count)04d", "%05.2f", "%%", "{vehicle}", "<b>", "</b>", "\n"),
            rule.extract(text),
        )
        self.assertEqual(len(masked.tokens), len(set(masked.tokens)))

    def test_placeholder_mismatch_is_error(self) -> None:
        placeholder = PlaceholderRule().mask("剩余 %(count)d", namespace="u")
        issues = ValidationRule().validate_masked_translation("剩余", placeholder)
        self.assertEqual("placeholder_mismatch", issues[0].code)
        self.assertEqual("error", issues[0].severity)

    def test_cyrillic_defaults_to_failure_without_transliteration(self) -> None:
        summary = ValidationRule().validate_text(
            "统帅 Полководец", adapter_id="gettext", relative_path="ui/main.mo"
        )
        self.assertTrue(summary.failed)
        self.assertEqual("统帅 Полководец", summary.text)
        self.assertEqual("source_language_residue", summary.issues[0].code)

    def test_cyrillic_allowlist_mapping_and_scope(self) -> None:
        rule = ValidationRule(
            cyrillic_exact_allowlist=("КВ-1",),
            cyrillic_mappings={"Полководец": "统帅"},
            cyrillic_scopes=(
                RuleScope(pattern=r"ИС-\d+", adapter_id="gettext", path_glob="vehicles/**"),
            ),
        )
        mapped = rule.validate_text(
            "Полководец КВ-1", adapter_id="gettext", relative_path="ui/main.mo"
        )
        self.assertFalse(mapped.failed)
        self.assertEqual("统帅 КВ-1", mapped.text)
        scoped = rule.validate_text(
            "坦克 ИС-7", adapter_id="gettext", relative_path="vehicles/ussr.mo"
        )
        self.assertFalse(scoped.failed)
        outside = rule.validate_text(
            "坦克 ИС-7", adapter_id="gettext", relative_path="ui/main.mo"
        )
        self.assertTrue(outside.failed)

    def test_repository_rules_file_loads(self) -> None:
        rule = load_validation_rule(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        self.assertIsInstance(rule, ValidationRule)


class ResponseParserTests(unittest.TestCase):
    def test_parses_numbered_response_and_discards_suffix(self) -> None:
        response = ProviderResponse("[1] 参见 [3] 号说明\n[2] 乙\n---END---\n附言")
        parsed = ResponseParser().parse(response, 2)
        self.assertEqual(("参见 [3] 号说明", "乙"), parsed.translations)

    def test_rejects_duplicate_number(self) -> None:
        response = ProviderResponse("[1] 甲\n[2] 乙\n[2] 丙\n---END---")
        with self.assertRaises(NumberingError):
            ResponseParser().parse(response, 2)

    def test_missing_sentinel_and_length_are_truncated(self) -> None:
        with self.assertRaises(TruncatedResponse):
            ResponseParser().parse(ProviderResponse("[1] 甲"), 1)
        with self.assertRaises(TruncatedResponse):
            ResponseParser().parse(
                ProviderResponse("[1] 甲\n---END---", finish_reason="length"), 1
            )


if __name__ == "__main__":
    unittest.main()
