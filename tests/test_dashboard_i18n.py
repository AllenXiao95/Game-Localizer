"""Regression coverage for the dashboard Chinese/English locale layer."""
from __future__ import annotations

import json
import unittest

from localizer.web import DashboardServer
from localizer.web.dashboard_server import _I18N_MARKER, inject_dashboard_i18n
from localizer.web.server import STATIC_ROOT


class DashboardI18nTests(unittest.TestCase):
    def test_public_dashboard_server_uses_i18n_wrapper(self) -> None:
        self.assertEqual("localizer.web.dashboard_server", DashboardServer.__module__)

    def test_real_dashboard_gets_locale_runtime_before_legacy_script(self) -> None:
        source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        rendered = inject_dashboard_i18n(source)

        self.assertIn(_I18N_MARKER, rendered)
        self.assertLess(rendered.index(_I18N_MARKER), rendered.index("const $ ="))
        self.assertIn("window.LocalizerI18n", rendered)
        self.assertIn("localizer.dashboard.locale", rendered)
        self.assertIn("MutationObserver", rendered)
        self.assertIn("localeToggle", rendered)
        # There must be only one browser-side state/observer engine. Long-form copy is merged into
        # its catalog before execution instead of installing a competing observer.
        self.assertEqual(1, rendered.count("const observer = new MutationObserver"))

    def test_long_form_catalog_is_merged_into_the_runtime(self) -> None:
        source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        rendered = inject_dashboard_i18n(source)
        catalog = json.loads(
            (STATIC_ROOT / "i18n-additions.json").read_text(encoding="utf-8")
        )

        self.assertGreaterEqual(len(catalog["phrases"]), 70)
        for chinese, english in [
            catalog["phrases"][0],
            ["阶段状态由磁盘产物反推，不是运行态权威状态。",
             "Stage status is inferred from persisted artifacts and is not the runtime source of truth."],
            ["本地重新校验不调用模型，也不产生费用。",
             "Local revalidation does not call the model and incurs no model cost."],
            ["目标版本默认继承父运行但可在本次重建中修改；源路径与 .env 仍从父运行快照继承。版本变化只影响制品、Manifest、tag 和上传目录，不会让已安全复用的词条重新调用 Provider。",
             "The target version defaults to the parent run but may be changed for this rebuild. Source path and .env still inherit from the parent snapshot. A version change affects only the artifact, Manifest, tag, and upload path; safely reused units do not call the provider again."],
        ]:
            self.assertIn(chinese, rendered)
            self.assertIn(english, rendered)

    def test_number_and_clock_formatting_follow_selected_locale(self) -> None:
        source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        rendered = inject_dashboard_i18n(source)

        self.assertNotIn('toLocaleString("zh-CN")', rendered)
        self.assertNotIn('toLocaleTimeString("zh-CN")', rendered)
        self.assertIn(
            'toLocaleString(window.LocalizerI18n?.locale() || "zh-CN")', rendered
        )
        self.assertIn(
            'toLocaleTimeString(window.LocalizerI18n?.locale() || "zh-CN")', rendered
        )

    def test_injection_is_idempotent(self) -> None:
        source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        once = inject_dashboard_i18n(source)
        twice = inject_dashboard_i18n(once)

        self.assertEqual(once, twice)
        self.assertEqual(1, twice.count(_I18N_MARKER))

    def test_locale_runtime_preserves_operator_state_by_translating_in_place(self) -> None:
        script = (STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")

        self.assertIn("WeakMap", script)
        self.assertIn("processTextNode", script)
        self.assertIn("processAttributes", script)
        self.assertIn("translateTree(document, false)", script)
        self.assertIn("#localeToggle,script,style,pre,code,textarea,.mono,[data-i18n-raw]", script)
        self.assertNotIn("location.reload", script)
        self.assertNotIn("window.location =", script)
        self.assertNotIn("innerHTML = document", script)

    def test_locale_runtime_has_browser_fallback_and_persistence(self) -> None:
        script = (STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")

        self.assertIn("localStorage.getItem(STORAGE_KEY)", script)
        self.assertIn("localStorage.setItem(STORAGE_KEY, currentLocale)", script)
        self.assertIn("navigator.languages", script)
        self.assertIn('"zh-CN"', script)
        self.assertIn('"en-US"', script)


if __name__ == "__main__":
    unittest.main()
