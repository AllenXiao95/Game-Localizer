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

    def test_intentional_variant_is_a_v1_log_action_not_a_new_store(self) -> None:
        event = ReviewDecisionEvent(
            action="intentional_variant",
            run_id="run-1",
            targets=("a", "b"),
            reason="radio acknowledgement uses a different translation",
            details={"group_id": "group"},
        )
        restored = ReviewDecisionEvent.from_payload(json.loads(event.to_line()))
        self.assertEqual("intentional_variant", restored.action)
        self.assertEqual(("a", "b"), restored.targets)
        self.assertEqual("group", restored.details["group_id"])


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

    def _group(self, source="Общий текст"):
        return next(
            item
            for item in self.service.groups(self.project.RUN_ID)["groups"]
            if item["source"] == source
        )

    def test_group_preview_overlays_current_tm_and_authority(self) -> None:
        run_id = self.project.RUN_ID
        protected = self.project.identity("g2")
        before = self._group()
        original = next(
            member for member in before["members"] if member["stable_identity"] == protected
        )
        self.assertEqual("g2", original["logical_key"])
        self.assertEqual("译法乙", original["current_translation"])
        self.assertFalse(original["current_from_tm"])
        self.assertFalse(original["current_human_authored"])

        self.service.commit(
            run_id,
            {protected: "收到！"},
            reason="无线电语境人工定稿",
        )

        after = self._group()
        current = next(
            member for member in after["members"] if member["stable_identity"] == protected
        )
        self.assertEqual("译法乙", current["translation"])
        self.assertEqual("收到！", current["current_translation"])
        self.assertTrue(current["current_from_tm"])
        self.assertEqual("human", current["current_origin"])
        self.assertEqual("reviewed", current["current_review_state"])
        self.assertTrue(current["current_is_formal"])
        self.assertTrue(current["current_human_authored"])

    def test_group_unify_refuses_divergent_existing_human_finalization(self) -> None:
        run_id = self.project.RUN_ID
        group = self._group()
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


class IntentionalVariantPreventionTests(unittest.TestCase):
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

    def _group(self, run_id=None, source="Общий текст"):
        return next(
            item
            for item in self.service.groups(run_id or self.project.RUN_ID)["groups"]
            if item["source"] == source
        )

    def _acknowledge(self, source="Общий текст"):
        group = self._group(source=source)
        session = self.service.session(self.project.RUN_ID)
        result = self.service.mark(
            self.project.RUN_ID,
            [
                {
                    "target_id": group["group_id"],
                    "action": "intentional_variant",
                    "reason": "无线电确认语境允许不同译法",
                }
            ],
            expected_log_revision=session["log_revision"],
        )
        return group, result

    def _clone_index(self, run_id: str, *, add_member: bool = False) -> Path:
        source = self.service._index_path(self.project.RUN_ID)
        self.assertIsNotNone(source)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if add_member:
            group = next(
                item for item in payload["same_source_groups"]
                if item["source"] == "Общий текст"
            )
            group["members"].append(
                {
                    "stable_identity": "new-coordinate",
                    "relative_path": "radio/new.mo",
                    "logical_key": "g-new",
                    "context": "new radio context",
                    "translation": "译法甲",
                }
            )
            group["member_count"] += 1
        target = (
            self.project.config.paths.output
            / "preview"
            / run_id
            / "reports"
            / "qa-review-index.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return target

    def test_exact_membership_acknowledgement_persists_across_run_ids(self) -> None:
        group, result = self._acknowledge()
        self.assertEqual(1, result["intentional_variant_groups"])
        self.assertEqual("pending", self.service.ledger(self.project.RUN_ID).state_of(group["group_id"]))

        current_sources = {item["source"] for item in self.service.groups(self.project.RUN_ID)["groups"]}
        self.assertNotIn("Общий текст", current_sources)

        self._clone_index("run-2")
        future_sources = {item["source"] for item in self.service.groups("run-2")["groups"]}
        self.assertNotIn("Общий текст", future_sources)
        counters = self.service.session("run-2")["counters"]
        self.assertEqual(2, counters["same_source_diagnostic_groups"])
        self.assertEqual(1, counters["same_source_groups"])
        self.assertEqual(1, counters["intentional_variant_groups"])

        event = self.service._log().read_all()[-1]
        self.assertEqual("intentional_variant", event.action)
        self.assertEqual(group["group_id"], event.details["group_id"])
        self.assertEqual(
            sorted(member["stable_identity"] for member in group["members"]),
            sorted(event.targets),
        )

    def test_new_same_source_coordinate_breaks_exact_membership_and_reappears(self) -> None:
        self._acknowledge()
        self._clone_index("run-2", add_member=True)
        group = self._group("run-2")
        self.assertEqual(4, group["member_count"])
        self.assertIn(
            "new-coordinate",
            {member["stable_identity"] for member in group["members"]},
        )

    def test_acknowledgement_does_not_modify_raw_qa_or_glossary_projection(self) -> None:
        index_path = self.service._index_path(self.project.RUN_ID)
        self.assertIsNotNone(index_path)
        report_path = index_path.parent / "qa-report.json"
        before_report = json.loads(report_path.read_text(encoding="utf-8"))
        before_warning_count = sum(
            1 for issue in before_report["issues"]
            if issue["code"] == "same_source_inconsistency"
        )
        before_glossary = self.service.glossary_clusters(self.project.RUN_ID)

        group, _result = self._acknowledge()

        raw_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertIn(
            group["group_id"],
            {item["group_id"] for item in raw_index["same_source_groups"]},
        )
        after_report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            before_warning_count,
            sum(
                1 for issue in after_report["issues"]
                if issue["code"] == "same_source_inconsistency"
            ),
        )
        self.assertEqual(before_glossary, self.service.glossary_clusters(self.project.RUN_ID))

    def test_majority_bulk_ignores_exact_acknowledged_group(self) -> None:
        group, _result = self._acknowledge(source="多数文本")
        majority_ids = [member["stable_identity"] for member in group["members"]]
        outcome = self.service.unify_majorities(
            self.project.RUN_ID,
            reason="只处理仍未解决的同源组",
            expected_log_revision=self.service.session(self.project.RUN_ID)["log_revision"],
        )
        self.assertEqual(0, outcome["groups_eligible"])
        self.assertEqual(0, outcome["items_written"])
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            self.assertEqual({}, tm.rows_for(majority_ids))


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
        self.assertIn("统一前坐标预览", html)
        self.assertIn("唯一最高频只代表当前分布", html)
        self.assertIn("确认保留语境差异", html)
        self.assertIn("未来多出任何同源坐标", html)

    def test_task_and_review_services_share_one_tm_maintenance_lock(self) -> None:
        self.assertTrue(self.server.reviews)
        for service in self.server.reviews.values():
            self.assertIs(service.mutation_lock, self.server._tm_maintenance_lock)
        for service in self.server.task_services.values():
            self.assertIs(service._maintenance_lock, self.server._tm_maintenance_lock)
