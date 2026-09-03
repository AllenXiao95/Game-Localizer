from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from localizer.application.review_log import ReviewDecisionEvent
from localizer.web import DashboardServer as ProductDashboardServer
from localizer.web.collector import DashboardCollector
from localizer.web.project_history_detail import project_history_coordinates
from localizer.web.review_coordinator import CoordinatedReviewService
from localizer.web.review_recovery import project_change_history, safe_revert
from test_review_recovery import _RecoveryCase
from test_web_review import _Project


class ReviewEventCompatibilityTests(unittest.TestCase):
    def test_legacy_v1_event_without_after_still_loads(self) -> None:
        payload = {
            "schema_version": 1,
            "action": "skip",
            "run_id": "old-run",
            "targets": ["coordinate"],
            "translation": None,
            "reason": "legacy",
            "before": {},
            "details": {},
            "actor": {},
            "decision_id": "legacy-decision",
            "decided_at": "2026-08-01T00:00:00+00:00",
        }
        event = ReviewDecisionEvent.from_payload(payload)
        self.assertEqual({}, event.after)
        self.assertIn('"after": {}', event.to_line())


class CoordinatedRecoveryTests(_RecoveryCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = CoordinatedReviewService(
            self.project.config,
            output_root=self.project.config.paths.output,
            workspace_root=self.project.config.paths.workspace,
        )

    def test_new_tm_decisions_capture_after_image_and_use_it_without_run_index(self) -> None:
        identities, outcome, payload, _operation, rows = self._prepare_bad_unify()
        decision_id = rows[identities["g2"]]["decision_id"]
        event = next(
            item for item in self.service._log().read_all()
            if item.decision_id == decision_id
        )
        self.assertEqual(
            "这就对了！", event.after[identities["g2"]]["translation"]
        )

        index_path = self.service._index_path(self.project.RUN_ID)
        self.assertIsNotNone(index_path)
        index_path.unlink()
        detail = project_history_coordinates(
            self.service,
            run_id=self.project.RUN_ID,
            action="unify",
            audit_id=outcome.audit_id,
            query="b.mo",
            recovery="revertible",
        )
        self.assertFalse(detail["run_index_available"])
        self.assertEqual(1, detail["total"])
        self.assertEqual("after_image", detail["coordinates"][0]["recovery_proof"])

        result = safe_revert(
            self.service,
            self.project.RUN_ID,
            [decision_id],
            reason="after-image recovery",
            expected_log_revision=payload["log_revision"],
        )
        self.assertTrue(result["complete"])
        self.assertEqual("after_image", result["recovery_proof"])

    def test_large_project_history_operation_is_summary_only(self) -> None:
        log = self.service._log()
        audit_id = "large-audit"
        events = [
            ReviewDecisionEvent(
                action="skip",
                run_id="large-run",
                targets=(f"coordinate-{index}",),
                reason="large operation",
                before={
                    f"coordinate-{index}": {
                        "relative_path": f"radio/{index}.mo",
                        "logical_key": f"key-{index}",
                        "source_text": "Affirmative!",
                    }
                },
                details={"audit_id": audit_id},
            )
            for index in range(201)
        ]
        log.append(events, expected_revision=log.revision())

        payload = project_change_history(
            self.service, action="skip", run_id="large-run"
        )
        operation = next(
            item for item in payload["operations"]
            if item["audit_id"] == audit_id
        )
        self.assertEqual(201, operation["coordinate_count"])
        self.assertFalse(operation["coordinates_inline"])
        self.assertEqual([], operation["coordinates"])
        self.assertIsNone(operation["revertible_count"])


class DashboardCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        collector = DashboardCollector(
            self.project.config, self.project.config_path, ROOT
        )
        self.server = ProductDashboardServer(collector, port=0).start_background()

    def tearDown(self) -> None:
        self.server.stop()
        self._temp.cleanup()

    def test_product_dashboard_composes_review_and_presentation_layers(self) -> None:
        with urllib.request.urlopen(self.server.url, timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("<!-- localizer-dashboard-i18n -->", html)
        self.assertIn("<!-- localizer-dashboard-workflow-ux -->", html)
        self.assertIn("变更历史 / Recovery", html)
        self.assertIn("Review Change History", html)
        self.assertIn("项目状态", html)

    def test_task_and_review_services_share_one_tm_maintenance_lock(self) -> None:
        self.assertTrue(self.server.reviews)
        for service in self.server.reviews.values():
            self.assertIs(service.mutation_lock, self.server._tm_maintenance_lock)
        for service in self.server.task_services.values():
            self.assertIs(service._maintenance_lock, self.server._tm_maintenance_lock)
