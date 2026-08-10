"""已修复的条目要标绿置底，再次点开要回显定稿译文与差异（需求 3）。

根因是 `unit()` 返回的是 sidecar 里那次**运行时**的译文 —— 落表之后它不会变，
所以再点开一条已修复的条目看到的是旧值，人会以为自己的修改没保存。

「置底」不是排版偏好：2000 个决策的场景下，定完一条它还留在原地，
会让人反复扫过同一批已经做完的东西。
"""
from __future__ import annotations

import re
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

from localizer.web.review import diff_ops
from test_web_review import _Project

INDEX = ROOT / "src" / "localizer" / "web" / "static" / "index.html"


class DiffOpsTests(unittest.TestCase):
    """差异在服务端算：CSP 只允许内联脚本（引不进 diff 库），
    而手写一份 JS diff 又是一份没人测的实现。"""

    def test_identical_text_is_one_equal_run(self) -> None:
        self.assertEqual([{"op": "equal", "text": "坦克"}], diff_ops("坦克", "坦克"))

    def test_pure_insertion(self) -> None:
        ops = diff_ops("", "补上的译文")
        self.assertEqual([{"op": "insert", "text": "补上的译文"}], ops)

    def test_replacement_shows_both_sides(self) -> None:
        ops = diff_ops("旧译文", "新译文")
        kinds = [op["op"] for op in ops]
        self.assertIn("delete", kinds)
        self.assertIn("insert", kinds)

    def test_insert_ops_reconstruct_the_new_text(self) -> None:
        before, after = "战斗获得的钱", "战斗获得的银币"
        rebuilt = "".join(
            op["text"] for op in diff_ops(before, after) if op["op"] != "delete"
        )
        self.assertEqual(after, rebuilt)

    def test_delete_ops_reconstruct_the_old_text(self) -> None:
        before, after = "战斗获得的钱", "战斗获得的银币"
        rebuilt = "".join(
            op["text"] for op in diff_ops(before, after) if op["op"] != "insert"
        )
        self.assertEqual(before, rebuilt)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        self.service = self.project.service()
        self.run_id = _Project.RUN_ID

    def tearDown(self) -> None:
        self._temp.cleanup()


class ReopenShowsTheCommittedTranslationTests(_Case):
    def test_before_commit_the_unit_shows_the_run_time_value(self) -> None:
        identity = self.project.identity("gloss")
        payload = self.service.unit(self.run_id, identity)
        self.assertFalse(payload["resolved"])
        self.assertIsNone(payload["committed_translation"])
        self.assertEqual("战斗获得的钱", payload["translation"])
        self.assertEqual([], payload["diff"])

    def test_after_commit_the_unit_echoes_what_was_written(self) -> None:
        identity = self.project.identity("gloss")
        self.service.commit(self.run_id, {identity: "战斗获得的银币"}, reason="改术语")
        payload = self.service.unit(self.run_id, identity)
        self.assertTrue(payload["resolved"])
        self.assertEqual("战斗获得的银币", payload["committed_translation"])
        # 原值仍然带着，差异才有参照。
        self.assertEqual("战斗获得的钱", payload["original_translation"])
        self.assertTrue(payload["diff"])
        rebuilt = "".join(
            op["text"] for op in payload["diff"] if op["op"] != "delete"
        )
        self.assertEqual("战斗获得的银币", rebuilt)

    def test_sidecar_itself_is_never_rewritten(self) -> None:
        # 旧运行不可变：落表只写 TM 与决策日志。
        identity = self.project.identity("gloss")
        index_path = self.service._index_path(self.run_id)
        before = index_path.read_bytes()
        self.service.commit(self.run_id, {identity: "战斗获得的银币"}, reason="x")
        self.assertEqual(before, index_path.read_bytes())


class ResolvedItemsSinkToTheBottomTests(_Case):
    def test_units_queue_puts_resolved_last(self) -> None:
        rows = self.service.units(self.run_id, code="empty_translation")["units"]
        self.assertGreaterEqual(len(rows), 2)
        first = rows[0]["stable_identity"]
        self.service.commit(self.run_id, {first: "补上的译文"}, reason="补译")

        after = self.service.units(self.run_id, code="empty_translation")["units"]
        self.assertEqual(after[-1]["stable_identity"], first, "已修复的应排到最后")
        self.assertTrue(after[-1]["resolved"])
        self.assertEqual("补上的译文", after[-1]["committed_translation"])
        self.assertFalse(after[0]["resolved"])

    def test_group_queue_puts_resolved_last(self) -> None:
        groups = self.service.groups(self.run_id)["groups"]
        target = groups[0]
        self.service.unify(self.run_id, target["group_id"], "统一译法", reason="定稿")

        after = self.service.groups(self.run_id)["groups"]
        self.assertEqual(after[-1]["group_id"], target["group_id"])
        self.assertTrue(after[-1]["resolved"])
        self.assertEqual("统一译法", after[-1]["committed_translation"])

    def test_partially_committed_group_is_not_marked_resolved(self) -> None:
        """只统一了一半是最坏的结果，绝不能标成已完成。

        剩下的成员下一次运行会被模型重译出第 N 种译法，警告复活而操作者
        以为已经处理完了。
        """
        groups = self.service.groups(self.run_id)["groups"]
        target = next(g for g in groups if g["member_count"] >= 2)
        half = target["members"][0]["stable_identity"]
        self.service.commit(self.run_id, {half: "只改了一条"}, reason="半途")

        after = self.service.groups(self.run_id)["groups"]
        row = next(g for g in after if g["group_id"] == target["group_id"])
        self.assertFalse(row["resolved"])
        self.assertEqual(1, row["committed_members"])
        self.assertLess(row["committed_members"], row["member_count"])
        # 没解决的必须仍排在已解决的前面。
        resolved_positions = [i for i, g in enumerate(after) if g["resolved"]]
        unresolved_positions = [i for i, g in enumerate(after) if not g["resolved"]]
        if resolved_positions and unresolved_positions:
            self.assertLess(max(unresolved_positions), min(resolved_positions))

    def test_reverting_moves_the_item_back_up(self) -> None:
        rows = self.service.units(self.run_id, code="empty_translation")["units"]
        identity = rows[0]["stable_identity"]
        self.service.commit(self.run_id, {identity: "补上的译文"}, reason="补译")
        decision = self.service.decisions(self.run_id)["decisions"][-1]
        self.service.revert(self.run_id, [decision["decision_id"]])

        after = self.service.units(self.run_id, code="empty_translation")["units"]
        row = next(u for u in after if u["stable_identity"] == identity)
        self.assertFalse(row["resolved"], "撤销之后不该还算已修复")
        self.assertIsNone(row["committed_translation"])


class FrontendRendersResolvedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = INDEX.read_text(encoding="utf-8")

    def test_done_style_derives_from_the_ok_variable(self) -> None:
        rule = re.search(r"\n\s*\.qitem\.done\s*\{([^}]*)\}", self.source)
        self.assertIsNotNone(rule, "已修复条目没有视觉区分")
        self.assertIn("var(--ok)", rule.group(1))
        self.assertIn("color-mix", rule.group(1))

    def test_queues_apply_the_done_class_from_the_server_flag(self) -> None:
        # 「已修复」由服务端判定（TM 里有没有 human 行），不是前端自己猜。
        self.assertIn('u.resolved ? " done" : ""', self.source)
        self.assertIn('g.resolved ? " done" : ""', self.source)

    def test_reopened_unit_echoes_the_committed_translation(self) -> None:
        self.assertIn("unit.resolved ? unit.committed_translation", self.source)
        self.assertIn("不是运行时的原值", self.source)

    def test_diff_is_rendered_from_server_ops(self) -> None:
        self.assertIn("function renderDiff", self.source)
        self.assertIn("<ins>", self.source)
        self.assertIn("<del>", self.source)
        # 前端不许自己实现 diff 算法。
        self.assertNotIn("SequenceMatcher", self.source)
        self.assertNotIn("levenshtein", self.source.lower())

    def test_partial_commit_is_flagged_in_the_list(self) -> None:
        self.assertIn("只落表", self.source)
        self.assertIn("会被重译出新的译法", self.source)


if __name__ == "__main__":
    unittest.main()
