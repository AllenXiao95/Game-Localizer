"""WebUI 任务的两个静默数据缺口（R14 / R15）。

两条都不会报错、不会红，只会让人**以为**自己拿到了想要的东西：

- **R14**：单文件任务无条件把 `paths.source` 改成 `source.parent`。`stable_identity`
  含 `relative_path`，而 `relative_path` 是相对 `paths.source` 算的 —— 嵌套布局下
  选中 `<root>/gui/menu.mo`，`relative_path` 从 `gui/menu.mo` 变成 `menu.mo`。
  同一个词条换了身份：TM 全部命中失败（整轮重新付费），新译文写在错误坐标上，
  而且没有任何判据会报警，因为两边各自都完全自洽。当前唯一生产项目完全平铺，
  所以这条一直不可达 —— 接第二个游戏之前必须先修好。

- **R15**：恢复闸门只认 `status == "failed"`。进程被杀（Ctrl-C、断电、OOM、
  面板重启）留下的快照停在 `running`，永远走不进恢复路径，checkpoint 里那些
  已经付过钱的译文只能整轮作废、换 run_id 重跑。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.local_build import BuildMode
from localizer.config.models import ProjectConfig
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.web.tasks import TaskService


def _config(root: Path, *, sources=None) -> ProjectConfig:
    source = root / "game"
    (source / "gui").mkdir(parents=True, exist_ok=True)
    for name in ("prompt.md", "glossary.yaml", "rules.yaml"):
        (root / name).write_text("schema_version: 1\nterms: []\n", encoding="utf-8")
    paths = {"workspace": root / "workspace", "output": root / "output"}
    if sources is None:
        paths["source"] = source
    else:
        paths["sources"] = sources
        paths["default_variant"] = next(iter(sources))
    data = {
        "schema_version": 1,
        "project": {"id": "game", "name": "任务测试", "game_version": "1"},
        "paths": paths,
        "languages": {"source": "ru-RU", "target": "zh-Hans"},
        "resources": {"adapters": [{"type": "gettext", "include": ["**/*.mo"]}]},
        "prompt": {"template": root / "prompt.md"},
        "glossary": {"file": root / "glossary.yaml"},
        "rules": {"file": root / "rules.yaml"},
        "provider": {
            "base_url": "https://provider.invalid/v1",
            "api_key_env": "TASK_TEST_KEY",
            "model": "fake",
        },
        "tm": {"database": root / "tm.sqlite3"},
    }
    return (
        ProjectConfig.model_validate(data)
        if hasattr(ProjectConfig, "model_validate")
        else ProjectConfig.parse_obj(data)
    )


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def service(self, *, active=None, **kwargs) -> TaskService:
        """多变体项目必须先 `for_variant()` —— 生产里 server.py 启动时就投影了。

        不投影的配置没有 `paths.source`，「当前激活的是哪个变体」无从谈起。
        """
        config = _config(self.root, **kwargs)
        if kwargs.get("sources"):
            config = config.for_variant(active or next(iter(kwargs["sources"])))
        return TaskService(config)


class SingleFileIdentityTests(_Case):
    """R14：单文件任务不得改变词条坐标。"""

    def test_a_nested_file_keeps_the_configured_source_root(self) -> None:
        service = self.service()
        nested = self.root / "game" / "gui" / "menu.mo"
        nested.write_bytes(b"")
        config = service._overridden_config(nested, "1")
        self.assertEqual(self.root / "game", Path(config.paths.source))
        # include 收窄成相对源根的路径，而不是裸文件名。
        self.assertEqual(["gui/menu.mo"], config.resources.adapters[0].include)

    def test_relative_path_is_identical_to_a_whole_directory_run(self) -> None:
        """这条才是 R14 的实质：两种跑法必须给出同一个 stable_identity。

        比对的是扫描器算出来的 relative_path —— 它是 stable_identity 的
        输入，也是唯一会漂移的那一项。
        """
        from localizer.application.scan import ResourceScanner

        service = self.service()
        nested = self.root / "game" / "gui" / "menu.mo"
        nested.write_bytes(b"")

        whole = service._overridden_config(self.root / "game", "1")
        single = service._overridden_config(nested, "1")
        scanned = {}
        for label, config in (("whole", whole), ("single", single)):
            adapter = config.resources.adapters[0]
            result = ResourceScanner().scan(
                config.paths.source,
                includes=adapter.include,
                excludes=adapter.exclude,
            )
            scanned[label] = sorted(
                item.relative_path for item in result.resources
            )
        self.assertEqual(["gui/menu.mo"], scanned["single"])
        self.assertEqual(scanned["whole"], scanned["single"])

    def test_a_flat_file_is_unchanged(self) -> None:
        # 当前唯一生产项目就是这个形状，行为必须逐字不变。
        service = self.service()
        flat = self.root / "game" / "menu.mo"
        flat.write_bytes(b"")
        config = service._overridden_config(flat, "1")
        self.assertEqual(self.root / "game", Path(config.paths.source))
        self.assertEqual(["menu.mo"], config.resources.adapters[0].include)

    def test_a_file_outside_every_root_falls_back_to_its_parent(self) -> None:
        """源根之外本来就是另一套坐标体系，不存在「漂移」一说。"""
        service = self.service()
        outside = self.root / "elsewhere" / "menu.mo"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"")
        config = service._overridden_config(outside, "1")
        self.assertEqual(outside.parent, Path(config.paths.source))
        self.assertEqual(["menu.mo"], config.resources.adapters[0].include)

    def test_multi_variant_projects_use_the_active_root_not_the_deepest(self) -> None:
        """嵌套变体（live 与 live/pts）下按**深度**挑根是 R14 换了个形状。

        面板跑在 variant=live 时，`paths.source` 已被 `for_variant()` 投影成 live。
        选中 live/pts 下的文件按深度会被判给 pts 根，`relative_path` 比整目录
        跑法少一层 —— 同一个词条又换了身份。
        """
        from localizer.application.scan import ResourceScanner

        live = self.root / "variants" / "live"
        pts = live / "pts"
        (pts / "gui").mkdir(parents=True)
        nested = pts / "gui" / "menu.mo"
        nested.write_bytes(b"")
        service = self.service(sources={"live": live, "pts": pts})

        def scanned(config):
            adapter = config.resources.adapters[0]
            result = ResourceScanner().scan(
                config.paths.source,
                includes=adapter.include,
                excludes=adapter.exclude,
            )
            return sorted(item.relative_path for item in result.resources)

        whole = service._overridden_config(live, "1")
        single = service._overridden_config(nested, "1")
        self.assertEqual(live, Path(single.paths.source))
        self.assertEqual(["pts/gui/menu.mo"], scanned(single))
        self.assertEqual(scanned(whole), scanned(single))

    def test_running_as_pts_gives_the_pts_coordinates(self) -> None:
        """对照组：同一个文件在 variant=pts 下本来就该是另一套坐标。"""
        live = self.root / "variants" / "live"
        pts = live / "pts"
        (pts / "gui").mkdir(parents=True)
        nested = pts / "gui" / "menu.mo"
        nested.write_bytes(b"")
        service = self.service(sources={"live": live, "pts": pts}, active="pts")
        config = service._overridden_config(nested, "1")
        self.assertEqual(pts, Path(config.paths.source))
        self.assertEqual(["gui/menu.mo"], config.resources.adapters[0].include)

    def test_a_file_from_another_variant_is_refused_not_switched(self) -> None:
        """静默按另一套坐标入库比报错糟得多 —— 没有任何判据会发现。"""
        live = self.root / "variants" / "live"
        other = self.root / "variants" / "pts"
        live.mkdir(parents=True)
        other.mkdir(parents=True)
        stray = other / "menu.mo"
        stray.write_bytes(b"")
        service = self.service(sources={"live": live, "pts": other})
        with self.assertRaises(ValueError) as ctx:
            service._overridden_config(stray, "1")
        self.assertIn("pts", str(ctx.exception))


class SubdirectorySelectionTests(_Case):
    """选中**子目录**同样不得改变坐标（R14 的另一半）。

    `_overridden_config` 的 else 分支原本对任何目录都无条件
    `paths.source = source`，相对路径因此被截短。而 `_validated_request` 明确
    允许 `source_path` 是任意已存在目录，面板上直接填一个子目录就走这条路。
    """

    def _scanned(self, config):
        from localizer.application.scan import ResourceScanner

        adapter = config.resources.adapters[0]
        result = ResourceScanner().scan(
            config.paths.source, includes=adapter.include, excludes=adapter.exclude
        )
        return sorted(item.relative_path for item in result.resources)

    def _tree(self):
        game = self.root / "game"
        (game / "gui" / "sub").mkdir(parents=True, exist_ok=True)
        for relative in ("gui/menu.mo", "gui/sub/deep.mo", "top.mo"):
            (game / relative).write_bytes(b"")
        return game

    def test_a_subdirectory_keeps_the_same_relative_paths(self) -> None:
        game = self._tree()
        service = self.service()
        whole = self._scanned(service._overridden_config(game, "1"))
        subdir = self._scanned(service._overridden_config(game / "gui", "1"))
        self.assertEqual(["gui/menu.mo", "gui/sub/deep.mo", "top.mo"], whole)
        # 坐标一致，且确实只跑了那棵子树。
        self.assertEqual(["gui/menu.mo", "gui/sub/deep.mo"], subdir)
        self.assertTrue(set(subdir) < set(whole))

    def test_a_subdirectory_scan_reaches_files_directly_inside_it(self) -> None:
        """`gui/**/*.mo` 匹配不到 `gui/menu.mo` —— 收窄写错会静默漏掉一层。

        这正是 `_scope_pattern` 存在的理由：scanner 用 fnmatch，`*` 跨 `/`，
        而 `**/` 开头有一条剥前缀的兜底，所以正解是 `gui/*.mo`。
        """
        game = self._tree()
        config = self.service()._overridden_config(game / "gui", "1")
        self.assertIn("gui/menu.mo", self._scanned(config))

    def test_the_original_file_type_filter_survives(self) -> None:
        game = self._tree()
        (game / "gui" / "readme.txt").write_bytes(b"")
        config = self.service()._overridden_config(game / "gui", "1")
        self.assertNotIn("gui/readme.txt", self._scanned(config))

    def test_selecting_the_root_itself_changes_nothing(self) -> None:
        game = self._tree()
        service = self.service()
        config = service._overridden_config(game, "1")
        self.assertEqual(game, Path(config.paths.source))
        self.assertEqual(["**/*.mo"], config.resources.adapters[0].include)

    def test_a_directory_outside_every_root_becomes_its_own_source(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "menu.mo").write_bytes(b"")
        config = self.service()._overridden_config(outside, "1")
        self.assertEqual(outside, Path(config.paths.source))


class ResumeGateTests(_Case):
    """R15：中断态的运行必须能复用自己的 checkpoint。"""

    def _seed(self, run_id: str, *, status: str, task_id: str = "t1") -> Path:
        run_path = self.root / "workspace" / "runs" / run_id
        run_path.mkdir(parents=True)
        source = self.root / "game" / "menu.mo"
        source.write_bytes(b"")
        (run_path / "task-request.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": status,
                    "version": "1",
                    "mode": "preview",
                    "source_path": str(source),
                    "dotenv_path": None,
                }
            ),
            encoding="utf-8",
        )
        (run_path / "checkpoint.json").write_text("{}", encoding="utf-8")
        return run_path

    def _validate(self, service, run_path: Path):
        return service._validate_resume(
            run_path,
            version="1",
            mode=BuildMode.PREVIEW,
            source=self.root / "game" / "menu.mo",
            dotenv_path=None,
        )

    def test_a_failed_run_is_still_resumable(self) -> None:
        service = self.service()
        self.assertEqual(
            "t1", self._validate(service, self._seed("r1", status="failed"))
        )

    def test_a_killed_run_left_at_running_is_resumable(self) -> None:
        """这条就是 R15。修复前它抛 `is not a failed task`，
        checkpoint 里已经付过钱的译文只能整轮作废。"""
        service = self.service()
        self.assertEqual(
            "t1", self._validate(service, self._seed("r2", status="running"))
        )

    def test_a_queued_run_is_resumable_too(self) -> None:
        service = self.service()
        self.assertEqual(
            "t1", self._validate(service, self._seed("r3", status="queued"))
        )

    def test_a_completed_run_is_never_resumable(self) -> None:
        """成功的运行是不可变的产物，绝不允许被覆盖。"""
        service = self.service()
        with self.assertRaises(FileExistsError) as ctx:
            self._validate(service, self._seed("r4", status="completed"))
        self.assertIn("not resumable", str(ctx.exception))

    def test_a_task_still_live_in_this_process_is_refused(self) -> None:
        """`running` 有两种可能：真的被杀了，或者它此刻还在跑。

        后者不是中断而是并发重入 —— 两个任务同时写同一个 run 工作区和
        同一个 SQLite TM。放松状态闸门时**必须**同时补上这一条，
        否则 R15 的修复本身就变成一个新的数据竞争入口。
        """
        service = self.service()
        run_path = self._seed("r5", status="running", task_id="live-1")
        service._tasks["live-1"] = {"task_id": "live-1", "status": "running"}
        with self.assertRaises(FileExistsError) as ctx:
            self._validate(service, run_path)
        self.assertIn("仍在运行中", str(ctx.exception))

    def test_a_lock_held_by_this_process_beats_an_empty_task_table(self) -> None:
        """跨进程判据的核心：权威来源是 run 目录里的 owner 锁，不是内存表。

        两个 dashboard 指向同一 workspace 时，B 的 `_tasks` 是空的；只看内存表
        就会把 A 正在跑的 run 判为可恢复，两个进程同时写同一个 checkpoint 和
        同一个 SQLite TM。
        """
        service = self.service()
        run_path = self._seed("r10", status="running", task_id="ghost")
        service._claim_run(run_path)          # 模拟"另一个进程"持锁
        self.assertTrue((run_path / "owner.lock").exists())
        with self.assertRaises(FileExistsError):
            self._validate(service, run_path)

    def test_a_lock_from_a_dead_process_is_taken_over(self) -> None:
        service = self.service()
        run_path = self._seed("r11", status="running", task_id="ghost")
        (run_path / "owner.lock").write_text(
            json.dumps(
                {
                    "pid": 999999,          # 几乎不可能存在
                    "process_start": "0",
                    "host": __import__("socket").gethostname(),
                    "claimed_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual("ghost", self._validate(service, run_path))

    def test_an_unreadable_lock_is_treated_as_alive(self) -> None:
        """读不了的锁按「还活着」处理 —— 宁可多拒绝一次恢复。"""
        service = self.service()
        run_path = self._seed("r12", status="running", task_id="ghost")
        (run_path / "owner.lock").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self._validate(service, run_path)

    def test_a_lock_from_another_host_is_treated_as_alive(self) -> None:
        service = self.service()
        run_path = self._seed("r13", status="running", task_id="ghost")
        (run_path / "owner.lock").write_text(
            json.dumps(
                {
                    "pid": 1,
                    "process_start": "0",
                    "host": "some-other-machine",
                    "claimed_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(FileExistsError):
            self._validate(service, run_path)

    def test_claiming_a_run_twice_is_refused(self) -> None:
        """判定与占位合并成一次原子声明 —— O_CREAT|O_EXCL 是唯一可靠的原语。"""
        service = self.service()
        run_path = self.root / "workspace" / "runs" / "claim"
        service._claim_run(run_path)
        with self.assertRaises(FileExistsError):
            service._claim_run(run_path)

    def test_releasing_lets_the_next_claim_through(self) -> None:
        service = self.service()
        run_path = self.root / "workspace" / "runs" / "claim2"
        service._claim_run(run_path)
        service._release_run(run_path)
        service._claim_run(run_path)   # 不该抛
        self.assertTrue((run_path / "owner.lock").exists())

    def test_concurrent_submits_do_not_both_win(self) -> None:
        """两个并发 POST 曾经都被接受，同一个 run 工作区被跑了两遍。"""
        import threading

        service = self.service()
        run_path = self.root / "workspace" / "runs" / "race"
        barrier = threading.Barrier(4)
        outcomes = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            try:
                with service._lock:
                    service._claim_run(run_path)
                with lock:
                    outcomes.append("ok")
            except FileExistsError:
                with lock:
                    outcomes.append("refused")

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"), outcomes)
        self.assertEqual(3, outcomes.count("refused"), outcomes)

    def test_a_stale_task_id_from_a_previous_process_does_not_block(self) -> None:
        # 面板重启后 `_tasks` 是空的 —— 那正是「真的被杀了」的判据。
        service = self.service()
        run_path = self._seed("r6", status="running", task_id="ghost")
        self.assertEqual("ghost", self._validate(service, run_path))

    def test_a_resumable_run_without_a_checkpoint_is_refused(self) -> None:
        service = self.service()
        run_path = self._seed("r7", status="running")
        (run_path / "checkpoint.json").unlink()
        with self.assertRaises(FileExistsError) as ctx:
            self._validate(service, run_path)
        self.assertIn("no checkpoint", str(ctx.exception))

    def test_release_runs_are_immutable_regardless_of_status(self) -> None:
        service = self.service()
        run_path = self._seed("r8", status="running")
        with self.assertRaises(FileExistsError) as ctx:
            service._validate_resume(
                run_path,
                version="1",
                mode=BuildMode.RELEASE,
                source=self.root / "game" / "menu.mo",
                dotenv_path=None,
            )
        self.assertIn("immutable", str(ctx.exception))

    def test_different_parameters_still_require_a_new_run_id(self) -> None:
        """放松状态闸门不等于放松参数一致性 —— 换了 version 就是另一次运行。"""
        service = self.service()
        run_path = self._seed("r9", status="running")
        with self.assertRaises(ValueError) as ctx:
            service._validate_resume(
                run_path,
                version="2",
                mode=BuildMode.PREVIEW,
                source=self.root / "game" / "menu.mo",
                dotenv_path=None,
            )
        self.assertIn("differ", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class OwnerLockIsNotAnArtifactTests(_Case):
    def test_the_lock_is_hidden_from_the_run_file_list(self) -> None:
        """owner.lock 是运行内部文件，不该出现在面板的「本次产物」里。"""
        from localizer.web.collector import _HIDDEN_RUN_FILES
        from localizer.web.tasks import _OWNER_LOCK

        self.assertIn(_OWNER_LOCK, _HIDDEN_RUN_FILES)


class RebuildOwnerLockTests(_Case):
    class _Executor:
        def __init__(self, error=None) -> None:
            self.error = error
            self.calls = []

        def submit(self, *args):
            if self.error is not None:
                raise self.error
            self.calls.append(args)
            return object()

    def _service_with_parent(self):
        service = self.service()
        original = service._executor
        original.shutdown(wait=True)
        service._executor = self._Executor()
        parent = self.root / "workspace" / "runs" / "parent"
        parent.mkdir(parents=True)
        (parent / "task-request.json").write_text(
            json.dumps(
                {
                    "version": "1",
                    "source_path": str(self.root / "game"),
                    "dotenv_path": None,
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        return service

    def test_rebuild_claims_the_child_before_queueing_it(self) -> None:
        service = self._service_with_parent()
        task = service.submit_rebuild(
            {"parent_run_id": "parent", "run_id": "child", "mode": "preview"}
        )
        child = self.root / "workspace" / "runs" / "child"
        self.assertEqual("child", task["run_id"])
        self.assertTrue((child / "owner.lock").is_file())
        self.assertEqual(1, len(service._executor.calls))

    def test_snapshot_write_failure_releases_the_child(self) -> None:
        service = self._service_with_parent()
        child = self.root / "workspace" / "runs" / "write-failed"
        with mock.patch.object(AtomicIO, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                service.submit_rebuild(
                    {
                        "parent_run_id": "parent",
                        "run_id": "write-failed",
                        "mode": "preview",
                    }
                )
        self.assertFalse((child / "owner.lock").exists())
        self.assertEqual([], service.list_tasks())

    def test_executor_rejection_releases_the_child(self) -> None:
        service = self._service_with_parent()
        service._executor = self._Executor(RuntimeError("executor stopped"))
        child = self.root / "workspace" / "runs" / "queue-failed"
        with self.assertRaises(RuntimeError):
            service.submit_rebuild(
                {
                    "parent_run_id": "parent",
                    "run_id": "queue-failed",
                    "mode": "preview",
                }
            )
        self.assertFalse((child / "owner.lock").exists())
        self.assertEqual([], service.list_tasks())
