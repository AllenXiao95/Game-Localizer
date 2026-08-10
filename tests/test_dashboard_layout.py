"""面板的长列表必须固定高度滚动（任务 2）。

面板是纯静态单文件（server.py 的 `_static` 直出 index.html），没有构建步骤，
所以这里用静态断言把布局约束钉住 —— 不是为了测 CSS 好不好看，而是为了防止
以后新加一张表时又忘了封顶。

真机数据量与实测对照（Chromium，desktop 视口）：

    Tab            修复前页面高   修复后    倍数
    实时翻译        14394px      2102px    6.8x
    QA 问题         12139px      1555px    7.8x
    批次            33807px      1776px    19.0x

不封顶时「批次」这一屏是 33807 像素高：滚动条完全失去参照，且表头一滚就没了，
底下几百行不知道哪列是哪列。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "localizer" / "web" / "static" / "index.html"

# 允许的高度修饰。`free` 是显式豁免：天然只有几行的表封顶反而多一条没用的滚动边界。
HEIGHT_CLASSES = {"tall", "short", "free"}


class ScrollRegionsAreBoundedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = INDEX.read_text(encoding="utf-8")

    def test_every_scroll_region_declares_a_height(self) -> None:
        """裸 `class="scroll"` 一律不允许 —— 必须显式选一个高度。

        这条断言的价值在于**逼人做选择**：新加一张表时必须想一下它会有多少行，
        而不是默认无限增长。想清楚了就写 free，也是合法答案。
        """
        naked = re.findall(r'class="scroll"(?![\w-])', self.source)
        self.assertEqual(
            [], naked,
            "有 .scroll 没有指定高度修饰（tall/short/free）：长列表会无限增长",
        )

    def test_all_height_modifiers_are_defined_in_css(self) -> None:
        used = set()
        for match in re.finditer(r'class="scroll([^"]*)"', self.source):
            used.update(token for token in match.group(1).split() if token)
        self.assertTrue(used, "index.html 里一个 .scroll 都没有？")
        self.assertLessEqual(used, HEIGHT_CLASSES, f"未知的高度修饰：{used - HEIGHT_CLASSES}")
        for name in used:
            with self.subTest(modifier=name):
                self.assertRegex(
                    self.source,
                    rf"\.scroll\.{name}\s*\{{[^}}]*max-height",
                    f".scroll.{name} 在 CSS 里没有定义 max-height",
                )

    def test_base_scroll_caps_height_and_scrolls_both_axes(self) -> None:
        rule = re.search(r"\n\s*\.scroll\s*\{([^}]*)\}", self.source)
        self.assertIsNotNone(rule, "找不到 .scroll 基础规则")
        body = rule.group(1)
        self.assertIn("max-height", body, ".scroll 没有封顶，纵向仍然是敞开的")
        # 原来是 overflow-x，宽表横向滚动的能力不能丢。
        self.assertRegex(body, r"overflow:\s*auto")

    def test_sticky_table_head(self) -> None:
        """固定高度下表头必须钉住，否则滚两屏就不知道哪列是哪列。"""
        self.assertRegex(
            self.source,
            r"\.scroll\s+thead\s+th\s*\{[^}]*position:\s*sticky",
        )
        head = re.search(r"\.scroll\s+thead\s+th\s*\{([^}]*)\}", self.source).group(1)
        # 必须有不透明背景，否则滚上来的行会透过表头。
        self.assertIn("background", head)

    def test_other_unbounded_containers_are_capped_too(self) -> None:
        for selector, pattern in (
            (".runlist", r"\.runlist\s*\{[^}]*max-height"),
            ("pre", r"\n\s*pre\s*\{[^}]*max-height"),
        ):
            with self.subTest(selector=selector):
                self.assertRegex(self.source, pattern)



class ReviewTabGuardsTests(unittest.TestCase):
    """审查视图的前端约束（W11）。

    面板是纯静态单文件（server.py 的 `_static` 直出），没有构建步骤，
    所以这些约束只能用静态断言守。
    """

    def setUp(self) -> None:
        self.source = INDEX.read_text(encoding="utf-8")

    def test_autorefresh_never_rebuilds_the_review_tab(self) -> None:
        """5 秒轮询整块重绘会把人正在做的编辑擦掉。

        2000 个决策是跨天的作业，不可能一坐到底。
        """
        self.assertIn("TAB_REBUILD", self.source)
        self.assertRegex(self.source, r"TAB_REBUILD\s*=\s*\{\s*review:\s*false")
        self.assertIn("TAB_REBUILD[activeTab] !== false", self.source)

    def test_unsaved_drafts_warn_before_unload(self) -> None:
        self.assertIn("beforeunload", self.source)

    def test_textarea_is_styled(self) -> None:
        # 原来的 `input, select` 选择器不含 textarea —— 深色模式下会是白底黑字。
        self.assertRegex(self.source, r"input,\s*select,\s*textarea\s*\{")
        self.assertRegex(self.source, r"\n\s*textarea\s*\{[^}]*min-height")

    def test_new_colours_derive_from_existing_variables(self) -> None:
        """新增的高亮/diff 颜色必须从既有变量派生，深浅两套自动成立。"""
        for selector in ("ins", "del", "mark.hl-term", "mark.hl-ph"):
            with self.subTest(selector=selector):
                rule = re.search(
                    rf"\n\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", self.source
                )
                self.assertIsNotNone(rule, f"{selector} 没有样式")
                self.assertIn("color-mix", rule.group(1))
                self.assertIn("var(--", rule.group(1))

    def test_batch_leverage_numbers_are_not_hardcoded(self) -> None:
        """批量能覆盖多少组必须运行时算，不许写死。

        实测跨组批量只覆盖 12% 左右。把它说大了，操作者点完两个按钮发现
        还剩一千多组，会直接放弃这个界面 —— 那是另一种假绿灯。
        """
        block = self.source[self.source.index("function loadReviewQueue") :]
        block = block[: block.index("async function selectReviewRow")]
        self.assertIn("groups_with_plurality", block)
        self.assertIn("groups_needing_case_by_case", block)
        # 队列说明里不许出现形如「几百组」「1900 组」这类字面量。
        self.assertNotRegex(block, r">\s*\d{3,}\s*组")

    def test_incomplete_write_is_never_reported_as_success(self) -> None:
        block = self.source[self.source.index("async function runReviewAction") :]
        block = block[: block.index("document.addEventListener")]
        self.assertIn("result.complete === false", block)
        self.assertIn("未完成", block)

    def test_review_states_that_release_is_not_decided_here(self) -> None:
        self.assertIn("这不是发布结论", self.source)

    def test_review_queues_reuse_the_bounded_scroll_system(self) -> None:
        # 队列自己是 .queue（有 max-height），组内译法表用 .scroll short。
        rule = re.search(r"\n\s*\.queue\s*\{([^}]*)\}", self.source)
        self.assertIsNotNone(rule)
        self.assertIn("max-height", rule.group(1))



class BoundaryTextIsConsistentTests(unittest.TestCase):
    """边界改了，四处代码内文案与两份文档必须同时改（W12）。

    这个项目反复出现的病就是「文档说 X、代码做 Y」。既然这次真的把
    §16 的边界收窄了，就不能留下任何一处还写着旧口径。
    """

    STALE = (
        "不提供任何翻译编辑",
        "人工审核仍在 ParaTranz 完成",
        "M0～M6 不建设本地审核平台",
    )

    FILES = (
        "src/localizer/web/__init__.py",
        "src/localizer/web/server.py",
        "src/localizer/cli/main.py",
        "src/localizer/web/static/index.html",
        "README.md",
        "README.en.md",
        "README.ja.md",
    )

    def test_no_file_still_claims_the_old_boundary(self) -> None:
        root = INDEX.parents[4]
        for name in self.FILES:
            text = (root / name).read_text(encoding="utf-8")
            for phrase in self.STALE:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_readmes_describe_the_single_user_boundary(self) -> None:
        root = INDEX.parents[4]
        expected = {
            "README.md": "单人定点修订",
            "README.en.md": "targeted single-user revision",
            "README.ja.md": "単一ユーザーによる個別修正",
        }
        for name, phrase in expected.items():
            with self.subTest(file=name):
                self.assertIn(phrase, (root / name).read_text(encoding="utf-8"))

    def test_review_log_contract_is_documented_in_code(self) -> None:
        root = INDEX.parents[4]
        text = (root / "src" / "localizer" / "application" / "review_log.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ReviewDecisionLog", text)
        self.assertIn("append-only", text)

if __name__ == "__main__":
    unittest.main()
