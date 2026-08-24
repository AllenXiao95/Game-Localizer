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
LOCALE_BRIDGE = STATIC_ROOT / "workflow-locale-bridge.js"
PUBLISH_WORKFLOW = STATIC_ROOT / "workflow-publish-ux.js"
WORKFLOW_I18N = STATIC_ROOT / "workflow-i18n.json"
PUBLISH_I18N = STATIC_ROOT / "workflow-publish-i18n.json"
INDEX = STATIC_ROOT / "index.html"


def _node_check(test: unittest.TestCase, path) -> None:
    node = shutil.which("node")
    if not node:
        test.skipTest("需要 node --check 才能校验 Dashboard JavaScript")
    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8"
    )
    test.assertEqual(0, result.returncode, result.stderr)


class WorkflowInjectionTests(unittest.TestCase):
    def test_extension_order_is_i18n_legacy_workflow_bridge_publish(self) -> None:
        rendered = render_dashboard_html(INDEX.read_text(encoding="utf-8"))
        positions = [
            rendered.index(_I18N_MARKER),
            rendered.index("const $ ="),
            rendered.index(_WORKFLOW_MARKER),
            rendered.index("window.LocalizerWorkflowUX"),
            rendered.index("window.LocalizerWorkflowLocaleBridge"),
            rendered.index("window.LocalizerWorkflowPublishUX"),
            rendered.index("</body>"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_extension_injection_is_idempotent_and_keeps_one_dom_observer(self) -> None:
        source = INDEX.read_text(encoding="utf-8")
        once = render_dashboard_html(source)
        twice = render_dashboard_html(once)
        self.assertEqual(once, twice)
        self.assertEqual(1, twice.count(_I18N_MARKER))
        self.assertEqual(1, twice.count(_WORKFLOW_MARKER))
        self.assertEqual(1, twice.count("const observer = new MutationObserver"))

    def test_workflow_and_publish_copy_live_in_shared_catalogs(self) -> None:
        workflow = dict(json.loads(WORKFLOW_I18N.read_text(encoding="utf-8"))["phrases"])
        publish = dict(json.loads(PUBLISH_I18N.read_text(encoding="utf-8"))["phrases"])
        self.assertEqual("Workflow", workflow["工作流"])
        self.assertEqual("Run preflight", workflow["运行预检"])
        self.assertEqual("Recommended next step", workflow["推荐下一步"])
        self.assertEqual("Publish receipt", publish["发布回执"])
        self.assertEqual("Republish (idempotent)", publish["重新发布（幂等）"])
        self.assertIn("target-level", publish["上次发布未完全成功；查看下方 target 原因后再重试。"])


class WorkflowStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    @staticmethod
    def state(**updates) -> dict:
        value = {
            "hasRun": False, "hasPreflight": False, "staleCount": 0,
            "activeTab": "summary", "taskStatus": "", "publishTaskStatus": "",
            "stageKey": "", "stageState": "", "qaAvailable": False,
            "qaPassed": None, "artifactAvailable": False, "publishAvailable": False,
            "publishStatus": "", "publishPassed": None, "mode": "preview",
        }
        value.update(updates)
        return value

    def run_cases(self, *cases: dict) -> list[dict]:
        if not self.node:
            self.skipTest("需要 node 才能执行 Dashboard workflow 状态映射")
        start = self.source.index("const RUN_STAGE_KEYS")
        end = self.source.index("\n  function installStyles()", start)
        logic = self.source[start:end]
        script = (
            logic + "\nconst cases=" + json.dumps(cases, ensure_ascii=False)
            + ";console.log(JSON.stringify(cases.map(x=>({r:workflowRecommendation(x),"
            + "s:Object.fromEntries(stepStates(x).map(([k,_l,v])=>[k,v]))}))));"
        )
        result = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_prepare_preflight_stale_and_start_mapping(self) -> None:
        results = self.run_cases(
            self.state(), self.state(hasPreflight=True),
            self.state(hasPreflight=True, staleCount=4),
        )
        self.assertEqual("preflight", results[0]["r"]["id"])
        self.assertEqual("start", results[1]["r"]["id"])
        self.assertEqual("resolve-stale", results[2]["r"]["id"])
        self.assertEqual("blocked", results[2]["s"]["preflight"])

    def test_run_gate_and_repair_mapping(self) -> None:
        results = self.run_cases(
            self.state(hasRun=True, taskStatus="running", stageKey="translate", stageState="running"),
            self.state(hasRun=True, stageKey="gate", stageState="blocked",
                       qaAvailable=True, qaPassed=False, mode="release"),
            self.state(hasRun=True, stageKey="gate", stageState="blocked",
                       qaAvailable=True, qaPassed=False, activeTab="qa", mode="release"),
        )
        self.assertEqual(("live", "live"), (results[0]["r"]["id"], results[0]["r"]["tab"]))
        self.assertEqual("qa", results[1]["r"]["id"])
        self.assertEqual("blocked", results[1]["s"]["validate"])
        self.assertEqual("ready", results[1]["s"]["repair"])
        self.assertEqual(("repair", "review"), (results[2]["r"]["id"], results[2]["r"]["tab"]))

    def test_publish_task_has_priority_over_generic_running_state(self) -> None:
        (result,) = self.run_cases(self.state(
            hasRun=True, taskStatus="completed", publishTaskStatus="queued",
            stageKey="build", stageState="done", qaAvailable=True, qaPassed=True,
            artifactAvailable=True, mode="release",
        ))
        self.assertEqual("publish-running", result["r"]["id"])
        self.assertEqual("artifact", result["r"]["tab"])
        self.assertEqual("running", result["s"]["publish"])
        self.assertEqual("done", result["s"]["build"])

    def test_publish_ready_failure_and_terminal_success_are_distinct(self) -> None:
        ready, failed, done = self.run_cases(
            self.state(hasRun=True, qaAvailable=True, qaPassed=True,
                       artifactAvailable=True, stageKey="build", stageState="done", mode="release"),
            self.state(hasRun=True, qaAvailable=True, qaPassed=True, artifactAvailable=True,
                       publishAvailable=True, publishStatus="failed", publishPassed=False, mode="release"),
            self.state(hasRun=True, qaAvailable=True, qaPassed=True, artifactAvailable=True,
                       publishAvailable=True, publishStatus="completed", publishPassed=True, mode="release"),
        )
        self.assertEqual(("publish-ready", "ready"), (ready["r"]["id"], ready["s"]["publish"]))
        self.assertEqual(("publish-failed", "blocked"), (failed["r"]["id"], failed["s"]["publish"]))
        self.assertEqual(("complete", "terminal", "done"),
                         (done["r"]["id"], done["r"]["kind"], done["s"]["publish"]))


class WorkflowInteractionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKFLOW.read_text(encoding="utf-8")

    def test_scripts_are_valid_javascript(self) -> None:
        for path in (WORKFLOW, LOCALE_BRIDGE, PUBLISH_WORKFLOW):
            with self.subTest(path=path.name):
                _node_check(self, path)

    def test_review_dirty_guard_covers_tab_run_pipeline_and_queue_navigation(self) -> None:
        for token in (
            '$("tabs")?.addEventListener("click"', '$("runlist")?.addEventListener("click"',
            '$("pipeline")?.addEventListener("click"', '$("tabBody")?.addEventListener("click"',
            '#reviewQueue .qitem[data-idx]', "guardReviewNavigation",
        ):
            self.assertIn(token, self.source)

    def test_busy_guard_releases_if_validation_stops_before_post(self) -> None:
        self.assertIn("data-ux-request-started", self.source)
        self.assertIn('button.dataset.uxRequestStarted !== "true"', self.source)
        self.assertIn("releaseBusyControl(button)", self.source)
        self.assertIn("event.stopImmediatePropagation()", self.source)

    def test_main_views_are_prioritized_over_diagnostics(self) -> None:
        self.assertIn('["summary", "live", "qa", "review", "artifact", "batches", "files"]', self.source)
        self.assertIn("diagnostic-tab", self.source)
        self.assertIn("diagnostic-first", self.source)
        self.assertIn("tab-group-label", self.source)
        self.assertNotIn('.tabs::before { content:"工作流"', self.source)

    def test_keyboard_and_aria_semantics_cover_tabs_runs_and_pipeline(self) -> None:
        for token in (
            'setAttribute("role", "tablist")', 'setAttribute("role", "listbox")',
            'setAttribute("role", "option")', 'aria-current="step"',
            '"ArrowLeft", "ArrowRight", "Home", "End"', '"ArrowUp", "ArrowDown"',
        ):
            self.assertIn(token, self.source)

    def test_mutations_expose_ownership_and_disabled_reasons(self) -> None:
        for control in (
            "launchTask", "confirmPreflightStale", "launchRebuild", "publishRelease",
            "syncMajorities", "clusterCommitChanged", "clusterExclude", "unitCommit",
        ):
            self.assertIn(f"{control}:", self.source)
        self.assertIn("aria-description", self.source)
        self.assertIn("launchHint", self.source)
        self.assertIn("QualityGate 未通过，不能发布。", self.source)
        self.assertIn("正式制品不存在，不能发布。", self.source)

    def test_auto_refresh_has_explicit_paused_state(self) -> None:
        self.assertIn("自动刷新已暂停", self.source)
        self.assertIn('addEventListener("change", syncAutoRefresh)', self.source)

    def test_locale_bridge_translates_non_dom_confirm_and_accessibility_copy(self) -> None:
        bridge = LOCALE_BRIDGE.read_text(encoding="utf-8")
        self.assertIn("window.confirm =", bridge)
        self.assertIn("api.translateText", bridge)
        self.assertIn("[data-ux-scope]", bridge)
        self.assertNotIn("MutationObserver", bridge)


class PublishReceiptUXTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.node = shutil.which("node")

    def test_target_receipt_executes_uploaded_skipped_and_failure_reason_logic(self) -> None:
        if not self.node:
            self.skipTest("需要 node 才能执行 publish receipt renderer")
        start = self.source.index("function targetResult(")
        end = self.source.index("\n  function receiptPanel()", start)
        helper = self.source[start:end]
        target = {
            "target": "r2", "status": "failed",
            "objects": [{"skipped": False}, {"skipped": False}, {"skipped": True}],
            "error_message": "credential rejected",
        }
        script = (
            "const esc=x=>String(x),num=x=>String(x),publishSummary=x=>`${x.target}:${x.status}`;\n"
            + helper + "\nconsole.log(targetResult(" + json.dumps(target) + "));"
        )
        result = subprocess.run(
            [self.node, "-e", script], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("上传 2", result.stdout)
        self.assertIn("跳过 1", result.stdout)
        self.assertIn("credential rejected", result.stdout)

    def test_terminal_success_and_failure_have_real_receipt_and_recovery_paths(self) -> None:
        for token in (
            "detail?.publish", "receipt.source", "receipt.targets", "receipt.started_at",
            "receipt.finished_at", "重新发布（幂等）", "重试发布到已配置目标",
            "主工作流已完成", "上次发布未完全成功",
        ):
            self.assertIn(token, self.source)
        self.assertNotIn('self.source.lower() if "publish-result"', self.source)

    def test_selected_run_active_publish_disables_duplicate_submit(self) -> None:
        self.assertIn('item.kind === "publish" && item.run_id === selectedRun', self.source)
        self.assertIn("button.disabled = true", self.source)
        self.assertIn("禁止重复提交", self.source)


if __name__ == "__main__":
    unittest.main()
