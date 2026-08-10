"""决策日志是权威，TM 是它的投影（W4）。

TM 没有 author、没有 reason、没有前像、没有 audit —— 它只知道「这条译文现在
是什么」，不知道「谁在什么时候把什么改成了什么、为什么」。把只读面板变成可编辑
面板时，这是**唯一**必须补上的东西。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.review_ledger import ReviewLedger
from localizer.application.review_log import (
    EMPTY_REVISION,
    LogRevisionMismatch,
    ReviewDecisionEvent,
    ReviewDecisionLog,
)


def _event(action: str, *targets: str, **changes) -> ReviewDecisionEvent:
    data = dict(action=action, run_id="r1", targets=tuple(targets), reason="测试")
    data.update(changes)
    return ReviewDecisionEvent(**data)


class _LogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.log = ReviewDecisionLog(Path(self._temp.name) / "decisions.jsonl")

    def tearDown(self) -> None:
        self._temp.cleanup()


class RevisionTests(_LogCase):
    def test_empty_log_has_a_stable_revision(self) -> None:
        self.assertEqual(EMPTY_REVISION, self.log.revision())

    def test_revision_changes_on_append(self) -> None:
        first = self.log.revision()
        self.log.append([_event("commit", "sid-1")])
        self.assertNotEqual(first, self.log.revision())

    def test_stale_revision_is_refused_and_nothing_is_written(self) -> None:
        self.log.append([_event("commit", "sid-1")])
        stale = EMPTY_REVISION
        with self.assertRaises(LogRevisionMismatch):
            self.log.append([_event("commit", "sid-2")], expected_revision=stale)
        self.assertEqual(1, len(self.log.read_all()), "被拒的整批一条都不该写进去")

    def test_matching_revision_is_accepted(self) -> None:
        revision = self.log.append([_event("commit", "sid-1")])
        self.log.append([_event("commit", "sid-2")], expected_revision=revision)
        self.assertEqual(2, len(self.log.read_all()))


class AppendOnlyTests(_LogCase):
    def test_concurrent_appends_lose_nothing(self) -> None:
        """audit.jsonl 那种「读全文 + 拼接 + 整体重写」在并发下会丢事件。"""
        errors = []

        def worker(index: int) -> None:
            try:
                for step in range(20):
                    self.log.append([_event("commit", f"sid-{index}-{step}")])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        events = self.log.read_all()
        self.assertEqual(160, len(events))
        self.assertEqual(160, len({event.decision_id for event in events}))

    def test_append_does_not_rewrite_existing_lines(self) -> None:
        # 真正的追加：已有内容的字节偏移不变。
        self.log.append([_event("commit", "sid-1")])
        shard = self.log.shards()[0]
        head = shard.read_bytes()
        self.log.append([_event("commit", "sid-2")])
        self.assertTrue(shard.read_bytes().startswith(head))

    def test_log_shards_by_month(self) -> None:
        self.log.append(
            [
                _event("commit", "a", decided_at="2026-08-04T01:00:00+00:00"),
                _event("commit", "b", decided_at="2026-09-01T01:00:00+00:00"),
            ]
        )
        names = sorted(path.name for path in self.log.shards())
        self.assertEqual(["decisions-202608.jsonl", "decisions-202609.jsonl"], names)
        self.assertEqual(2, len(self.log.read_all()), "read_all 必须跨分片")


class EventShapeTests(_LogCase):
    def test_unknown_action_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _event("frobnicate", "sid-1")

    def test_before_snapshot_survives_the_round_trip(self) -> None:
        before = {"sid-1": {"stable_identity": "sid-1", "translation": "旧译文",
                            "origin": "legacy", "is_formal": 0}}
        self.log.append([_event("commit", "sid-1", before=before, translation="新译文")])
        event = self.log.read_all()[0]
        self.assertEqual(before, event.before)
        self.assertEqual("新译文", event.translation)

    def test_absent_row_is_recorded_as_none(self) -> None:
        # 「当时这个坐标没有行」与「有一行但内容为空」必须能区分，
        # 否则撤销会把一条本不存在的行造出来。
        self.log.append([_event("commit", "sid-1", before={"sid-1": None})])
        self.assertEqual({"sid-1": None}, self.log.read_all()[0].before)

    def test_schema_version_is_enforced_on_read(self) -> None:
        self.log.append([_event("commit", "sid-1")])
        shard = self.log.shards()[0]
        payload = json.loads(shard.read_text("utf-8").splitlines()[0])
        payload["schema_version"] = 99
        shard.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.log.read_all()

    def test_decision_ids_and_timestamps_are_generated(self) -> None:
        event = _event("commit", "sid-1")
        self.assertTrue(event.decision_id)
        # 微秒精度：批量操作里同秒几十次是常态，秒级会让排序不可区分。
        self.assertIn(".", event.decided_at)


class LedgerTests(_LogCase):
    def _ledger_path(self) -> Path:
        return Path(self._temp.name) / "ledger.json"

    def test_rebuild_from_log_is_exact(self) -> None:
        events = []
        for index in range(20):
            events.append(_event("draft", f"d-{index}", translation="草稿"))
        for index in range(10):
            events.append(_event("commit", f"c-{index}", translation="定稿"))
        events.append(_event("skip", "s-1"))
        events.append(_event("defer", "f-1"))
        events.append(_event("unify", "u-1", "u-2", translation="统一"))
        events.append(_event("revert", "c-0"))
        self.log.append(events)

        incremental = ReviewLedger(self._ledger_path(), {})
        for event in self.log.read_all():
            for target in event.targets:
                mapping = {
                    "draft": "draft", "commit": "committed", "unify": "committed",
                    "accept_debt": "committed", "revert": "reverted",
                    "retire": "pending", "skip": "skipped", "defer": "deferred",
                }
                incremental.mark(target, mapping[event.action])

        rebuilt = ReviewLedger.rebuild_from_log(self._ledger_path(), self.log)
        self.assertEqual(
            {k: v["state"] for k, v in incremental.items.items()},
            {k: v["state"] for k, v in rebuilt.items.items()},
        )
        self.assertEqual("reverted", rebuilt.state_of("c-0"))
        self.assertEqual("committed", rebuilt.state_of("u-1"))
        self.assertEqual("committed", rebuilt.state_of("u-2"))

    def test_replay_order_is_write_order_not_timestamp_order(self) -> None:
        """同一微秒写入的事件，重放顺序必须与写入顺序一致。

        按 (decided_at, decision_id) 排序会让并列事件被随机 uuid 打乱，
        而顺序直接决定最终状态：先 commit 后 revert 与反过来是两个结果。
        """
        stamp = "2026-08-04T01:00:00.000000+00:00"
        self.log.append(
            [
                _event("commit", "x", decided_at=stamp, translation="定稿"),
                _event("revert", "x", decided_at=stamp),
            ]
        )
        rebuilt = ReviewLedger.rebuild_from_log(self._ledger_path(), self.log)
        self.assertEqual("reverted", rebuilt.state_of("x"))
        self.assertEqual(
            ["commit", "revert"], [e.action for e in self.log.read_all()]
        )

    def test_committed_is_a_server_side_marker(self) -> None:
        """分片提交里第 3 块失败时，刷新页面必须能分清哪些已落表。

        不然操作者就得重做已经做过的判断 —— 2000 个决策的场景下这是致命的。
        """
        ledger = ReviewLedger(self._ledger_path(), {})
        for index in range(300):
            ledger.mark(f"sid-{index}", "committed")
        for index in range(300, 500):
            ledger.mark(f"sid-{index}", "draft")
        ledger.save()

        reloaded = ReviewLedger.load(self._ledger_path())
        counters = reloaded.counters()
        self.assertEqual(300, counters["committed"])
        self.assertEqual(200, counters["draft"])
        self.assertEqual(200, len(reloaded.drafts()))

    def test_round_trip_and_schema_guard(self) -> None:
        ledger = ReviewLedger(self._ledger_path(), {})
        ledger.mark("sid-1", "draft", translation="草稿")
        ledger.cursor = "sid-1"
        ledger.save()
        reloaded = ReviewLedger.load(self._ledger_path())
        self.assertEqual("sid-1", reloaded.cursor)
        self.assertEqual("草稿", reloaded.items["sid-1"]["translation"])

        payload = json.loads(self._ledger_path().read_text("utf-8"))
        payload["schema_version"] = 99
        self._ledger_path().write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            ReviewLedger.load(self._ledger_path())

    def test_unknown_state_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ReviewLedger(self._ledger_path(), {}).mark("sid-1", "maybe")


if __name__ == "__main__":
    unittest.main()
