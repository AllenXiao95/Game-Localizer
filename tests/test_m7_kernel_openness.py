"""M7 内核开放性：新增一种资源格式不应该需要改内核。

M7 的验收标准是「核心层没有 WOT、俄语或 Gettext 硬编码；新 Adapter 不需要修改
翻译内核」。这个文件把那句话变成可执行断言 —— 它守的不是某个功能，而是
「以后加第三种格式时，改动范围是否仍然只有 adapters/resources/ 一个目录」。
"""
from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.resources.registry import (
    ADAPTER_FACTORIES,
    available_adapters,
    build_adapter,
    register_adapter,
)
from localizer.rules.placeholder import (
    PlaceholderRule,
    register_placeholder_syntax,
    registered_syntax,
)


class AdapterRegistryTests(unittest.TestCase):
    def test_builtin_adapters_are_discovered_without_explicit_import(self) -> None:
        self.assertIn("gettext", available_adapters())
        self.assertIn("paratranz_json", available_adapters())

    def test_unknown_adapter_names_the_available_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError) as ctx:
                build_adapter(
                    "no_such_format",
                    project_id="p",
                    source_root=Path(temp),
                    source_locale="en",
                    target_locale="zh-Hans",
                )
        # 报错要能自解释：告诉使用者有哪些可选，而不是只说「不认识」。
        self.assertIn("gettext", str(ctx.exception))

    def test_registering_a_duplicate_id_is_rejected(self) -> None:
        class Clash:
            adapter_id = "gettext"

        with self.assertRaises(ValueError):
            register_adapter(Clash)

    def test_adapters_own_and_validate_their_options_schema(self) -> None:
        # 内核不解释格式字段，但 Adapter 必须声明 Schema，不能再接收任意字典。
        with tempfile.TemporaryDirectory() as temp:
            gettext = build_adapter(
                "gettext",
                project_id="p",
                source_root=Path(temp),
                source_locale="en",
                target_locale="zh-Hans",
                options={"layout": "keyed_source"},
            )
            self.assertEqual("keyed_source", gettext.options["layout"])
            paratranz = build_adapter(
                "paratranz_json",
                project_id="p",
                source_root=Path(temp),
                source_locale="en",
                target_locale="zh-Hans",
                options={"source_field": "source"},
            )
            self.assertEqual("source", paratranz.options["source_field"])
            with self.assertRaises(Exception):
                build_adapter(
                    "gettext",
                    project_id="p",
                    source_root=Path(temp),
                    source_locale="en",
                    target_locale="zh-Hans",
                    options={"anything": 1},
                )


class KernelHasNoFormatHardcodingTests(unittest.TestCase):
    """内核层不得 import 具体 Adapter，也不得按名字分支。"""

    KERNEL_DIRS = ("application", "domain", "config", "infrastructure", "ports")

    def _kernel_sources(self):
        for name in self.KERNEL_DIRS:
            for path in (SRC / "localizer" / name).rglob("*.py"):
                yield path

    def test_kernel_never_imports_a_concrete_adapter(self) -> None:
        offenders = []
        for path in self._kernel_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = getattr(node, "module", "") or ""
                if isinstance(node, ast.ImportFrom) and module.startswith(
                    "localizer.adapters.resources."
                ):
                    # 注册表本身是允许的；具体 Adapter 模块不允许。
                    if not module.endswith(".registry"):
                        offenders.append(f"{path.name}: {module}")
        self.assertEqual(
            [], offenders, msg="内核 import 了具体 Adapter，新增格式就得改内核"
        )

    def test_project_runner_no_longer_branches_on_adapter_type(self) -> None:
        source = (SRC / "localizer/application/project_runner.py").read_text("utf-8")
        self.assertNotIn('!= "gettext"', source)
        self.assertIn("build_adapter", source)


class PlaceholderSyntaxInjectionTests(unittest.TestCase):
    """占位符语法必须可按格式注入 —— 这是 M7 的真正瓶颈。

    generic 那套只覆盖 printf 与 {name}。对 Paradox 的 $VAR$ / £icon£ / §Y /
    [Root.GetName] 是**零匹配加静默通过**：mask 不动它，round_trip 恒为 True，
    多重集比对里根本不出现这一项。译文把 $NAME$ 改成 $名字$ 也能全绿。
    """

    PARADOX = (r"\$[^$\s]*\$", r"£[^£\s]*£", r"§.", r"\[[A-Za-z][^\]\s]*\]")
    SAMPLE = "§Y$COUNTRY_ADJ$§! needs £political_power£ $VALUE|*0$ [Root.GetName]"

    def test_generic_preset_silently_misses_foreign_syntax(self) -> None:
        generic = PlaceholderRule()
        masked = generic.mask(self.SAMPLE)
        self.assertEqual(0, len(masked.token_to_value))
        # 这就是危险之处：什么都没识别，round_trip 却是 True。
        self.assertTrue(generic.round_trip(self.SAMPLE))
        # 探测式必须把它们列出来，否则这件事永远不可见。
        self.assertGreaterEqual(
            len(generic.find_unmasked_candidates(masked.masked_text)), 5
        )

    def test_injected_preset_masks_and_round_trips(self) -> None:
        rule = PlaceholderRule(self.PARADOX)
        masked = rule.mask(self.SAMPLE)
        self.assertEqual(6, len(masked.token_to_value))
        self.assertEqual((), rule.find_unmasked_candidates(masked.masked_text))
        self.assertTrue(rule.round_trip(self.SAMPLE))

    def test_registry_lookup_by_adapter_id(self) -> None:
        register_placeholder_syntax("unittest_fake_format", self.PARADOX)
        self.addCleanup(
            lambda: registered_syntax("unittest_fake_format") and None
        )
        rule = PlaceholderRule.for_adapter("unittest_fake_format")
        self.assertEqual(6, len(rule.mask(self.SAMPLE).token_to_value))
        # 未登记的格式退回 generic，不报错。
        self.assertEqual(
            0, len(PlaceholderRule.for_adapter("nothing_here").mask(self.SAMPLE).token_to_value)
        )

    def test_bad_pattern_fails_at_registration_not_at_runtime(self) -> None:
        import re

        with self.assertRaises(re.error):
            register_placeholder_syntax("unittest_bad_format", (r"[unclosed",))


class PlaceholderOnlyUnitsAreNotUntranslatedTests(unittest.TestCase):
    """P0-G · 纯占位符条目本来就不该被翻译，判 untranslated 会大批误报。"""

    def test_translatable_heuristic(self) -> None:
        rule = PlaceholderRule(PlaceholderSyntaxInjectionTests.PARADOX)
        for source, expected in (
            ("$VALUE$", False),
            ("$A$ $B$", False),
            ("— / |", False),
            ("Rearmament Program", True),
            ("Cost: £pp£ $V$ now", True),
        ):
            with self.subTest(source=source):
                self.assertEqual(expected, rule.is_translatable(source))

    def test_both_stages_share_one_judgement(self) -> None:
        """翻译阶段与构建阶段必须用同一套判据。

        判据曾经只是 batch_orchestrator 的私有函数，local_build 没有对应守卫，
        于是同一批纯占位符条目在翻译阶段被豁免、在构建阶段被重判成
        `untranslated/machine`，落进零容忍的 new_error 且无任何出口。
        """
        import inspect

        from localizer.application import batch_orchestrator, local_build

        # 判据在 W1 之后搬到了 inspect_unit（面板要在毫秒级复判一条编辑，
        # 不能有第二份实现）。这里同时断言两件事，缺一不可：
        # ① 守卫仍在判据里；② build() 确实经由 inspect_unit 走这条判据。
        self.assertIn(
            "is_translatable",
            inspect.getsource(local_build.LocalBuildPipeline.inspect_unit),
        )
        self.assertIn(
            "inspect_unit", inspect.getsource(local_build.LocalBuildPipeline.build)
        )
        # 编排器不得再持有自己那份私有实现。
        self.assertFalse(hasattr(batch_orchestrator, "_is_translatable"))


if __name__ == "__main__":
    unittest.main()
