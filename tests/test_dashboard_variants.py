"""只配 `paths.sources` 的多目录项目，面板必须能打开且看的是对的目录（任务 3）。

两个 bug，第二个更安静也更糟：

1. `paths.source` 是 None → `collector.py` 的 `_path_status(None)` 直接
   `TypeError: expected str, bytes or os.PathLike object, not NoneType`，面板打不开。
2. **就算把崩溃挡掉**，`paths.workspace` / `paths.output` 指的是根目录，而 run 实际
   落在 `<root>/<variant>/` 下 —— 面板会正常打开、然后显示「没有运行」。
   那读起来像「什么都没跑过」，而不是「你在看错的目录」。

根因是 `dashboard` 从来没调过 `ProjectConfig.for_variant()`，尽管这个方法就是为
「把多目录项目投影成单目录配置」写的，CLI 的 `run`/`build` 都在用。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.web.collector import DashboardCollector
from localizer.web.server import (
    DashboardServer,
    VariantRequired,
    build_collector,
)

EMPTY_CHECKPOINT = json.dumps(
    {
        "schema_version": 3,
        "units": {},
        "batches": [],
        "metrics": {},
        "workers": {},
        "resources": {},
    }
)


class _Project:
    """在临时目录里搭一个真实可加载的项目配置。"""

    def __init__(self, root: Path, *, paths: dict, build: dict = None) -> None:
        self.root = root
        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        base["paths"] = paths
        if build:
            base["build"].update(build)
        self.path = root / "project.yaml"
        self.path.write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


def _multi_variant(root: Path, *, default: bool = True) -> _Project:
    for name in ("live", "pts"):
        (root / name).mkdir(exist_ok=True)
    paths = {
        "sources": {"live": str(root / "live"), "pts": str(root / "pts")},
        "workspace": str(root / "ws"),
        "output": str(root / "out"),
    }
    if default:
        paths["default_variant"] = "live"
    return _Project(
        root,
        paths=paths,
        build={
            "variant_overrides": {
                "live": {"variant": "ru", "compatibility_env": "RU"},
                "pts": {"variant": "pt", "compatibility_env": "PT"},
            }
        },
    )


def _seed_run(collector: DashboardCollector, run_id: str) -> None:
    """按 for_variant 的真实布局造一次运行。"""
    runs = Path(collector.config.paths.workspace) / "runs" / run_id
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "checkpoint.json").write_text(EMPTY_CHECKPOINT, encoding="utf-8")


class MultiVariantDashboardTests(unittest.TestCase):
    def test_overview_does_not_crash_and_names_the_active_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root)
            collector = build_collector(project.path, root)
            overview = collector.overview()
        self.assertEqual("live", overview["project"]["active_variant"])
        self.assertTrue(overview["paths"]["source"]["configured"])
        self.assertTrue(overview["paths"]["source"]["path"].endswith("live"))
        self.assertEqual(
            [("live", True), ("pts", False)],
            [(v["name"], v["active"]) for v in overview["project"]["variants"]],
        )

    def test_runs_of_the_selected_variant_are_visible(self) -> None:
        """这是比崩溃更安静的那个 bug：面板打开了，但一个 run 都看不到。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root)
            live = build_collector(project.path, root, variant="live")
            pts = build_collector(project.path, root, variant="pts")
            _seed_run(live, "run-live")
            _seed_run(pts, "run-pts")
            self.assertEqual(
                ["run-live"], [r["run_id"] for r in live.list_runs()]
            )
            self.assertEqual(["run-pts"], [r["run_id"] for r in pts.list_runs()])
            # 两个变体的工作区必须是不同目录，否则 run 会互相覆盖。
            self.assertNotEqual(
                Path(live.config.paths.workspace), Path(pts.config.paths.workspace)
            )

    def test_unprojected_config_would_have_found_nothing(self) -> None:
        """反证：说明上一条测的是真效果，不是「反正就一个 run」。

        直接拿未投影的配置构造 collector（即修复前 dashboard 的做法），
        run 一个都看不到。
        """
        from localizer.config import load_project_config

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root)
            live = build_collector(project.path, root, variant="live")
            _seed_run(live, "run-live")
            raw = load_project_config(project.path)
            self.assertIsNone(raw.paths.source)
            blind = DashboardCollector(raw, project.path, root)
            self.assertEqual([], blind.list_runs())

    def test_missing_default_variant_names_the_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root, default=False)
            with self.assertRaises(VariantRequired) as ctx:
                build_collector(project.path, root)
            message = str(ctx.exception)
            self.assertIn("live", message)
            self.assertIn("pts", message)
            self.assertIn("--variant", message)

    def test_unknown_variant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root)
            with self.assertRaises(VariantRequired):
                build_collector(project.path, root, variant="sandbox")

    def test_single_source_project_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "game").mkdir()
            project = _Project(
                root,
                paths={
                    "source": str(root / "game"),
                    "workspace": str(root / "ws"),
                    "output": str(root / "out"),
                },
            )
            collector = build_collector(project.path, root)
            _seed_run(collector, "r1")
            overview = collector.overview()
            # 单目录项目不该凭空长出变体概念，工作区也不该多一层子目录。
            self.assertEqual("", overview["project"]["active_variant"])
            self.assertEqual([], overview["project"]["variants"])
            self.assertTrue(
                (root / "ws").samefile(Path(collector.config.paths.workspace))
            )
            self.assertEqual(["r1"], [r["run_id"] for r in collector.list_runs()])


class PathStatusToleratesMissingPathTests(unittest.TestCase):
    """`_path_status` 的防御层。

    正常路径上 `build_collector` 已经投影过，不该再拿到 None；但面板是纯只读的
    观测面，任何一个字段缺失都不该让整个 overview 崩掉。
    """

    def test_none_is_reported_as_unconfigured_rather_than_raising(self) -> None:
        from localizer.config import load_project_config

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _multi_variant(root)
            raw = load_project_config(project.path)
            collector = DashboardCollector(raw, project.path, root)
            status = collector._path_status(None)
        self.assertEqual({"path": "", "exists": False, "configured": False}, status)


class MultiVariantOverHttpTests(unittest.TestCase):
    """走真实 HTTP 服务的端到端：崩溃是在 /api/overview 上被用户看到的。

    上面的用例只驱动 collector；这一条把 DashboardServer 真起起来，
    确认多目录项目下面板首屏能返回 200 而不是 500。
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        project = _multi_variant(self.root)
        live = build_collector(project.path, self.root, variant="live")
        collector = build_collector(project.path, self.root, variant="pts")
        _seed_run(live, "run-live")
        _seed_run(collector, "run-pts")
        self.server = DashboardServer(collector, port=0).start_background()

    def tearDown(self) -> None:
        self.server.stop()
        self._temp.cleanup()

    def _get(self, path: str):
        url = self.server.url.rstrip("/") + path
        with urllib.request.urlopen(url, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.server.url.rstrip("/") + path,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Localizer-Action": "1",
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        with urllib.request.urlopen(request, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_overview_returns_200_and_names_the_variant(self) -> None:
        status, payload = self._get("/api/overview")
        self.assertEqual(200, status)
        # 修复前这里是 500 + {"error": "TypeError"}。
        self.assertNotIn("error", payload)
        self.assertEqual("pts", payload["project"]["active_variant"])
        self.assertTrue(payload["paths"]["source"]["path"].endswith("pts"))

    def test_runs_endpoint_sees_the_variant_run(self) -> None:
        status, payload = self._get("/api/runs")
        self.assertEqual(200, status)
        self.assertEqual(["run-pts"], [r["run_id"] for r in payload["runs"]])

    def test_variant_query_switches_the_complete_read_scope(self) -> None:
        _, overview = self._get("/api/overview?variant=live")
        _, pts_overview = self._get("/api/overview?variant=pts")
        _, payload = self._get("/api/runs?variant=live")
        self.assertEqual("live", overview["project"]["active_variant"])
        self.assertTrue(overview["paths"]["source"]["path"].endswith("live"))
        self.assertEqual(("ru", "RU"), (
            overview["project"]["release_variant"], overview["project"]["release_env"]
        ))
        self.assertEqual(("pt", "PT"), (
            pts_overview["project"]["release_variant"],
            pts_overview["project"]["release_env"],
        ))
        self.assertEqual(["run-live"], [r["run_id"] for r in payload["runs"]])

    def test_task_profile_persists_variant_in_the_shared_store(self) -> None:
        status, profile = self._post(
            "/api/task-profiles",
            {
                "name": "PT preview",
                "version": "1.44.0.0",
                "source_path": str(self.root / "pts"),
                "mode": "preview",
                "variant": "pts",
            },
        )
        self.assertEqual(201, status)
        self.assertEqual("pts", profile["variant"])
        _, live_view = self._get("/api/task-profiles?variant=live")
        self.assertEqual("pts", live_view["profiles"][0]["variant"])
        self.assertTrue((self.root / "ws" / "web" / "task-profiles.json").is_file())

    def test_variant_body_routes_preflight_to_the_matching_task_service(self) -> None:
        captured = {}

        class Plan:
            def as_dict(self):
                return {
                    "plan_fingerprint": "pts-plan",
                    "files_total": 0,
                    "files_pending": 0,
                    "extracted_units": 0,
                    "tm_hits": 0,
                    "embedded_translations": 0,
                    "pending_units": 0,
                    "by_match_scope": {},
                    "files": [],
                }

        class Runner:
            def __init__(self, config):
                captured["variant"] = config.active_variant
                captured["source"] = Path(config.paths.source)

            def plan(self):
                return Plan()

        self.server.task_services["pts"].runner_factory = Runner
        status, preflight = self._post(
            "/api/tasks/preflight",
            {
                "version": "1.44.0.0",
                "source_path": str(self.root / "pts"),
                "mode": "preview",
                "variant": "pts",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("pts", preflight["variant"])
        self.assertEqual("pts", captured["variant"])
        self.assertTrue((self.root / "pts").samefile(captured["source"]))

    def test_unknown_variant_is_a_bad_request(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/overview?variant=sandbox")
        self.assertEqual(400, ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
