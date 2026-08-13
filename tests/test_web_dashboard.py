"""观测面板与受控任务启动器的契约测试。

重点不在页面长什么样，而在三条不可退让的性质：
  1. 写边界——只有无凭据预设与任务启动接受 POST；
  2. 不越界——只能读工作区与输出目录下的文本产物，路径穿越必须失败；
  3. 不泄密——响应里只能出现环境变量名，绝不出现凭据值。
其余是缺产物时的降级行为：面板必须能在流水线只跑了一半、或者游戏资源目录
根本不存在的机器上打开。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.quality_gate import QualityGateError, QualityGateResult
from localizer.application.artifact import ArtifactBuilder
from localizer.config.models import ProjectConfig, PublishSection, PublishTargetSection
from localizer.web.collector import DashboardCollector
from localizer.web.server import DashboardServer


def _config(root: Path) -> ProjectConfig:
    source = root / "source"
    source.mkdir(exist_ok=True)
    for name in ("prompt.md", "glossary.yaml", "rules.yaml"):
        (root / name).write_text("schema_version: 1\nterms: []\n", encoding="utf-8")
    data = {
        "schema_version": 1,
        "project": {"id": "game", "name": "面板测试", "game_version": "1"},
        "paths": {
            "source": source,
            "workspace": root / "workspace",
            "output": root / "output",
        },
        "languages": {"source": "ru-RU", "target": "zh-Hans"},
        "resources": {"adapters": [{"type": "gettext", "include": ["**/*.po"]}]},
        "prompt": {"template": root / "prompt.md"},
        "glossary": {"file": root / "glossary.yaml"},
        "rules": {"file": root / "rules.yaml"},
        "provider": {
            "base_url": "https://provider.invalid/v1",
            "api_key_env": "DASHBOARD_TEST_KEY",
            "model": "fake",
        },
        "tm": {"database": root / "tm.sqlite3"},
    }
    return (
        ProjectConfig.model_validate(data)
        if hasattr(ProjectConfig, "model_validate")
        else ProjectConfig.parse_obj(data)
    )


def _seed_run(root: Path, run_id: str = "run-1") -> None:
    workspace = root / "workspace" / "runs" / run_id
    (workspace / "reports").mkdir(parents=True)
    (workspace / "checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "units": {
                    "a": {"state": "succeeded", "translation": "你好"},
                    "b": {"state": "succeeded", "translation": "世界"},
                    "c": {"state": "failed", "translation": None},
                },
                "batches": [
                    {
                        "batch_id": "batch-000001",
                        "identities": ["a", "b"],
                        "resource_path": "ui/menu.mo",
                        "worker_id": "translation-0",
                        "state": "submitted",
                        "reason": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "timestamp": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "batch_id": "batch-000001",
                        "identities": ["a", "b"],
                        "resource_path": "ui/menu.mo",
                        "worker_id": "translation-0",
                        "state": "succeeded",
                        "reason": "",
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "timestamp": "2026-01-01T00:00:01+00:00",
                    },
                    {
                        "batch_id": "batch-000002",
                        "identities": ["c"],
                        "resource_path": "ui/hud.mo",
                        "worker_id": "translation-0",
                        "state": "failed",
                        "reason": "numbering mismatch",
                        "input_tokens": 10,
                        "output_tokens": 3,
                        "timestamp": "2026-01-01T00:00:02+00:00",
                    },
                ],
                "metrics": {
                    "requests": 2,
                    "input_tokens": 30,
                    "output_tokens": 11,
                    "translation_units_total": 3,
                    "translation_files_total": 2,
                    "completed_files": ["ui/menu.mo"],
                },
                "workers": {
                    "translation-0": {
                        "state": "running",
                        "resource_path": "ui/hud.mo",
                        "batch_id": "batch-000002",
                        "batch_size": 1,
                    }
                },
                "resources": {
                    "ui/menu.mo": {
                        "state": "completed",
                        "worker_id": "translation-0",
                        "units_total": 2,
                        "units_succeeded": 2,
                        "units_failed": 0,
                        "batches_total": 1,
                        "requests": 1,
                        "input_tokens": 20,
                        "output_tokens": 8,
                    },
                    "ui/hud.mo": {
                        "state": "running",
                        "worker_id": "translation-0",
                        "units_total": 1,
                        "units_succeeded": 0,
                        "units_failed": 1,
                        "batches_total": 1,
                        "requests": 1,
                        "input_tokens": 10,
                        "output_tokens": 3,
                        "batch_id": "batch-000002",
                        "batch_state": "failed",
                        "reason": "numbering mismatch",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    reports = root / "output" / "preview" / run_id / "reports"
    reports.mkdir(parents=True)
    (reports / "qa-report.json").write_text(
        json.dumps(
            {
                "summary": {"passed": False, "error_count": 2, "failed_unit_count": 1},
                "issues": [
                    {
                        "code": "placeholder_mismatch",
                        "severity": "error",
                        "message": "placeholder multiset differs from source",
                        "stable_identity": "a",
                        "relative_path": "ui/menu.mo",
                        "details": {"source": ["%s"], "target": []},
                    },
                    {
                        "code": "untranslated",
                        "severity": "error",
                        "message": "translation is identical to source text",
                        "stable_identity": "b",
                        "relative_path": "ui/hud.mo",
                        "details": {},
                    },
                    {
                        "code": "same_source_inconsistency",
                        "severity": "warning",
                        "message": "same source text has multiple translations",
                        "stable_identity": "c",
                        "relative_path": "ui/menu.mo",
                        "details": {},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


class CollectorTests(unittest.TestCase):
    def test_opens_on_a_machine_with_nothing_built_yet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            overview = collector.overview()
            # 工作区、输出、TM 全都不存在时也必须给出完整骨架，而不是抛异常。
            self.assertEqual("game", overview["project"]["id"])
            self.assertEqual(
                str(_config(root).cache.tokenizers),
                overview["paths"]["tokenizers"]["path"],
            )
            self.assertIsNone(overview["provider"]["tokenizer"])
            self.assertEqual(8, len(overview["pipeline"]))
            self.assertFalse(overview["tm"]["available"])
            self.assertEqual([], collector.list_runs())
            self.assertIsNone(collector.run_detail("nope"))

    def test_progress_and_qa_are_read_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_run(root)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            runs = collector.list_runs()
            self.assertEqual(1, len(runs))
            detail = collector.run_detail("run-1")

            progress = detail["progress"]
            self.assertEqual(3, progress["total"])
            self.assertEqual(2, progress["succeeded"])
            self.assertEqual(1, progress["failed"])

            qa = detail["qa"]
            self.assertFalse(qa["passed"])
            self.assertEqual(2, qa["error_count"])
            self.assertEqual({"error": 2, "warning": 1}, qa["by_severity"])

            # preview 不产出正式 Manifest，制品必须是「不可用」而不是伪造一个。
            self.assertFalse(detail["artifact"]["available"])
            self.assertEqual(2, detail["batches"]["total"])
            self.assertEqual({"succeeded": 1, "failed": 1}, detail["batches"]["by_state"])
            self.assertEqual(1, detail["batches"]["rows"][-1]["attempts"])
            self.assertEqual(41, detail["runtime"]["total_tokens"])
            self.assertEqual("ui/hud.mo", detail["runtime"]["workers"][0]["resource_path"])
            self.assertEqual(2, len(detail["runtime"]["files"]))
            self.assertEqual("ui/hud.mo", detail["runtime"]["files"][0]["relative_path"])
            self.assertEqual(100.0, detail["runtime"]["files"][0]["percent"])

    def test_qa_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_run(root)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            self.assertEqual(2, collector.qa_issues("run-1", severity="error")["total"])
            self.assertEqual(
                1, collector.qa_issues("run-1", code="untranslated")["total"]
            )
            self.assertEqual(2, collector.qa_issues("run-1", query="menu.mo")["total"])
            self.assertEqual(0, collector.qa_issues("run-1", query="不存在")["total"])

    def test_stage_is_blocked_when_quality_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_run(root)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            stage = collector.run_detail("run-1")["stage"]
            self.assertEqual("gate", stage["key"])
            self.assertEqual("blocked", stage["state"])

    def test_file_reader_refuses_paths_outside_the_run_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_run(root)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            allowed = root / "workspace" / "runs" / "run-1" / "checkpoint.json"
            self.assertIsNotNone(collector.read_text_file(str(allowed)))

            secret = root / "secret.txt"
            secret.write_text("ghp_not_a_real_token", encoding="utf-8")
            self.assertIsNone(collector.read_text_file(str(secret)))
            self.assertIsNone(
                collector.read_text_file(
                    str(root / "workspace" / "runs" / ".." / ".." / ".." / "secret.txt")
                )
            )
            # 二进制/非白名单后缀也不给读。
            blob = root / "workspace" / "runs" / "run-1" / "artifact.zip"
            blob.write_bytes(b"PK\x03\x04")
            self.assertIsNone(collector.read_text_file(str(blob)))

    def test_run_id_with_separators_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _seed_run(root)
            collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
            for bad in ("../secret", "a/b", "a\\b", ""):
                self.assertIsNone(collector.run_detail(bad))


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        _seed_run(root)
        collector = DashboardCollector(_config(root), root / "project.yaml", ROOT)
        self.server = DashboardServer(collector, port=0).start_background()
        self.base = self.server.url

    def tearDown(self) -> None:
        self.server.stop()
        self._temp.cleanup()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base.rstrip("/") + path, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, *, action_header: bool = True):
        headers = {"Content-Type": "application/json"}
        if action_header:
            headers["X-Localizer-Action"] = "1"
        request = urllib.request.Request(
            self.base.rstrip("/") + path,
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
        )
        with urllib.request.urlopen(request, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_index_is_self_contained(self) -> None:
        with urllib.request.urlopen(self.base, timeout=10) as res:
            body = res.read().decode("utf-8")
        self.assertEqual(200, res.status)
        self.assertIn("分析待翻译内容", body)
        self.assertIn("preflightPanel", body)
        self.assertIn('id="taskVariant"', body)
        self.assertIn("scopedApiPath", body)
        self.assertIn("variant: selectedVariant", body)
        self.assertIn("实时翻译", body)
        self.assertIn("viewLive", body)
        self.assertIn("launchRebuild", body)
        self.assertIn("/api/tasks/rebuild", body)
        self.assertIn("/api/tasks/publish", body)
        self.assertIn("发布到全部已配置目标", body)
        self.assertIn("rebuildVersion", body)
        self.assertIn("reuse_checkpoint_run_id", body)
        self.assertIn("syncMajorities", body)
        self.assertIn("/api/review/unify-majorities", body)
        self.assertIn("highlightVisible", body)
        self.assertIn("lineBreakSummary", body)
        self.assertIn("/api/review/glossary-exclude", body)
        # 页面不得引用任何外部资源：CSP 会拦，离线环境也打不开。
        for marker in ("http://", "https://", "//cdn", "<script src"):
            self.assertNotIn(marker, body.lower().replace("http://www.w3.org", ""))

    def test_api_endpoints(self) -> None:
        status, overview = self._get("/api/overview")
        self.assertEqual(200, status)
        self.assertEqual("game", overview["project"]["id"])

        status, runs = self._get("/api/runs")
        self.assertEqual(1, len(runs["runs"]))

        status, detail = self._get("/api/runs/run-1")
        self.assertEqual(2, detail["progress"]["succeeded"])

        status, qa = self._get("/api/runs/run-1/qa?severity=error")
        self.assertEqual(2, qa["total"])

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/runs/missing")
        self.assertEqual(404, ctx.exception.code)

    def test_write_methods_are_rejected(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            request = urllib.request.Request(
                self.base + "api/overview", method=method, data=b"{}"
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(405, ctx.exception.code)

    def test_task_profile_can_be_saved_without_credentials(self) -> None:
        root = Path(self._temp.name)
        status, profile = self._post(
            "/api/task-profiles",
            {
                "name": "WOT preview",
                "version": "1.44.0.0",
                "source_path": str(root / "source"),
                "mode": "preview",
            },
        )
        self.assertEqual(201, status)
        self.assertEqual("1.44.0.0", profile["version"])
        persisted = root / "workspace" / "web" / "task-profiles.json"
        self.assertTrue(persisted.is_file())
        body = persisted.read_text(encoding="utf-8")
        self.assertNotIn("DASHBOARD_TEST_KEY", body)
        self.assertNotIn("password", body.lower())

    def test_task_post_requires_local_action_header(self) -> None:
        root = Path(self._temp.name)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/task-profiles",
                {
                    "name": "blocked",
                    "version": "1",
                    "source_path": str(root / "source"),
                    "mode": "preview",
                },
                action_header=False,
            )
        self.assertEqual(403, ctx.exception.code)

    def test_task_launch_overrides_version_and_single_source_file(self) -> None:
        root = Path(self._temp.name)
        selected = root / "source" / "selected.po"
        selected.write_text("msgid \"\"\nmsgstr \"\"\n", encoding="utf-8")
        dotenv = root / "task.env"
        dotenv.write_text("WEB_TASK_DOTENV=loaded-for-one-run\n", encoding="utf-8")
        os.environ["WEB_TASK_DOTENV"] = "process-default"
        self.addCleanup(os.environ.pop, "WEB_TASK_DOTENV", None)
        captured = {}

        class FakeRunner:
            def __init__(self, config):
                captured["config"] = config

            def run(self, *, mode, run_id):
                captured.update(
                    {
                        "mode": mode.value,
                        "run_id": run_id,
                        "dotenv": os.environ.get("WEB_TASK_DOTENV"),
                    }
                )
                return SimpleNamespace(
                    extracted_units=1,
                    tm_hits=0,
                    machine_successes=1,
                    failed_units=0,
                    build=SimpleNamespace(
                        output_root=root / "output" / mode.value / run_id,
                        bundle=None,
                        quality_gate=SimpleNamespace(
                            passed=False, error_count=2, failed_unit_count=1
                        ),
                    ),
                )

        self.server.tasks.runner_factory = FakeRunner
        status, task = self._post(
            "/api/tasks/run",
            {
                "version": "1.44.0.0",
                "source_path": str(selected),
                "mode": "preview",
                "run_id": "web-run-1",
                "dotenv_path": str(dotenv),
            },
        )
        self.assertEqual(202, status)
        for _ in range(50):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertFalse(current["result"]["quality_gate"]["passed"])
        config = captured["config"]
        self.assertEqual("1.44.0.0", config.project.game_version)
        self.assertEqual(selected.parent.resolve(), config.paths.source)
        self.assertEqual([selected.name], config.resources.adapters[0].include)
        self.assertEqual("loaded-for-one-run", captured["dotenv"])
        self.assertEqual("process-default", os.environ["WEB_TASK_DOTENV"])
        snapshot = root / "workspace" / "runs" / "web-run-1" / "task-request.json"
        saved = json.loads(snapshot.read_text("utf-8"))
        self.assertEqual("completed", saved["status"])
        self.assertFalse(saved["result"]["quality_gate"]["passed"])
        self.assertNotIn("loaded-for-one-run", snapshot.read_text("utf-8"))

    def test_rebuild_from_selected_run_is_exposed_as_a_web_task(self) -> None:
        root = Path(self._temp.name)
        selected = root / "source" / "selected.po"
        selected.write_text('msgid "Hello"\nmsgstr ""\n', encoding="utf-8")
        dotenv = root / "rebuild.env"
        dotenv.write_text("WEB_REBUILD_DOTENV=from-parent\n", encoding="utf-8")
        parent_request = root / "workspace" / "runs" / "run-1" / "task-request.json"
        parent_request.write_text(
            json.dumps(
                {
                    "version": "1.44.0.0",
                    "source_path": str(selected),
                    "dotenv_path": str(dotenv),
                    "mode": "preview",
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        # 最新的零 Provider 子运行可能只有 task-request，没有 checkpoint。
        # Web 层应接受逻辑父运行，真正的祖先回溯由 Application Service 完成。
        (parent_request.parent / "checkpoint.json").unlink()
        captured = {}

        class RebuildRunner:
            def __init__(self, config):
                captured["config"] = config

            def rebuild_from_run(self, parent_run_id, *, mode, run_id):
                captured.update(
                    {
                        "parent_run_id": parent_run_id,
                        "mode": mode.value,
                        "run_id": run_id,
                        "dotenv": os.environ.get("WEB_REBUILD_DOTENV"),
                    }
                )
                return SimpleNamespace(
                    extracted_units=3,
                    tm_hits=0,
                    machine_successes=0,
                    failed_units=0,
                    rebuild=SimpleNamespace(
                        as_dict=lambda: {
                            "parent_run_id": parent_run_id,
                            "reuse_checkpoint_run_id": "ancestor-run",
                            "reused": 2,
                            "retried": 0,
                            "stale": 0,
                            "resolved_by_human": 1,
                        }
                    ),
                    build=SimpleNamespace(
                        output_root=root / "output" / mode.value / run_id,
                        bundle=None,
                        quality_gate=SimpleNamespace(
                            passed=True, error_count=0, failed_unit_count=0
                        ),
                    ),
                )

        self.server.tasks.runner_factory = RebuildRunner
        status, task = self._post(
            "/api/tasks/rebuild",
            {
                "parent_run_id": "run-1",
                "run_id": "web-rebuild-1",
                "version": "1.44.0.1",
                "mode": "preview",
            },
        )
        self.assertEqual(202, status)
        self.assertEqual("rebuild", task["kind"])
        self.assertEqual("run-1", task["parent_run_id"])
        self.assertEqual("1.44.0.1", task["version"])
        for _ in range(50):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertEqual("run-1", captured["parent_run_id"])
        self.assertEqual("web-rebuild-1", captured["run_id"])
        self.assertEqual("preview", captured["mode"])
        self.assertEqual("1.44.0.1", captured["config"].project.game_version)
        self.assertEqual(selected.parent.resolve(), captured["config"].paths.source)
        self.assertEqual([selected.name], captured["config"].resources.adapters[0].include)
        self.assertEqual("from-parent", captured["dotenv"])
        self.assertNotIn("WEB_REBUILD_DOTENV", os.environ)
        self.assertEqual(2, current["result"]["rebuild"]["reused"])
        self.assertEqual(
            "ancestor-run",
            current["result"]["rebuild"]["reuse_checkpoint_run_id"],
        )
        self.assertEqual(1, current["result"]["rebuild"]["resolved_by_human"])
        saved = json.loads(
            (root / "workspace" / "runs" / "web-rebuild-1" / "task-request.json")
            .read_text("utf-8")
        )
        self.assertEqual("run-1", saved["parent_run_id"])
        self.assertEqual("1.44.0.1", saved["version"])
        self.assertEqual("completed", saved["status"])
        _, child_detail = self._get("/api/runs/web-rebuild-1")
        self.assertEqual("rebuild", child_detail["task"]["kind"])
        self.assertEqual("run-1", child_detail["task"]["parent_run_id"])
        self.assertEqual("1.44.0.1", child_detail["task"]["version"])
        self.assertEqual(2, child_detail["task"]["rebuild"]["reused"])

    def test_rebuild_rejects_a_version_with_repeated_v_prefix(self) -> None:
        root = Path(self._temp.name)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/tasks/rebuild",
                {
                    "parent_run_id": "run-1",
                    "run_id": "invalid-version-child",
                    "version": "v1.44.0.2",
                    "mode": "release",
                },
            )
        self.assertEqual(400, ctx.exception.code)
        self.assertFalse(
            (root / "workspace" / "runs" / "invalid-version-child").exists()
        )

    def test_failed_preview_can_resume_identical_checkpoint_from_webui(self) -> None:
        root = Path(self._temp.name)
        run_path = root / "workspace" / "runs" / "resume-run"
        run_path.mkdir(parents=True)
        (run_path / "checkpoint.json").write_text(
            json.dumps({"schema_version": 2, "units": {}, "batches": []}),
            encoding="utf-8",
        )
        (run_path / "task-request.json").write_text(
            json.dumps(
                {
                    "task_id": "failed-task-id",
                    "run_id": "resume-run",
                    "version": "1.44.0.0",
                    "source_path": str(root / "source"),
                    "dotenv_path": None,
                    "mode": "preview",
                    "status": "failed",
                }
            ),
            encoding="utf-8",
        )
        calls = []

        class ResumeRunner:
            def __init__(self, config):
                self.config = config

            def run(self, *, mode, run_id):
                calls.append(run_id)
                return SimpleNamespace(
                    extracted_units=3,
                    tm_hits=0,
                    machine_successes=3,
                    failed_units=0,
                    build=SimpleNamespace(
                        output_root=root / "output" / mode.value / run_id,
                        bundle=None,
                        quality_gate=SimpleNamespace(
                            passed=True, error_count=0, failed_unit_count=0
                        ),
                    ),
                )

        self.server.tasks.runner_factory = ResumeRunner
        status, task = self._post(
            "/api/tasks/run",
            {
                "version": "1.44.0.0",
                "source_path": str(root / "source"),
                "mode": "preview",
                "run_id": "resume-run",
            },
        )
        self.assertEqual(202, status)
        self.assertEqual("failed-task-id", task["resumed_from_task_id"])
        for _ in range(50):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertEqual(["resume-run"], calls)
        saved = json.loads((run_path / "task-request.json").read_text("utf-8"))
        self.assertEqual("completed", saved["status"])
        self.assertEqual("failed-task-id", saved["resumed_from_task_id"])

    def test_failed_preview_resume_rejects_changed_parameters(self) -> None:
        root = Path(self._temp.name)
        run_path = root / "workspace" / "runs" / "resume-mismatch"
        run_path.mkdir(parents=True)
        (run_path / "checkpoint.json").write_text("{}", encoding="utf-8")
        (run_path / "task-request.json").write_text(
            json.dumps(
                {
                    "task_id": "failed-task-id",
                    "version": "old-version",
                    "source_path": str(root / "source"),
                    "dotenv_path": None,
                    "mode": "preview",
                    "status": "failed",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/tasks/run",
                {
                    "version": "new-version",
                    "source_path": str(root / "source"),
                    "mode": "preview",
                    "run_id": "resume-mismatch",
                },
            )
        self.assertEqual(400, ctx.exception.code)

    def test_preflight_lists_pending_files_and_is_revalidated_on_launch(self) -> None:
        root = Path(self._temp.name)
        selected = root / "source" / "selected.po"
        selected.write_text('msgid "Hello"\nmsgstr ""\n', encoding="utf-8")
        captured = {}

        class FakePlan:
            fingerprint = "plan-fingerprint-1"

            def as_dict(self):
                return {
                    "plan_fingerprint": self.fingerprint,
                    "files_total": 2,
                    "files_pending": 1,
                    "extracted_units": 12,
                    "tm_hits": 9,
                    "embedded_translations": 0,
                    "pending_units": 3,
                    "by_match_scope": {"legacy_coordinate_exact": 9},
                    "files": [
                        {
                            "relative_path": "selected.po",
                            "extracted_units": 12,
                            "tm_hits": 9,
                            "embedded_translations": 0,
                            "pending_units": 3,
                            "by_match_scope": {"legacy_coordinate_exact": 9},
                        }
                    ],
                }

        class PlanningRunner:
            def __init__(self, config):
                self.config = config

            def plan(self):
                captured["plan_calls"] = captured.get("plan_calls", 0) + 1
                return FakePlan()

            def prepare_translation_runtime(self):
                captured["runtime_prepares"] = (
                    captured.get("runtime_prepares", 0) + 1
                )
                return SimpleNamespace(resolved_source=root / "tokenizer-snapshot")

            def run(self, *, mode, run_id, plan=None):
                captured["executed_plan"] = plan.fingerprint
                return SimpleNamespace(
                    extracted_units=12,
                    tm_hits=9,
                    machine_successes=3,
                    failed_units=0,
                    build=SimpleNamespace(
                        output_root=root / "output" / mode.value / run_id,
                        bundle=None,
                        quality_gate=SimpleNamespace(
                            passed=True, error_count=0, failed_unit_count=0
                        ),
                    ),
                )

        self.server.tasks.runner_factory = PlanningRunner
        status, preflight = self._post(
            "/api/tasks/preflight",
            {
                "version": "1.44.0.0",
                "source_path": str(selected),
                "mode": "preview",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(1, preflight["files_pending"])
        self.assertEqual(3, preflight["pending_units"])
        self.assertEqual("selected.po", preflight["files"][0]["relative_path"])
        self.assertTrue(preflight["runtime"]["tokenizer_ready"])
        self.assertEqual(
            str(root / "tokenizer-snapshot"),
            preflight["runtime"]["tokenizer_source"],
        )
        self.assertEqual(1, captured["runtime_prepares"])

        status, task = self._post(
            "/api/tasks/run",
            {
                "version": "1.44.0.0",
                "source_path": str(selected),
                "mode": "preview",
                "run_id": "preflight-run",
                "preflight_id": preflight["preflight_id"],
            },
        )
        self.assertEqual(202, status)
        for _ in range(50):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertEqual("plan-fingerprint-1", captured["executed_plan"])
        self.assertEqual(2, captured["plan_calls"])

    def test_task_parameters_cannot_change_after_preflight(self) -> None:
        root = Path(self._temp.name)
        selected = root / "source" / "selected.po"
        selected.write_text('msgid "Hello"\nmsgstr ""\n', encoding="utf-8")

        class FakePlan:
            fingerprint = "fixed"

            def as_dict(self):
                return {
                    "plan_fingerprint": self.fingerprint,
                    "files_total": 1,
                    "files_pending": 1,
                    "extracted_units": 1,
                    "tm_hits": 0,
                    "embedded_translations": 0,
                    "pending_units": 1,
                    "by_match_scope": {},
                    "files": [],
                }

        class PlanningRunner:
            def __init__(self, config):
                pass

            def plan(self):
                return FakePlan()

        self.server.tasks.runner_factory = PlanningRunner
        _, preflight = self._post(
            "/api/tasks/preflight",
            {"version": "1", "source_path": str(selected), "mode": "preview"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/tasks/run",
                {
                    "version": "2",
                    "source_path": str(selected),
                    "mode": "preview",
                    "preflight_id": preflight["preflight_id"],
                },
            )
        self.assertEqual(400, ctx.exception.code)

    def test_release_gate_rejection_is_completed_with_qa_failed(self) -> None:
        root = Path(self._temp.name)
        selected = root / "source" / "gate.mo"
        selected.write_bytes(b"fixture")

        class GateRejectedRunner:
            def __init__(self, config):
                self.config = config

            def run(self, *, mode, run_id):
                raise QualityGateError(
                    "release blocked",
                    QualityGateResult(False, error_count=3, failed_unit_count=2),
                )

        self.server.tasks.runner_factory = GateRejectedRunner
        status, task = self._post(
            "/api/tasks/run",
            {
                "version": "1.44.0.0",
                "source_path": str(selected),
                "mode": "release",
                "run_id": "web-gate-blocked",
            },
        )
        self.assertEqual(202, status)
        for _ in range(50):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertFalse(current["result"]["quality_gate"]["passed"])
        self.assertEqual(3, current["result"]["quality_gate"]["error_count"])
        self.assertEqual(2, current["result"]["failed_units"])
        self.assertIsNone(current["result"]["artifact"])
        self.assertIsNone(current["error"])

    def test_quality_gate_passed_release_can_be_explicitly_published(self) -> None:
        root = Path(self._temp.name)
        resource_root = root / "rendered"
        resource_root.mkdir()
        resource = resource_root / "messages.mo"
        resource.write_bytes(b"release fixture")
        run_id = "web-publish-run"
        bundle = ArtifactBuilder().build_release(
            project_id="game",
            run_id=run_id,
            resource_root=resource_root,
            resource_paths=[resource],
            destination=root / "output" / "release" / run_id,
            version="1.44.0.0",
            variant="ru",
            compatibility_metadata={
                "enabled": True,
                "format": "legacy_v6",
                "filename": "metadata.json",
                "env": "RU",
            },
        )
        request_dir = root / "workspace" / "runs" / run_id
        request_dir.mkdir(parents=True)
        (request_dir / "task-request.json").write_text(
            json.dumps({"dotenv_path": None}), encoding="utf-8"
        )
        published = root / "published"
        self.server.tasks.config.publish = PublishSection(
            targets=[PublishTargetSection(type="local", destination=published)]
        )

        status, task = self._post("/api/tasks/publish", {"run_id": run_id})
        self.assertEqual(202, status)
        for _ in range(100):
            _, current = self._get(f"/api/tasks/{task['task_id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual("completed", current["status"])
        self.assertTrue(current["result"]["passed"])
        self.assertTrue((published / bundle.artifact.name).is_file())
        self.assertTrue((published / "metadata.json").is_file())
        self.assertTrue((published / bundle.manifest.name).is_file())

    def test_provider_credentials_are_never_echoed(self) -> None:
        # 把哨兵值放进配置指名的那个环境变量。面板若有任何一处读取并回显它，
        # 哨兵就会出现在响应里——这比检查字段名可靠得多。
        sentinel = "sk-dashboard-sentinel-must-never-be-echoed"
        os.environ["DASHBOARD_TEST_KEY"] = sentinel
        self.addCleanup(os.environ.pop, "DASHBOARD_TEST_KEY", None)

        _, overview = self._get("/api/overview")
        self.assertEqual("DASHBOARD_TEST_KEY", overview["provider"]["api_key_env"])

        for path in ("/api/overview", "/api/runs", "/api/runs/run-1",
                     "/api/runs/run-1/qa"):
            _, payload = self._get(path)
            self.assertNotIn(sentinel, json.dumps(payload, ensure_ascii=False),
                             msg=f"{path} 回显了凭据值")

    def test_file_endpoint_rejects_traversal(self) -> None:
        outside = Path(self._temp.name) / "secret.txt"
        outside.write_text("ghp_not_a_real_token", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/file?path=" + urllib.request.quote(str(outside)))
        self.assertEqual(404, ctx.exception.code)



class QaIssueRowsCarryProvenanceTests(unittest.TestCase):
    """审查视图靠 provenance 区分「本次新译（零容忍）」与「存量债」（W0d）。"""

    def test_row_carries_provenance_and_labels_cover_every_emitted_code(self) -> None:
        import re

        from localizer.web.collector import QA_CODE_LABELS

        source = (
            ROOT / "src" / "localizer" / "application" / "local_build.py"
        ).read_text("utf-8")
        # 抓 QARecord( 后面第一个字符串字面量 —— 那就是 code。
        emitted = set(re.findall(r'QARecord\(\s*\n?\s*"([a-z_]+)"', source))
        emitted |= {"empty_translation", "source_language_residue"}  # ValidationRule 产的
        missing = emitted - set(QA_CODE_LABELS)
        self.assertEqual(
            set(), missing, f"这些 code 在面板上没有中文说明：{sorted(missing)}"
        )

if __name__ == "__main__":
    unittest.main()
