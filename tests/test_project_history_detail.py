from __future__ import annotations

import json
import sys
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
from localizer.web.project_history_detail import (
    project_history_coordinates,
    safe_revert_with_history_fallback,
)
from localizer.web.review import ReviewConflict
from localizer.web.server import DashboardServer
from test_review_recovery import _RecoveryCase


class ProjectHistoryCoordinateTests(_RecoveryCase):
    def test_coordinate_query_filters_inside_one_audit(self) -> None:
        identities, outcome, _payload, _operation, _rows = self._prepare_bad_unify()
        result = project_history_coordinates(
            self.service,
            run_id=self.project.RUN_ID,
            action="unify",
            audit_id=outcome.audit_id,
            query="b.mo",
            status="current",
            recovery="revertible",
            limit=1,
        )
        self.assertEqual(1, result["total"])
        self.assertEqual(1, len(result["coordinates"]))
        row = result["coordinates"][0]
        self.assertEqual(identities["g2"], row["stable_identity"])
        self.assertEqual("收到！", row["before_translation"])
        self.assertEqual("这就对了！", row["after_translation"])
        self.assertTrue(row["revertible"])
        self.assertEqual("review_index", row["recovery_proof"])

    def test_old_run_without_review_index_can_use_matching_before_image(self) -> None:
        identities, outcome, _payload, _operation, rows = self._prepare_bad_unify()
        index_path = self.service._index_path(self.project.RUN_ID)
        self.assertIsNotNone(index_path)
        index_path.unlink()

        result = project_history_coordinates(
            self.service,
            run_id=self.project.RUN_ID,
            action="unify",
            audit_id=outcome.audit_id,
            query="b.mo",
            recovery="revertible",
        )
        self.assertFalse(result["run_index_available"])
        self.assertEqual(1, result["total"])
        row = result["coordinates"][0]
        self.assertEqual("before_image", row["recovery_proof"])
        self.assertTrue(row["revertible"])

        revision = self.service._log().revision()
        reverted = safe_revert_with_history_fallback(
            self.service,
            self.project.RUN_ID,
            [rows[identities["g2"]]["decision_id"]],
            reason="历史 run 按 before-image 恢复",
            expected_log_revision=revision,
        )
        self.assertEqual("before_image", reverted["recovery_proof"])
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            current = tm.rows_for([identities["g2"]])[identities["g2"]]
        self.assertEqual("收到！", current["translation"])

    def test_old_run_with_source_drift_stays_fail_closed(self) -> None:
        identities, outcome, _payload, _operation, rows = self._prepare_bad_unify()
        index_path = self.service._index_path(self.project.RUN_ID)
        self.assertIsNotNone(index_path)
        index_path.unlink()
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            tm.connection.execute(
                "UPDATE tm_entries SET source_fingerprint = ? WHERE stable_identity = ?",
                ("drifted-fingerprint", identities["g2"]),
            )
            tm.connection.commit()

        blocked = project_history_coordinates(
            self.service,
            run_id=self.project.RUN_ID,
            action="unify",
            audit_id=outcome.audit_id,
            query="b.mo",
            recovery="blocked",
        )
        self.assertEqual(1, blocked["total"])
        self.assertEqual("missing_evidence", blocked["coordinates"][0]["recovery_proof"])
        self.assertFalse(blocked["coordinates"][0]["revertible"])

        with self.assertRaises(ReviewConflict):
            safe_revert_with_history_fallback(
                self.service,
                self.project.RUN_ID,
                [rows[identities["g2"]]["decision_id"]],
                reason="不能绕过漂移证据",
                expected_log_revision=self.service._log().revision(),
            )


class ProjectHistoryCoordinateHttpTests(_RecoveryCase):
    def setUp(self) -> None:
        super().setUp()
        self.identities, self.outcome, _payload, _operation, _rows = self._prepare_bad_unify()
        collector = DashboardCollector(self.project.config, self.project.config_path, ROOT)
        self.server = DashboardServer(collector, port=0).start_background()

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def _url(self, path: str) -> str:
        return self.server.url.rstrip("/") + path

    def test_http_coordinate_endpoint_is_paged_and_searchable(self) -> None:
        path = (
            f"/api/review/history/coordinates?run_id={self.project.RUN_ID}"
            f"&action=unify&audit_id={self.outcome.audit_id}"
            "&q=b.mo&status=current&recovery=revertible&limit=1&offset=0"
        )
        with urllib.request.urlopen(self._url(path), timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(1, payload["total"])
        self.assertEqual(1, len(payload["coordinates"]))
        self.assertTrue(payload["coordinates"][0]["revertible"])

    def test_served_dashboard_exposes_coordinate_filtering_controls(self) -> None:
        with urllib.request.urlopen(self._url("/"), timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("coordinate 搜索", html)
        self.assertIn("仅可安全撤销", html)
        self.assertIn("全选本页可安全撤销", html)
        self.assertIn("before-image fallback", html)
