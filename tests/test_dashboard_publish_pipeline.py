"""Dashboard 发布流水线状态的回归测试。

发布不是一次按钮点击，而是一个 run 级、可恢复的终态。TaskService 会把发布结果写到
``workspace/runs/<run_id>/publish-result.json``；Collector 必须消费这份回执，否则
Dashboard 刷新或重启后流水线会永远停在 Build。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localizer.web.collector import DashboardCollector, RunRef


class PublishPipelineStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "runs" / "release-1"
        self.workspace.mkdir(parents=True)
        self.ref = RunRef("release-1", self.workspace, None, Path(self.temp.name) / "release")
        self.collector = DashboardCollector.__new__(DashboardCollector)
        self.progress = {"available": False}
        self.qa = {"available": True, "passed": True}
        self.artifact = {"available": True}

    def _write_publish(self, payload: dict) -> dict:
        (self.workspace / "publish-result.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return DashboardCollector._publish_summary(self.ref)

    def _stage(self, publish: dict) -> dict:
        return self.collector._current_stage(
            self.ref, self.progress, self.qa, self.artifact, publish
        )

    def test_without_publish_receipt_release_stays_at_build(self) -> None:
        publish = DashboardCollector._publish_summary(self.ref)
        self.assertFalse(publish["available"])
        self.assertEqual(
            {"key": "build", "state": "done", "note": "已产出正式制品与 Manifest"},
            self._stage(publish),
        )

    def test_successful_publish_advances_to_publish_done(self) -> None:
        publish = self._write_publish(
            {
                "task_id": "pub-1",
                "kind": "publish",
                "run_id": "release-1",
                "status": "completed",
                "result": {
                    "passed": True,
                    "manifest": "release.manifest.json",
                    "targets": [
                        {"target": "github_release", "status": "ok"},
                        {"target": "r2", "status": "ok", "objects": [{"skipped": True}]},
                    ],
                },
                "error": None,
            }
        )
        self.assertTrue(publish["passed"])
        self.assertEqual("publish", self._stage(publish)["key"])
        self.assertEqual("done", self._stage(publish)["state"])
        self.assertIn("2 个 target", self._stage(publish)["note"])

    def test_idempotent_skip_is_still_a_successful_publish(self) -> None:
        publish = self._write_publish(
            {
                "task_id": "pub-skip",
                "kind": "publish",
                "run_id": "release-1",
                "status": "completed",
                "result": {
                    "passed": True,
                    "targets": [
                        {
                            "target": "r2",
                            "status": "ok",
                            "objects": [{"skipped": True}, {"skipped": True}],
                        }
                    ],
                },
            }
        )
        self.assertEqual("done", self._stage(publish)["state"])

    def test_failed_publish_is_visible_as_blocked(self) -> None:
        publish = self._write_publish(
            {
                "task_id": "pub-failed",
                "kind": "publish",
                "run_id": "release-1",
                "status": "failed",
                "result": None,
                "error": {"type": "RuntimeError", "message": "credential missing"},
            }
        )
        stage = self._stage(publish)
        self.assertEqual("publish", stage["key"])
        self.assertEqual("blocked", stage["state"])
        self.assertIn("credential missing", stage["note"])

    def test_partial_target_failure_is_visible_as_blocked(self) -> None:
        publish = self._write_publish(
            {
                "task_id": "pub-partial",
                "kind": "publish",
                "run_id": "release-1",
                "status": "completed",
                "result": {
                    "passed": False,
                    "targets": [
                        {"target": "local", "status": "ok"},
                        {"target": "r2", "status": "failed"},
                    ],
                },
            }
        )
        stage = self._stage(publish)
        self.assertEqual("blocked", stage["state"])
        self.assertIn("1 个 target", stage["note"])

    def test_persisted_running_state_is_rendered_as_publish_running(self) -> None:
        publish = self._write_publish(
            {
                "task_id": "pub-running",
                "kind": "publish",
                "run_id": "release-1",
                "status": "running",
                "result": None,
                "error": None,
            }
        )
        self.assertEqual(
            {"key": "publish", "state": "running", "note": "发布任务正在执行"},
            self._stage(publish),
        )

    def test_other_run_receipt_cannot_affect_this_run(self) -> None:
        other = self.workspace.parent / "release-2"
        other.mkdir()
        (other / "publish-result.json").write_text(
            json.dumps(
                {
                    "task_id": "other",
                    "kind": "publish",
                    "run_id": "release-2",
                    "status": "completed",
                    "result": {"passed": True, "targets": []},
                }
            ),
            encoding="utf-8",
        )
        publish = DashboardCollector._publish_summary(self.ref)
        self.assertFalse(publish["available"])
        self.assertEqual("build", self._stage(publish)["key"])


if __name__ == "__main__":
    unittest.main()
