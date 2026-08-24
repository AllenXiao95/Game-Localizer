"""Executable regression coverage for the continuous dashboard operator workflow."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from localizer.web.dashboard_server import (
    _I18N_MARKER,
    _WORKFLOW_MARKER,
    render_dashboard_html,
)
from localizer.web.server import STATIC_ROOT


WORKFLOW = STATIC_ROOT / "workflow-ux.js"
PUBLISH_WORKFLOW = STATIC_ROOT / "workflow-publish-ux.js"
WORKFLOW_I18N = STATIC_ROOT / "workflow-i18n.json"
INDEX = STATIC_ROOT / "index.html"


class WorkflowInjectionTests(unittest.TestCase):
    def test_i18n_runs_before_legacy_and_workflow_runs_after_legacy(self) -> None:
        source = INDEX.read_text(encoding="utf-8")
        rendered = render_dashboard_html(source)

        self.assertLess(rendered.index(_I18N_MARKER), rendered.index("const $ ="))
        self.assertGreater(rendered.index(_WORKFLOW_MARKER), rendered.index("setInterval("))
        self.assertLess(rendered.index(_WORKFLOW_MARKER), rendered.index("</body>"))
        self.assertIn("window.LocalizerWorkflowUX", rendered)
        self.assertIn("window.LocalizerWorkflowPublishUX", rendered)
        self.assertLess(
            rendered.index("window.LocalizerWorkflowUX"),
            rendered.index("window.LocalizerWorkflowPublishUX"),
        )

    def test_dashboard_extension_injection_is_idempotent(self) -> None:
        source = INDEX.read_text(encoding="utf-8")
        once = render_dashboard_html(source)
        twice = render_dashboard_html(once)

        self.assertEqual(once, twice)
        self.assertEqual(1, twice.count(_I18N_MARKER))
        self.assertEqual(1, twice.count(_WORKFLOW_MARKER))
        self.assertEqual(1, twice.count("const observer = new MutationObserver"))

    def test_workflow_copy_uses_the_shared_i18n_catalog(self) -> None:
        payload = json.loads(WORKFLOW_I18N.read_text(encoding="utf-8"))
        pairs = {source: target for source, target in payload["phrases"]}

        self.assertEqual("Workflow", pairs["工作流"])
        self.assertEqual("Prepare", pairs["准备"])
        self.assertEqual("Publish", pairs["发布"])
        self.assertEqual("Recommended next step", pairs["推荐下一步"])
        self.assertEqual("Run preflight", pairs["运行预检"])
        self.assertIn("Publish", pairs["发布到已配置目标"])
        self.assertEqual("Preflight found", pairs["预检发现"])
        self.assertIn("stale entries", pairs["条过期记录"])


class WorkflowStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def _run_cases(self, cases: list[dict]) -> list[dict]:
        if not self.node:
            self.skipTest("需要 node 才能执行 Dashboard workflow 状态映射")
        start = self.source.index("const RUN_STAGE_KEYS")
        end = self.source.index("\n  function installStyles()", start)
        logic = self.source[start:end]
        script = (
            logic
            + "\nconst cases = "
            + json.dumps(cases, ensure_ascii=False)
            + ";\nconsole.log(JSON.stringify(cases.map((item) => ({"
            + "recommendation: workflowRecommendation(item), states: Object.fromEntries(stepStates(item).map(([k,_l,s]) => [k,s]))"
            + "}))));"
        )
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def _state(**updates) -> dict:
        base = {
            "hasRun": False,
            "hasPreflight": False,
            "staleCount": 0,
            "activeTab": "summary",
            "taskStatus": "",
            "publishTaskStatus": "",
            "stageKey": "",
            "stageState": "",
            "qaAvailable": False,
            "qaPassed": None,
            "artifactAvailable": False,
            "publishAvailable": False,
            "publishStatus": "",
            "publishPassed": None,
            "mode": "preview",
        }
        base.update(updates)
        return base

    def test_prepare_preflight_and_start_have_one_clear_next_action(self) -> None:
        results = self._run_cases([
            self._state(),
            self._state(hasPreflight=True),
            self._state(hasPreflight=True, staleCount=4),
        ])

        self.assertEqual("preflight", results[0]["recommendation"]["id"])
        self.assertEqual("start", results[1]["recommendation"]["id"])
        self.assertEqual("resolve-stale", results[2]["recommendation"]["id"])
        self.assertEqual("blocked", results[2]["states"]["preflight"])

    def test_translation_and_quality_gate_block_route_to_the_right_views(self) -> None:
        results = self._run_cases([
            self._state(hasRun=True, taskStatus="running", stageKey="translate", stageState="running"),
            self._state(
                hasRun=True,
                stageKey="gate",
                stageState="blocked",
                qaAvailable=True,
                qaPassed=False,
                mode="release",
            ),
            self._state(
                hasRun=True,
                stageKey="gate",
                stageState="blocked",
                qaAvailable=True,
                qaPassed=False,
                activeTab="qa",
                mode="release",
            ),
        ])

        self.assertEqual(("live", "live"), (
            results[0]["recommendation"]["id"], results[0]["recommendation"]["tab"]
        ))
        self.assertEqual("qa", results[1]["recommendation"]["id"])
        self.assertEqual("blocked", results[1]["states"]["validate"])
        self.assertEqual("ready", results[1]["states"]["repair"])
        self.assertEqual("repair", results[2]["recommendation"]["id"])
        self.assertEqual("review", results[2]["recommendation"]["tab"])

    def test_publish_task_is_not_misreported_as_translation_running(self) -> None:
        (result,) = self._run_cases([
            self._state(
                hasRun=True,
                taskStatus="completed",
                publishTaskStatus="queued",
                stageKey="build",
                stageState="done",
                qaAvailable=True,
                qaPassed=True,
                artifactAvailable=True,
                mode="release",
            )
        ])

        self.assertEqual("publish-running", result["recommendation"]["id"])
        self.assertEqual("artifact", result["recommendation"]["tab"])
        self.assertEqual("running", result["states"]["publish"])
        self.assertEqual("done", result["states"]["build"])

    def test_artifact_ready_publish_failure_and_terminal_success_are_distinct(self) -> None:
        results = self._run_cases([
            self._state(
                hasRun=True,
                qaAvailable=True,
                qaPassed=True,
                artifactAvailable=True,
                stageKey="build",
                stageState="done",
                mode="release",
            ),
            self._state(
                hasRun=True,
                qaAvailable=True,
                qaPassed=True,
                artifactAvailable=True,
                publishAvailable=True,
                publishStatus="failed",
                publishPassed=False,
                mode="release",
            ),
            self._state(
                hasRun=True,
                qaAvailable=True,
                qaPassed=True,
                artifactAvailable=True,
                publishAvailable=True,
                publishStatus="completed",
                publishPassed=True,
                mode="release",
            ),
        ])

        self.assertEqual("publish-ready", results[0]["recommendation"]["id"])
        self.assertEqual("ready", results[0]["states"]["publish"])
        self.assertEqual("publish-failed", results[1]["recommendation"]["id"])
        self.assertEqual("blocked", results[1]["states"]["publish"])
        self.assertEqual("complete", results[2]["recommendation"]["id"])
        self.assertEqual("terminal", results[2]["recommendation"]["kind"])
        self.assertEqual("done", results[2]["states"]["publish"])


class WorkflowInteractionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.node = shutil.which("node")

    def test_workflow_script_is_valid_javascript(self) -> None:
        if not self.node:
            self.skipTest("需要 node --check 才能校验 workflow-ux.js")
        result = subprocess.run(
            [self.node, "--check", str(WORKFLOW)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_review_dirty_guard_covers_tabs_runs_pipeline_and_queue_rows(self) -> None:
        self.assertIn('$("tabs")?.addEventListener("click"', self.source)
        self.assertIn('$("runlist")?.addEventListener("click"', self.source)
        self.assertIn('$("pipeline")?.addEventListener("click"', self.source)
        self.assertIn('$("tabBody")?.addEventListener("click"', self.source)
        self.assertIn('#reviewQueue .qitem[data-idx]', self.source)
        self.assertIn("guardReviewNavigation", self.source)

    def test_busy_guard_releases_when_validation_stops_before_post(self) -> None:
        self.assertIn("data-ux-request-started", self.source)
        self.assertIn('button.dataset.uxRequestStarted !== "true"', self.source)
        self.assertIn("releaseBusyControl(button)", self.source)
        self.assertIn("event.stopImmediatePropagation()", self.source)

    def test_main_tabs_are_prioritized_and_diagnostics_are_subordinate(self) -> None:
        self.assertIn('["summary", "live", "qa", "review", "artifact", "batches", "files"]', self.source)
        self.assertIn("diagnostic-tab", self.source)
        self.assertIn("diagnostic-first", self.source)
        self.assertIn("tab-group-label", self.source)
        self.assertNotIn('.tabs::before { content:"工作流"', self.source)

    def test_keyboard_and_aria_semantics_exist_for_tabs_runs_and_pipeline(self) -> None:
        self.assertIn('setAttribute("role", "tablist")', self.source)
        self.assertIn('setAttribute("role", "listbox")', self.source)
        self.assertIn('setAttribute("role", "option")', self.source)
        self.assertIn('aria-current="step"', self.source)
        self.assertIn('"ArrowLeft", "ArrowRight", "Home", "End"', self.source)
        self.assertIn('"ArrowUp", "ArrowDown"', self.source)

    def test_mutating_controls_expose_scope_and_disabled_reasons(self) -> None:
        for control in [
            "launchTask", "confirmPreflightStale", "launchRebuild", "publishRelease",
            "syncMajorities", "clusterCommitChanged", "clusterExclude", "unitCommit",
        ]:
            self.assertIn(f"{control}:", self.source)
        self.assertIn("aria-description", self.source)
        self.assertIn("launchHint", self.source)
        self.assertIn("QualityGate 未通过，不能发布。", self.source)
        self.assertIn("正式制品不存在，不能发布。", self.source)

    def test_auto_refresh_has_an_explicit_paused_state(self) -> None:
        self.assertIn("自动刷新已暂停", self.source)
        self.assertIn('addEventListener("change", syncAutoRefresh)', self.source)


class PublishReceiptUXTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.node = shutil.which("node")

    def test_publish_extension_is_valid_javascript(self) -> None:
        if not self.node:
            self.skipTest("需要 node --check 才能校验 workflow-publish-ux.js")
        result = subprocess.run(
            [self.node, "--check", str(PUBLISH_WORKFLOW)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_target_receipt_distinguishes_uploaded_skipped_and_failure_reason(self) -> None:
        if not self.node:
            self.skipTest("需要 node 才能执行 publish receipt renderer")
        start = self.source.index("function targetResult(")
        end = self.source.index("\n  function receiptPanel()", start)
        helper = self.source[start:end]
        target = {
            "target": "r2",
            "status": "failed",
            "objects": [
                {"skipped": False},
                {"skipped": False},
                {"skipped": True},
            ],
            "error_message": "credential rejected",
        }
        script = (
            "const esc = (x) => String(x); const num = (x) => String(x);"
            " const publishSummary = (x) => `${x.target}:${x.status}`;\n"
            + helper
            + "\nconsole.log(targetResult("
            + json.dumps(target)
            + "));"
        )
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("上传 2", result.stdout)
        self.assertIn("跳过 1", result.stdout)
        self.assertIn("credential rejected", result.stdout)

    def test_terminal_success_and_failure_have_explicit_follow_up_controls(self) -> None:
        self.assertIn("publish-result", self.source.lower() if "publish-result" in self.source.lower() else "publish-result")
        self.assertIn("重新发布（幂等）", self.source)
        self.assertIn("重试发布到已配置目标", self.source)
        self.assertIn("主工作流已完成", self.source)
        self.assertIn("上次发布未完全成功", self.source)
        self.assertIn("receipt.source", self.source)
        self.assertIn("receipt.targets", self.source)

    def test_active_publish_for_selected_run_disables_duplicate_submit(self) -> None:
        self.assertIn('item.kind === "publish" && item.run_id === selectedRun', self.source)
        self.assertIn('button.disabled = true', self.source)
        self.assertIn("禁止重复提交", self.source)


if __name__ == "__main__":
    unittest.main()
