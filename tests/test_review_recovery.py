from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.web.collector import DashboardCollector
from localizer.web.review import ReviewConflict
from localizer.web.review_recovery import recovery_operations, safe_revert
from localizer.web.server import DashboardServer
from test_web_review import _Project


class _RecoveryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        self.service = self.project.service()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _prepare_bad_unify(self):
        run_id = self.project.RUN_ID
        group = next(
            item
            for item in self.service.groups(run_id)["groups"]
            if item["source"] == "Общий текст"
        )
        identities = {
            key: self.project.identity(key)
            for key in ("g1", "g2", "g3")
        }
        # Two contexts intentionally use different translations before a bad
        # same-source consolidation.  These are human rows so the unify event's
        # before-images are exactly the data we need to recover.
        self.service.commit(
            run_id,
            {
                identities["g1"]: "这就对了！",
                identities["g2"]: "收到！",
                identities["g3"]: "收到！",
            },
            reason="按上下文人工定稿",
        )
        outcome = self.service.unify(
            run_id,
            group["group_id"],
            "这就对了！",
            reason="错误的同源批量统一",
        )
        payload = recovery_operations(self.service, run_id, action="unify")
        operation = next(
            item for item in payload["operations"] if item["audit_id"] == outcome.audit_id
        )
        by_identity = {
            item["stable_identity"]: item for item in operation["coordinates"]
        }
        return identities, outcome, payload, operation, by_identity


class RecoveryQueryTests(_RecoveryCase):
    def test_bulk_unify_is_exposed_with_before_after_and_current_values(self) -> None:
        identities, _outcome, _payload, operation, rows = self._prepare_bad_unify()

        self.assertEqual("unify", operation["action"])
        self.assertEqual(3, operation["coordinate_count"])
        self.assertEqual(3, operation["revertible_count"])
        self.assertEqual(0, operation["conflict_count"])

        radio_like = rows[identities["g2"]]
        self.assertEqual("b.mo", radio_like["relative_path"])
        self.assertEqual("g2", radio_like["logical_key"])
        self.assertEqual("收到！", radio_like["before_translation"])
        self.assertEqual("这就对了！", radio_like["after_translation"])
        self.assertEqual("这就对了！", radio_like["current_translation"])
        self.assertTrue(radio_like["revertible"])

    def test_later_human_edit_marks_old_unify_decision_as_conflicted(self) -> None:
        identities, _outcome, _payload, _operation, rows = self._prepare_bad_unify()
        old_decision = rows[identities["g2"]]["decision_id"]

        self.service.commit(
            self.project.RUN_ID,
            {identities["g2"]: "无线电确认：收到！"},
            reason="后续人工修正",
        )
        refreshed = recovery_operations(self.service, self.project.RUN_ID, action="unify")
        stale = next(
            row
            for operation in refreshed["operations"]
            for row in operation["coordinates"]
            if row["decision_id"] == old_decision
        )
        self.assertFalse(stale["revertible"])
        self.assertIn("后已有新的", stale["conflict_reason"])
        self.assertEqual("无线电确认：收到！", stale["current_translation"])


class SelectiveRevertTests(_RecoveryCase):
    def _tm_translations(self, identities):
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            rows = tm.rows_for(list(identities))
        return {identity: row["translation"] for identity, row in rows.items()}

    def test_selective_batch_revert_restores_only_wrong_coordinates(self) -> None:
        identities, _outcome, payload, _operation, rows = self._prepare_bad_unify()
        selected = [
            rows[identities["g2"]]["decision_id"],
            rows[identities["g3"]]["decision_id"],
        ]
        result = safe_revert(
            self.service,
            self.project.RUN_ID,
            selected,
            reason="恢复上下文相关无线电译法",
            expected_log_revision=payload["log_revision"],
        )
        self.assertTrue(result["complete"])
        self.assertEqual(2, result["restored"])

        current = self._tm_translations(identities.values())
        self.assertEqual("这就对了！", current[identities["g1"]])
        self.assertEqual("收到！", current[identities["g2"]])
        self.assertEqual("收到！", current[identities["g3"]])

        events = self.service.decisions(self.project.RUN_ID)["decisions"]
        self.assertEqual("revert", events[-1]["action"])
        self.assertEqual(
            "selective_coordinate", events[-1]["details"]["recovery_mode"]
        )
        self.assertEqual(set(selected), set(events[-1]["details"]["reverted_decision_ids"]))

    def test_one_stale_member_rejects_the_entire_batch_before_any_restore(self) -> None:
        identities, _outcome, _payload, _operation, rows = self._prepare_bad_unify()
        safe_decision = rows[identities["g1"]]["decision_id"]
        stale_decision = rows[identities["g2"]]["decision_id"]

        self.service.commit(
            self.project.RUN_ID,
            {identities["g2"]: "后来又确认成收到！"},
            reason="后续人工修正",
        )
        revision = self.service.session(self.project.RUN_ID)["log_revision"]
        with self.assertRaises(ReviewConflict) as ctx:
            safe_revert(
                self.service,
                self.project.RUN_ID,
                [safe_decision, stale_decision],
                reason="尝试批量恢复",
                expected_log_revision=revision,
            )
        self.assertIn("整批未修改", str(ctx.exception))

        current = self._tm_translations(identities.values())
        # g1 was safe, but must not be restored before the stale g2 is rejected.
        self.assertEqual("这就对了！", current[identities["g1"]])
        self.assertEqual("后来又确认成收到！", current[identities["g2"]])
        self.assertEqual("这就对了！", current[identities["g3"]])


class RecoveryHttpAndGuiTests(_RecoveryCase):
    def setUp(self) -> None:
        super().setUp()
        self.identities, self.outcome, _payload, _operation, _rows = self._prepare_bad_unify()
        collector = DashboardCollector(
            self.project.config, self.project.config_path, ROOT
        )
        self.server = DashboardServer(collector, port=0).start_background()

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def _url(self, path: str) -> str:
        return self.server.url.rstrip("/") + path

    def _get_json(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict):
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Localizer-Action": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_dashboard_root_contains_the_recovery_function_area(self) -> None:
        with urllib.request.urlopen(self._url("/"), timeout=10) as response:
            html = response.read().decode("utf-8")
            csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("变更历史 / Recovery", html)
        self.assertIn('data-rview="recovery"', html)
        self.assertIn("撤销所选坐标", html)
        self.assertIn("script-src 'unsafe-inline'", csp)

    def test_http_recovery_can_revert_one_selected_coordinate(self) -> None:
        run_id = self.project.RUN_ID
        status, payload = self._get_json(
            f"/api/runs/{run_id}/review/recovery?action=unify"
        )
        self.assertEqual(200, status)
        operation = next(
            item for item in payload["operations"] if item["audit_id"] == self.outcome.audit_id
        )
        target = next(
            item
            for item in operation["coordinates"]
            if item["stable_identity"] == self.identities["g2"]
        )
        status, result = self._post_json(
            "/api/review/revert",
            {
                "run_id": run_id,
                "decision_ids": [target["decision_id"]],
                "reason": "GUI 选择性恢复",
                "expected_log_revision": payload["log_revision"],
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(result["complete"])
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            row = tm.rows_for([self.identities["g2"]])[self.identities["g2"]]
        self.assertEqual("收到！", row["translation"])

    def test_http_stale_revert_maps_to_409(self) -> None:
        run_id = self.project.RUN_ID
        payload = self._get_json(
            f"/api/runs/{run_id}/review/recovery?action=unify"
        )[1]
        operation = next(
            item for item in payload["operations"] if item["audit_id"] == self.outcome.audit_id
        )
        target = next(
            item
            for item in operation["coordinates"]
            if item["stable_identity"] == self.identities["g2"]
        )
        # Mutate through the same server-side Review semantics after the old recovery
        # view was read.  The stale decision must not be able to overwrite this.
        self.server.review.commit(
            run_id,
            {self.identities["g2"]: "更新后的人工译法"},
            reason="后续人工编辑",
        )
        revision = self.server.review.session(run_id)["log_revision"]
        status, error = self._post_json(
            "/api/review/revert",
            {
                "run_id": run_id,
                "decision_ids": [target["decision_id"]],
                "reason": "旧决策撤销",
                "expected_log_revision": revision,
            },
        )
        self.assertEqual(409, status)
        self.assertEqual("ReviewConflict", error["error"])
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            row = tm.rows_for([self.identities["g2"]])[self.identities["g2"]]
        self.assertEqual("更新后的人工译法", row["translation"])


if __name__ == "__main__":
    unittest.main()
