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

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.application.review_log import ReviewDecisionEvent
from localizer.web import DashboardServer as ProductDashboardServer
from localizer.web.collector import DashboardCollector
from localizer.web.project_history_detail import project_history_coordinates
from localizer.web.review import ReviewConflict
from localizer.web.review_coordinator import CoordinatedReviewService
from localizer.web.review_recovery import (
    project_change_history,
    recovery_operations,
    safe_revert,
)
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
        run_id = self.project.RUN_ID
        group = next(
            item
            for item in self.service.groups(run_id)["groups"]
            if item["source"] == "Общий текст"
        )
        outcome = self.service.unify(
            run_id,
            group["group_id"],
            "这就对了！",
            reason="安全统一用于 after-image 回归",
        )
        payload = recovery_operations(self.service, run_id, action="unify")
        operation = next(
            item for item in payload["operations"] if item["audit_id"] == outcome.audit_id
        )
        row = next(
            item for item in operation["coordinates"] if item["logical_key"] == "g2"
        )
        decision_id = row["decision_id"]
        event = next(
            item for item in self.service._log().read_all()
            if item.decision_id == decision_id
        )
        self.assertEqual("这就对了！", event.after[row["stable_identity"]]["translation"])

        index_path = self.service._index_path(run_id)
        self.assertIsNotNone(index_path)
        index_path.unlink()
        detail = project_history_coordinates(
            self.service,
            run_id=run_id,
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
            run_id,
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


class DivergentHumanPreventionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        self.service = CoordinatedReviewService(
            self.project.config,
            output_root=self.project.config.paths.output,
            workspace_root=self.project.config.paths.workspace,
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _rows(self, identities):
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            return tm.rows_for(list(identities))

    def test_group_unify_refuses_divergent_existing_human_finalization(self) -> None:
        run_id = self.project.RUN_ID
        group = next(
            item
            for item in self.service.groups(run_id)["groups"]
            if item["source"] == "Общий текст"
        )
        protected = self.project.identity("g2")
        self.service.commit(
            run_id,
            {protected: "收到！"},
            reason="无线电语境人工定稿",
        )

        with self.assertRaises(ReviewConflict) as ctx:
            self.service.unify(
                run_id,
                group["group_id"],
                "这就对了！",
                reason="不应覆盖语境译法",
            )
        self.assertIn("人工定稿", str(ctx.exception))
        self.assertIn("b.mo:g2", str(ctx.exception))

        identities = [member["stable_identity"] for member in group["members"]]
        rows = self._rows(identities)
        self.assertEqual({protected}, set(rows))
        self.assertEqual("收到！", rows[protected]["translation"])
        self.assertEqual(1, len(self.service._log().read_all()))

    def test_majority_bulk_refuses_before_any_partial_write(self) -> None:
        run_id = self.project.RUN_ID
        protected = self.project.identity("m5")
        majority_ids = [self.project.identity(f"m{i}") for i in range(1, 6)]
        self.service.commit(
            run_id,
            {protected: "语境译法"},
            reason="保留上下文差异",
        )

        with self.assertRaises(ReviewConflict):
            self.service.unify_majorities(
                run_id,
                reason="多数派便利操作必须尊重人工定稿",
            )

        rows = self._rows(majority_ids)
        self.assertEqual({protected}, set(rows))
        self.assertEqual("语境译法", rows[protected]["translation"])
        self.assertEqual(1, len(self.service._log().read_all()))


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
