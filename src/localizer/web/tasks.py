"""受控的本地任务启动与无凭据预设存储。

这不是本地审核系统。它只把一次运行的 version/source/mode 覆盖挂到既有
ProjectRunner 上；所有长期凭据和压缩密码仍只能由 project.yaml 引用环境变量。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from localizer.application.local_build import BuildMode
from localizer.application.artifact import ReleaseBundle
from localizer.application.publish import PublishOrchestrator
from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.application.project_runner import ProjectRunner, StaleFormalEntryError
from localizer.application.quality_gate import QualityGateError
from localizer.config.models import ProjectConfig
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.infrastructure.dotenv import temporary_dotenv
from localizer.infrastructure.workspace import validate_run_id


_RELEASE_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# 可以复用既有 run 工作区与 checkpoint 的前任状态（R15）。
#
# 原来只认 `failed`。但**进程被杀**（Ctrl-C、断电、OOM、面板重启）留下的快照
# 停在 `running` 或 `queued`，永远走不进恢复路径 —— checkpoint 里那些已经付过
# 钱的译文只能整轮作废。中断态同样应当可恢复；「它是不是真的不在跑了」由
# `_is_live` 单独判断，而不是靠状态字面量猜。
_INTERRUPTED_STATES = frozenset({"queued", "running"})
_RESUMABLE_STATES = frozenset({"failed", "waiting_confirmation"}) | _INTERRUPTED_STATES

# 进程身份的兜底令牌。同一进程内恒定，进程间几乎必然不同。
_FALLBACK_PROCESS_TOKEN = f"nostart-{os.getpid()}-{uuid.uuid4().hex}"


def _model_data(config: ProjectConfig) -> dict:
    return config.model_dump() if hasattr(config, "model_dump") else config.dict()


# 运行工作区的归属锁。它是运行内部文件，不是产物 ——
# `collector._HIDDEN_RUN_FILES` 把它挡在面板的文件列表之外。
_OWNER_LOCK = "owner.lock"


def _process_start_time() -> str:
    """本进程的启动时刻，用来把「pid 被复用」和「pid 还是我」区分开。

    拿不到就退回一个进程内唯一的随机值：那样跨进程判据退化成「不是我写的锁
    就当它还活着」，**偏保守**（可能多拒绝一次恢复），而不是偏危险。
    """
    try:
        import psutil  # type: ignore
    except Exception:
        pass
    else:  # pragma: no cover —— 可选依赖，CI 不装
        try:
            return str(psutil.Process(os.getpid()).create_time())
        except Exception:
            pass
    return _FALLBACK_PROCESS_TOKEN


def _contains(root: Path, candidate: Path) -> bool:
    """candidate 是否落在 root 之内（含 root 自身）。"""
    try:
        Path(candidate).relative_to(root)
    except ValueError:
        return False
    return True


def _scope_pattern(relative: str, pattern: str) -> str:
    """把一条 include 模式限制到某棵子树里，同时保留它原本的文件类型过滤。

    `ResourceScanner._matches` 用 `fnmatch`：`*` 会跨 `/`，而以 `**/` 开头的
    模式还有一条「剥掉前缀再试一次」的兜底。所以 `**/*.mo` 等价于 `*.mo`，
    加上前缀就成了 `gui/*.mo` —— 它同时匹配 `gui/menu.mo` 和 `gui/sub/x.mo`。

    **不要**写成 `gui/**/*.mo`：那个模式匹配不到 `gui/menu.mo`（实测），
    选中一个子目录会漏掉它正下方的全部文件。
    """
    cleaned = pattern.replace("\\", "/")
    if cleaned.startswith("**/"):
        cleaned = cleaned[3:]
    if cleaned == "**":
        cleaned = "*"
    return f"{relative}/{cleaned}"


def _validate_version(value: object) -> str:
    version = str(value or "").strip()
    if not _RELEASE_VALUE_RE.fullmatch(version):
        raise ValueError(
            "version must be 1-64 path-safe characters: letters, digits, dot, dash or underscore"
        )
    if re.match(r"^[vV]\d", version):
        raise ValueError("version must not start with 'v'; release naming adds it")
    return version


def _validate_mode(value: object) -> BuildMode:
    try:
        return BuildMode(str(value or "preview"))
    except ValueError as exc:
        raise ValueError("mode must be preview or release") from exc


class TaskProfileStore:
    """一个项目一个本地 JSON 文件；只存非敏感任务参数。"""

    def __init__(self, workspace: Path) -> None:
        self.path = Path(workspace).resolve() / "web" / "task-profiles.json"
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            import json

            raw = json.loads(AtomicIO.read_text(self.path))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(
                "task profile store is unreadable; refusing to overwrite"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("unsupported task profile store schema")
        profiles = raw.get("profiles")
        if not isinstance(profiles, list) or not all(
            isinstance(item, dict) for item in profiles
        ):
            raise ValueError("task profile store contains invalid profiles")
        # schema v1 的旧预设没有 variant；API 统一补成空字符串，页面可以在
        # 单目录项目和升级后的多变体项目之间使用同一份渲染逻辑。
        return [{**item, "variant": str(item.get("variant") or "")} for item in profiles]

    def save(self, payload: Mapping[str, Any]) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ValueError("profile name must be 1-100 characters")
        profile_id = str(payload.get("id") or self._slug(name)).strip()
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("profile id must be a path-safe identifier")
        source = Path(str(payload.get("source_path") or "")).expanduser().resolve(
            strict=True
        )
        if not source.is_file() and not source.is_dir():
            raise ValueError("source_path must be an existing file or directory")
        profile = {
            "id": profile_id,
            "name": name,
            "version": _validate_version(payload.get("version")),
            "source_path": str(source),
            "mode": _validate_mode(payload.get("mode")).value,
            "dotenv_path": self._dotenv_path(payload.get("dotenv_path")),
            "variant": str(payload.get("variant") or "").strip(),
        }
        with self._lock:
            profiles = self.list()
            profiles = [item for item in profiles if item.get("id") != profile_id]
            profiles.append(profile)
            profiles.sort(key=lambda item: (str(item.get("name", "")), item["id"]))
            AtomicIO.write_json(
                self.path, {"schema_version": 1, "profiles": profiles}
            )
        return profile

    @staticmethod
    def _slug(name: str) -> str:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
        return (candidate[:56] or "profile") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def _dotenv_path(value: object) -> Optional[str]:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("dotenv_path must be an existing file")
        return str(path)


class TaskService:
    """串行执行本地任务，避免两个运行并发写同一 SQLite TM。"""

    def __init__(
        self,
        config: ProjectConfig,
        *,
        runner_factory: Callable[[ProjectConfig], ProjectRunner] = ProjectRunner,
        executor: Optional[ThreadPoolExecutor] = None,
        profiles: Optional[TaskProfileStore] = None,
        is_busy=None,
        maintenance_lock=None,
    ) -> None:
        self.config = config
        self.variant = config.active_variant
        self.profiles = profiles or TaskProfileStore(config.paths.workspace)
        self.runner_factory = runner_factory
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="localizer-task"
        )
        self._tasks: Dict[str, dict] = {}
        self._preflights: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._is_busy = is_busy or self._local_busy
        self._maintenance_lock = maintenance_lock or threading.RLock()

    def list_profiles(self) -> list[dict]:
        return self.profiles.list()

    def save_profile(self, payload: Mapping[str, Any]) -> dict:
        variant = self._validate_variant(payload)
        return self.profiles.save({**payload, "variant": variant})

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return sorted(
                (dict(item) for item in self._tasks.values()),
                key=lambda item: item["created_at"],
                reverse=True,
            )

    def task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            value = self._tasks.get(task_id)
            return dict(value) if value else None

    def preflight(self, payload: Mapping[str, Any]) -> dict:
        """只读扫描资源和 TM，不调用 Provider、不创建 run 工作区。"""
        variant = self._validate_variant(payload)
        version, mode, source, dotenv_path = self._validated_request(payload)
        config = self._overridden_config(source, version)
        dotenv_files = [Path(dotenv_path)] if dotenv_path else []
        with temporary_dotenv(dotenv_files, override=True):
            runner = self.runner_factory(config)
            plan = runner.plan()
            runtime = None
            prepare_runtime = getattr(runner, "prepare_translation_runtime", None)
            if callable(prepare_runtime) and plan.as_dict().get("pending_units", 0) > 0:
                counter = prepare_runtime()
                resolved_source = getattr(counter, "resolved_source", None)
                runtime = {
                    "tokenizer_ready": True,
                    "tokenizer_source": (
                        str(resolved_source) if resolved_source is not None else None
                    ),
                }
        preflight_id = uuid.uuid4().hex
        result = {
            "preflight_id": preflight_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": version,
            "source_path": str(source),
            "source_kind": "file" if source.is_file() else "directory",
            "dotenv_path": dotenv_path,
            "mode": mode.value,
            "variant": variant,
            **plan.as_dict(),
        }
        result["stale_formal"] = self._stale_formal_preview(config, plan)
        if runtime is not None:
            result["runtime"] = runtime
        with self._lock:
            self._preflights[preflight_id] = result
            # 预检是短生命周期页面状态，限制内存中最多保留最近 20 份。
            stale = sorted(
                self._preflights.values(), key=lambda item: item["created_at"]
            )[:-20]
            for item in stale:
                self._preflights.pop(item["preflight_id"], None)
        return dict(result)

    def confirm_stale(self, payload: Mapping[str, Any]) -> dict:
        """人工确认资源更新后失效的 formal 记录，并按需恢复原 checkpoint。"""
        variant = self._validate_variant(payload)
        run_id = str(payload.get("run_id") or "").strip()
        preflight_id = str(payload.get("preflight_id") or "").strip()
        if bool(run_id) == bool(preflight_id):
            raise ValueError("confirm-stale requires exactly one of run_id or preflight_id")

        with self._maintenance_lock:
            if self._is_busy():
                raise ValueError("有任务正在运行；请等待结束后再修改共享 TM")
            if run_id:
                validate_run_id(run_id)
                request_path = (
                    self.config.paths.workspace / "runs" / run_id / "task-request.json"
                )
                previous = json.loads(AtomicIO.read_text(request_path))
                if not isinstance(previous, Mapping):
                    raise ValueError("task snapshot must be a JSON object")
                if previous.get("status") not in {"failed", "waiting_confirmation"}:
                    raise ValueError("run is not waiting for stale formal confirmation")
                error = previous.get("error") or {}
                if not isinstance(error, Mapping) or error.get("type") != "StaleFormalEntryError":
                    raise ValueError("run is not blocked by stale formal entries")
                self._validate_variant(previous)
                version, mode, source, dotenv_path = self._validated_request(previous)
                config = self._overridden_config(source, version)
            else:
                with self._lock:
                    preflight = self._preflights.get(preflight_id)
                if preflight is None:
                    raise ValueError("unknown or expired preflight_id; run preflight again")
                self._validate_variant(preflight)
                version, mode, source, dotenv_path = self._validated_request(preflight)
                config = self._overridden_config(source, version)

            dotenv_files = [Path(dotenv_path)] if dotenv_path else []
            with temporary_dotenv(dotenv_files, override=True):
                plan = self.runner_factory(config).plan()
            if preflight_id and plan.fingerprint != preflight["plan_fingerprint"]:
                raise ValueError(
                    "preflight is stale because source, TM, rules, glossary, Prompt or "
                    "configuration changed; run preflight again"
                )
            preview = self._stale_formal_preview(config, plan)
            expected = set(
                (previous.get("confirmation") or {}).get("identities") or ()
            ) if run_id else set(
                (preflight.get("stale_formal") or {}).get("identities") or ()
            )
            actual = set(preview["identities"])
            if expected and actual != expected:
                raise ValueError("stale formal candidates changed; inspect and confirm again")

            backup_path = None
            removed = 0
            if actual:
                backup_path = self._backup_tm(config, run_id or preflight_id)
                with SQLiteTranslationMemory(config.tm.database) as tm:
                    removed = tm.retire_stale_formal_entries(
                        self._plan_entries(plan), expected_identities=actual
                    )

            audit = {
                "schema_version": 1,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "variant": variant,
                "run_id": run_id or None,
                "preflight_id": preflight_id or None,
                "backup": str(backup_path) if backup_path else None,
                "removed": removed,
                "entries": preview["entries"],
            }
            if run_id:
                AtomicIO.write_json(
                    self.config.paths.workspace
                    / "runs"
                    / run_id
                    / "review"
                    / "stale-formal-retirement.json",
                    audit,
                )
            else:
                AtomicIO.write_json(
                    self.config.paths.workspace
                    / "web"
                    / "stale-formal-retirements"
                    / f"{preflight_id}.json",
                    audit,
                )
                with self._lock:
                    self._preflights.pop(preflight_id, None)

            response = dict(audit)
            if run_id:
                response["task"] = self.submit(
                    {
                        "version": version,
                        "source_path": str(source),
                        "mode": mode.value,
                        "run_id": run_id,
                        "dotenv_path": dotenv_path,
                        "variant": variant,
                    }
                )
            return response

    def stale_for_run(self, run_id: str) -> dict:
        """为旧版写成 failed 的 StaleFormalEntryError 即时重建确认清单。"""
        validate_run_id(run_id)
        request_path = (
            self.config.paths.workspace / "runs" / run_id / "task-request.json"
        )
        previous = json.loads(AtomicIO.read_text(request_path))
        if not isinstance(previous, Mapping):
            raise ValueError("task snapshot must be a JSON object")
        if previous.get("status") not in {"failed", "waiting_confirmation"}:
            raise ValueError("run is not waiting for stale formal confirmation")
        error = previous.get("error") or {}
        if not isinstance(error, Mapping) or error.get("type") != "StaleFormalEntryError":
            raise ValueError("run is not blocked by stale formal entries")
        self._validate_variant(previous)
        version, _mode, source, dotenv_path = self._validated_request(previous)
        config = self._overridden_config(source, version)
        dotenv_files = [Path(dotenv_path)] if dotenv_path else []
        with temporary_dotenv(dotenv_files, override=True):
            plan = self.runner_factory(config).plan()
        return self._stale_formal_preview(config, plan)

    def _local_busy(self) -> bool:
        return any(
            item.get("status") in {"queued", "running"}
            for item in self.list_tasks()
        )

    @staticmethod
    def _plan_entries(plan) -> list[TMEntry]:
        return [
            TMEntry(
                stable_identity=unit.stable_identity,
                project_id=unit.project_id,
                adapter_id=unit.adapter_id,
                relative_path=unit.relative_path,
                logical_key=unit.logical_key,
                source_text=unit.source_text,
                source_fingerprint=unit.source_fingerprint,
                translation=unit.translation or "",
                origin="machine",
                review_state="unreviewed",
                match_scope="coordinate",
            )
            for resource in getattr(plan, "resources", ())
            for unit in resource.units
        ]

    def _stale_formal_preview(self, config: ProjectConfig, plan) -> dict:
        candidates = self._plan_entries(plan)
        # 预检承诺只读；新项目尚无 TM 时也不能顺手创建目录/数据库。
        with SQLiteTranslationMemory(config.tm.database, read_only=True) as tm:
            identities = tm.stale_formal_identities(candidates)
            rows = tm.rows_for(identities)
        current = {entry.stable_identity: entry for entry in candidates}
        entries = []
        for identity in identities:
            old = rows.get(identity)
            new = current.get(identity)
            if old is None or new is None:
                continue
            entries.append(
                {
                    "stable_identity": identity,
                    "relative_path": new.relative_path,
                    "logical_key": new.logical_key,
                    "origin": old.get("origin"),
                    "review_state": old.get("review_state"),
                    "old_source": old.get("source_text"),
                    "new_source": new.source_text,
                    "old_translation": old.get("translation"),
                }
            )
        return {
            "count": len(entries),
            "requires_confirmation": bool(entries),
            "identities": [entry["stable_identity"] for entry in entries],
            "entries": entries,
        }

    @staticmethod
    def _backup_tm(config: ProjectConfig, label: str) -> Path:
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")[:48]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        destination = (
            config.tm.database.parent
            / "backups"
            / f"tm-before-stale-confirm-{safe_label}-{stamp}.sqlite3"
        ).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"backup already exists: {destination}")
        source = sqlite3.connect(str(config.tm.database))
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def submit(self, payload: Mapping[str, Any]) -> dict:
        # 与 stale formal 维护共用一把锁：确认页完成“复核→备份→退休→恢复”期间，
        # 不能从另一个请求缝隙插入新任务。RLock 允许确认流程在锁内恢复原任务。
        with self._maintenance_lock:
            return self._submit(payload)

    def _submit(self, payload: Mapping[str, Any]) -> dict:
        variant = self._validate_variant(payload)
        version, mode, source, dotenv_path = self._validated_request(payload)
        preflight_id = str(payload.get("preflight_id") or "").strip() or None
        expected_plan_fingerprint = None
        if preflight_id is not None:
            with self._lock:
                preflight = self._preflights.get(preflight_id)
            if preflight is None:
                raise ValueError("unknown or expired preflight_id; run preflight again")
            expected = (version, str(source), mode.value, dotenv_path, variant)
            actual = (
                preflight["version"],
                preflight["source_path"],
                preflight["mode"],
                preflight["dotenv_path"],
                preflight.get("variant", self.variant),
            )
            if expected != actual:
                raise ValueError("task parameters changed after preflight; run preflight again")
            expected_plan_fingerprint = preflight["plan_fingerprint"]
        run_id = str(payload.get("run_id") or self._default_run_id()).strip()
        validate_run_id(run_id)
        run_path = self.config.paths.workspace / "runs" / run_id
        # 判定与占位必须是**一次**原子声明。
        #
        # `run_path.exists()` → `_validate_resume` → 写 task-request.json 之间
        # 原本没有任何锁，而面板用的是 ThreadingHTTPServer：两个并发 POST 会
        # 同时读到同一份"已死"快照、同时通过判定、同时排队，同一个 run 工作区
        # 被跑两遍，后完成的覆盖前一次的终态。实测两次 submit 都被接受。
        #
        # 跨进程更没有东西可依赖：`_is_live` 只查本进程的 `_tasks`，两个
        # dashboard 指向同一 workspace 时，B 读到 A 正在跑的 run 会判为可恢复。
        # 所以归属声明落在文件系统上（`O_CREAT|O_EXCL`），而不是内存里。
        with self._lock:
            resumed_from_task_id = None
            if run_path.exists():
                resumed_from_task_id = self._validate_resume(
                    run_path,
                    version=version,
                    mode=mode,
                    source=source,
                    dotenv_path=dotenv_path,
                )
            self._claim_run(run_path)
        task_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        task = {
            "task_id": task_id,
            "run_id": run_id,
            "version": version,
            "source_path": str(source),
            "source_kind": "file" if source.is_file() else "directory",
            "dotenv_path": dotenv_path,
            "preflight_id": preflight_id,
            "expected_plan_fingerprint": expected_plan_fingerprint,
            "mode": mode.value,
            "variant": variant,
            "status": "queued",
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "resumed_from_task_id": resumed_from_task_id,
        }
        with self._lock:
            self._tasks[task_id] = task
        try:
            AtomicIO.write_json(run_path / "task-request.json", task)
        except Exception:
            with self._lock:
                self._tasks.pop(task_id, None)
            self._release_run(run_path)
            raise
        try:
            self._executor.submit(
                self._run,
                task_id,
                source,
                version,
                mode,
                run_id,
                dotenv_path,
                expected_plan_fingerprint,
            )
        except Exception:
            with self._lock:
                self._tasks.pop(task_id, None)
            self._release_run(run_path)
            raise
        return dict(task)

    def submit_rebuild(self, payload: Mapping[str, Any]) -> dict:
        with self._maintenance_lock:
            return self._submit_rebuild(payload)

    def _submit_rebuild(self, payload: Mapping[str, Any]) -> dict:
        """基于选中的父运行创建不可变子运行。

        WebUI 不复制 ``ProjectRunner.rebuild_from_run`` 的兼容性判断。这里只负责
        恢复父任务当时的动态 version/source/dotenv 覆盖、建立任务快照并排队；
        源文指纹与 checkpoint 复用安全性仍由 Application Service 判定。
        """
        variant = self._validate_variant(payload)
        parent_run_id = str(payload.get("parent_run_id") or "").strip()
        validate_run_id(parent_run_id)
        mode = _validate_mode(payload.get("mode"))
        run_id = str(payload.get("run_id") or self._default_run_id()).strip()
        validate_run_id(run_id)
        if run_id == parent_run_id:
            raise ValueError("rebuild must create a new run; parent runs are immutable")

        parent_path = self.config.paths.workspace / "runs" / parent_run_id
        if not parent_path.is_dir():
            raise FileNotFoundError(f"parent run does not exist: {parent_run_id}")
        run_path = self.config.paths.workspace / "runs" / run_id

        inherited_version, source, dotenv_path = self._parent_request(parent_path)
        version = _validate_version(payload.get("version") or inherited_version)
        # rebuild 与普通 submit 必须走同一条原子归属路径。只做 exists() 检查会让
        # 两个 Dashboard 进程同时通过并写同一份 checkpoint/TM。
        with self._lock:
            if run_path.exists():
                raise FileExistsError(f"child run_id already exists: {run_id}")
            self._claim_run(run_path)
        task_id = uuid.uuid4().hex
        task = {
            "task_id": task_id,
            "kind": "rebuild",
            "parent_run_id": parent_run_id,
            "run_id": run_id,
            "version": version,
            "source_path": str(source),
            "source_kind": "file" if source.is_file() else "directory",
            "dotenv_path": dotenv_path,
            "preflight_id": None,
            "expected_plan_fingerprint": None,
            "mode": mode.value,
            "variant": variant,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "resumed_from_task_id": None,
        }
        with self._lock:
            self._tasks[task_id] = task
        try:
            AtomicIO.write_json(run_path / "task-request.json", task)
        except Exception:
            with self._lock:
                self._tasks.pop(task_id, None)
            self._release_run(run_path)
            raise
        try:
            self._executor.submit(
                self._run,
                task_id,
                source,
                version,
                mode,
                run_id,
                dotenv_path,
                None,
                parent_run_id,
            )
        except Exception:
            with self._lock:
                self._tasks.pop(task_id, None)
            self._release_run(run_path)
            raise
        return dict(task)

    def submit_publish(self, payload: Mapping[str, Any]) -> dict:
        with self._maintenance_lock:
            return self._submit_publish(payload)

    def _submit_publish(self, payload: Mapping[str, Any]) -> dict:
        """Queue an explicit publish for an existing, verified release run."""
        variant = self._validate_variant(payload)
        run_id = str(payload.get("run_id") or "").strip()
        validate_run_id(run_id)
        if not self.config.publish.targets:
            raise ValueError("config.publish.targets is empty; nothing to publish")
        release_dir = self.config.paths.output / "release" / run_id
        manifests = sorted(release_dir.glob("*.manifest.json"))
        if len(manifests) != 1:
            raise ValueError(
                f"release run must contain exactly one internal manifest: {release_dir}"
            )
        bundle = ReleaseBundle.load(manifests[0])
        bundle.verify()
        request_path = self.config.paths.workspace / "runs" / run_id / "task-request.json"
        dotenv_path = None
        if request_path.is_file():
            raw = json.loads(AtomicIO.read_text(request_path))
            if isinstance(raw, Mapping):
                dotenv_path = TaskProfileStore._dotenv_path(raw.get("dotenv_path"))
        with self._lock:
            if any(
                item.get("kind") == "publish"
                and item.get("run_id") == run_id
                and item.get("status") in {"queued", "running"}
                for item in self._tasks.values()
            ):
                raise ValueError(f"publish is already queued or running for {run_id}")
        task_id = uuid.uuid4().hex
        task = {
            "task_id": task_id,
            "kind": "publish",
            "run_id": run_id,
            "manifest": str(manifests[0]),
            "dotenv_path": dotenv_path,
            "variant": variant,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._tasks[task_id] = task
        self._executor.submit(
            self._publish, task_id, run_id, manifests[0], dotenv_path
        )
        return dict(task)

    def _parent_request(self, parent_path: Path) -> tuple[str, Path, Optional[str]]:
        request_path = parent_path / "task-request.json"
        if request_path.is_file():
            try:
                previous = json.loads(AtomicIO.read_text(request_path))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"parent task request is unreadable: {request_path}"
                ) from exc
            if not isinstance(previous, Mapping):
                raise ValueError("parent task request must be a JSON object")
            version = _validate_version(previous.get("version"))
            source = Path(str(previous.get("source_path") or "")).expanduser().resolve(
                strict=True
            )
            if not source.is_file() and not source.is_dir():
                raise ValueError("parent source_path must be an existing file or directory")
            dotenv_path = TaskProfileStore._dotenv_path(previous.get("dotenv_path"))
            return version, source, dotenv_path

        # CLI 创建的父运行没有 WebUI 请求快照；这种情况下复用 Dashboard 当前
        # 已投影的项目配置。多变体项目在 server 构造时已经 for_variant()。
        if self.config.paths.source is None:
            raise ValueError(
                "parent run has no task-request.json and project has no active source"
            )
        source = Path(self.config.paths.source).resolve(strict=True)
        return _validate_version(self.config.project.game_version), source, None

    def _validate_resume(
        self,
        run_path: Path,
        *,
        version: str,
        mode: BuildMode,
        source: Path,
        dotenv_path: Optional[str],
    ) -> Optional[str]:
        """只有参数完全一致的 preview 才可以复用自己的持久化 checkpoint。

        R15：原来的闸门是 `status != "failed"` 就拒绝。但**进程被杀**（Ctrl-C、
        断电、OOM、面板重启）留下的快照停在 `running`，永远走不进这条路 ——
        checkpoint 里那些已经付过钱的译文只能整轮作废，换一个 run_id 重跑。
        中断态同样可恢复，前提是它确实已经不在跑：本进程仍持有该任务时，
        这不是中断而是并发重入，照旧拒绝。
        """
        if mode is not BuildMode.PREVIEW:
            raise FileExistsError(
                f"run_id already exists: {run_path.name}; release runs are immutable"
            )
        request_path = run_path / "task-request.json"
        checkpoint_path = run_path / "checkpoint.json"
        try:
            previous = json.loads(AtomicIO.read_text(request_path))
        except (OSError, json.JSONDecodeError) as exc:
            raise FileExistsError(
                f"run_id already exists and has no readable failed task: {run_path.name}"
            ) from exc
        if not isinstance(previous, Mapping):
            raise FileExistsError(
                f"run_id already exists and has no readable task snapshot: {run_path.name}"
            )
        status = previous.get("status")
        if status not in _RESUMABLE_STATES:
            raise FileExistsError(
                f"run_id already exists and is not resumable (status={status!r}): "
                f"{run_path.name}"
            )
        if status in _INTERRUPTED_STATES and self._is_live(previous, run_path):
            raise FileExistsError(
                f"run {run_path.name} 仍在运行中 —— 这不是中断，是并发重入。"
            )
        if not checkpoint_path.is_file():
            raise FileExistsError(
                f"run {run_path.name} has no checkpoint to resume"
            )
        try:
            previous_source = Path(str(previous.get("source_path"))).resolve(strict=True)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("failed task source_path is no longer valid") from exc
        expected = (
            str(previous.get("version")),
            str(previous.get("mode")),
            previous_source,
            previous.get("dotenv_path"),
            str(previous.get("variant") or self.variant),
        )
        actual = (version, mode.value, source, dotenv_path, self.variant)
        if expected != actual:
            raise ValueError(
                "failed run parameters differ from this request; use a new run_id"
            )
        previous_task_id = previous.get("task_id")
        return str(previous_task_id) if previous_task_id else None

    def _claim_run(self, run_path: Path) -> None:
        """在 run 目录里原子地声明归属。调用方已持 `self._lock`。

        `O_CREAT | O_EXCL` 是这里唯一可靠的原语：它把「检查」和「占位」压成
        一个不可分割的系统调用，同一台机器上无论多少线程、多少进程同时进来，
        只有一个能成功。
        """
        run_path.mkdir(parents=True, exist_ok=True)
        lock = run_path / _OWNER_LOCK
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "process_start": _process_start_time(),
                "host": socket.gethostname(),
                "claimed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = self._read_owner(lock)
            if self._owner_is_alive(owner):
                raise FileExistsError(
                    f"run {run_path.name} 已被另一个进程占用"
                    f"（pid={owner.get('pid')} host={owner.get('host')}）。"
                    f"两个进程同时跑同一个 run 会互相覆盖 checkpoint 和 TM。"
                )
            # owner 已经死了（面板被杀、机器重启）—— 接管它的锁。
            lock.unlink(missing_ok=True)
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(handle, payload.encode("utf-8"))
        finally:
            os.close(handle)

    def _release_run(self, run_path: Path) -> None:
        """任务结束时放掉归属。失败不抛 —— 锁没删掉的后果只是下次接管而已。"""
        try:
            (Path(run_path) / _OWNER_LOCK).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _read_owner(lock: Path) -> Dict[str, Any]:
        try:
            raw = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            # 读不了的锁按「还活着」处理：宁可多拒绝一次恢复，
            # 也不要让两个进程同时写同一个 checkpoint。
            return {"unreadable": True}
        return raw if isinstance(raw, Mapping) else {"unreadable": True}

    def _owner_is_alive(self, owner: Mapping[str, Any]) -> bool:
        """锁的主人还在吗？

        同机同进程 → 一定在（就是我们自己，那是并发重入）。
        同机异进程 → 看 pid 是否存在；pid 会被复用，所以还要比启动时刻。
        异机 → 无从判断，按活着处理（fail-closed）。
        """
        if owner.get("unreadable"):
            return True
        if str(owner.get("host") or "") != socket.gethostname():
            return True
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return True
        if pid == os.getpid():
            return str(owner.get("process_start")) == _process_start_time()
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        except Exception:  # pragma: no cover —— 平台差异
            return True
        # pid 存在。它可能是复用的新进程，但我们没法从外部读它的启动时刻，
        # 所以按活着处理 —— 误拒一次恢复远好过两个进程写同一个 checkpoint。
        return True

    def _is_live(self, previous: Mapping[str, Any], run_path: Path) -> bool:
        """这个 run 现在还有活着的主人吗？

        判据的**权威来源是 run 目录里的 owner 锁**，不是内存里的 `_tasks`：
        后者只回答得了「本进程知不知道它」，两个 dashboard 指向同一 workspace
        时，B 的 `_tasks` 是空的，于是把 A 正在跑的 run 判为可恢复，两个进程
        同时写同一个 checkpoint 和同一个 SQLite TM。

        锁不在（正常结束 / 从未被本版本写过）时退回内存表 —— 老工作区里没有
        owner 锁，不能因此把它们全判成「有人在跑」。
        """
        lock = Path(run_path) / _OWNER_LOCK
        if lock.exists():
            return self._owner_is_alive(self._read_owner(lock))
        task_id = previous.get("task_id")
        if not task_id:
            return False
        with self._lock:
            live = self._tasks.get(str(task_id))
        return bool(live and live.get("status") in _INTERRUPTED_STATES)

    def _run(
        self,
        task_id: str,
        source: Path,
        version: str,
        mode: BuildMode,
        run_id: str,
        dotenv_path: Optional[str],
        expected_plan_fingerprint: Optional[str],
        parent_run_id: Optional[str] = None,
    ) -> None:
        self._update(
            task_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        final_changes: Dict[str, Any]
        try:
            config = self._overridden_config(source, version)
            dotenv_files = [Path(dotenv_path)] if dotenv_path else []
            # 单次任务显式选择的文件具有任务内优先级；上下文退出后恢复启动
            # Dashboard 时的进程环境，因此不会污染下一次串行任务。
            with temporary_dotenv(dotenv_files, override=True):
                runner = self.runner_factory(config)
                if parent_run_id is not None:
                    result = runner.rebuild_from_run(
                        parent_run_id, mode=mode, run_id=run_id
                    )
                elif expected_plan_fingerprint is not None:
                    fresh_plan = runner.plan()
                    if fresh_plan.fingerprint != expected_plan_fingerprint:
                        raise RuntimeError(
                            "preflight is stale because source, TM, rules, glossary, "
                            "Prompt or configuration changed; run preflight again"
                        )
                    result = runner.run(mode=mode, run_id=run_id, plan=fresh_plan)
                else:
                    result = runner.run(mode=mode, run_id=run_id)
            final_changes = {
                # completed 仅表示执行链路结束；语言质量是否通过由 quality_gate
                # 独立表达，preview 可以 completed + quality_gate.failed。
                "status": "completed",
                "result": {
                    "extracted_units": result.extracted_units,
                    "tm_hits": result.tm_hits,
                    "machine_successes": result.machine_successes,
                    "failed_units": result.failed_units,
                    "quality_gate": {
                        "passed": result.build.quality_gate.passed,
                        "error_count": result.build.quality_gate.error_count,
                        "failed_unit_count": (
                            result.build.quality_gate.failed_unit_count
                        ),
                    },
                    "output_root": str(result.build.output_root),
                    "artifact": str(result.build.bundle.artifact)
                    if result.build.bundle
                    else None,
                    "manifest": str(result.build.bundle.manifest)
                    if result.build.bundle
                    else None,
                    "rebuild": result.rebuild.as_dict()
                    if getattr(result, "rebuild", None)
                    else None,
                },
            }
        except QualityGateError as exc:
            # QualityGate 拒绝是一次有结论的正常执行，不是基础设施/程序异常。
            # release 制品仍然不存在，质量结论则必须完整呈现。
            final_changes = {
                "status": "completed",
                "result": {
                    "extracted_units": None,
                    "tm_hits": None,
                    "machine_successes": None,
                    "failed_units": exc.result.failed_unit_count,
                    "quality_gate": {
                        "passed": False,
                        "error_count": exc.result.error_count,
                        "failed_unit_count": exc.result.failed_unit_count,
                    },
                    "output_root": str(
                        self.config.paths.output / mode.value / run_id
                    ),
                    "artifact": None,
                },
                "error": None,
            }
        except Exception as exc:
            if isinstance(exc, StaleFormalEntryError):
                try:
                    confirmation = self._stale_formal_preview(config, runner.plan())
                except Exception:
                    confirmation = {
                        "count": len(exc.identities),
                        "requires_confirmation": True,
                        "identities": list(exc.identities),
                        "entries": [],
                    }
                final_changes = {
                    "status": "waiting_confirmation",
                    "confirmation": confirmation,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            else:
                final_changes = {
                    "status": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
        finally:
            # 先原子落盘最终快照，再对查询端公开终态，避免 API 已显示 completed
            # 而磁盘仍停在 queued 的短暂竞态。
            run_path = self.config.paths.workspace / "runs" / run_id
            try:
                with self._lock:
                    snapshot = dict(self._tasks[task_id])
                snapshot.update(final_changes)
                snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
                AtomicIO.write_json(run_path / "task-request.json", snapshot)
                with self._lock:
                    self._tasks[task_id] = snapshot
            finally:
                # 即使终态快照因为磁盘/权限问题写不下，也不能让本进程永久持锁。
                self._release_run(run_path)

    def _publish(
        self,
        task_id: str,
        run_id: str,
        manifest: Path,
        dotenv_path: Optional[str],
    ) -> None:
        self._update(
            task_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            dotenv_files = [Path(dotenv_path)] if dotenv_path else []
            with temporary_dotenv(dotenv_files, override=True):
                results = PublishOrchestrator(
                    security=self.config.security
                ).publish(ReleaseBundle.load(manifest), self.config.publish)
            serialized = [item.to_dict() for item in results]
            changes = {
                "status": "completed",
                "result": {
                    "passed": all(item.succeeded for item in results),
                    "targets": serialized,
                    "manifest": str(manifest),
                },
                "error": None,
            }
        except Exception as exc:
            changes = {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        with self._lock:
            snapshot = dict(self._tasks[task_id])
        snapshot.update(changes)
        snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
        AtomicIO.write_json(
            self.config.paths.workspace / "runs" / run_id / "publish-result.json",
            snapshot,
        )
        with self._lock:
            self._tasks[task_id] = snapshot

    def _overridden_config(self, source: Path, version: str) -> ProjectConfig:
        """把 WebUI 选中的源路径投影成一份运行配置。

        不变量只有一条：**同一个文件，无论怎么选中，`relative_path` 必须一样。**

        `stable_identity` 含 `relative_path`，而 `relative_path` 是相对
        `paths.source` 算出来的。原实现无条件把 `paths.source` 改成选中项
        （单文件取 parent、目录取自身），于是嵌套布局下选中 `<root>/gui/menu.mo`
        或 `<root>/gui/`，`relative_path` 都会从 `gui/menu.mo` 塌成 `menu.mo` ——
        同一个词条换了身份：TM 全部命中失败（整轮重新付费），新译文又写在错误
        坐标上，而且**没有任何判据会报警**，因为两边各自都完全自洽。

        所以只要选中项还在当前激活的源根之内，就保持 `paths.source` 不变，
        只把 `include` 收窄。根之外才退回选中项自身 —— 那种情况下坐标体系
        本来就换了，不存在"漂移"一说。
        """
        data = _model_data(self.config)
        data["project"]["game_version"] = version
        root = self._enclosing_source_root(source)
        if root is None:
            # 任何已配置源根之外：坐标体系本就不同，按选中项自成一套。
            data["paths"]["source"] = source.parent if source.is_file() else source
            if source.is_file():
                for adapter in data["resources"]["adapters"]:
                    adapter["include"] = [source.name]
                    adapter["exclude"] = []
        else:
            data["paths"]["source"] = root
            relative = source.relative_to(root).as_posix()
            for adapter in data["resources"]["adapters"]:
                if source.is_file():
                    # 单文件：include 就是它相对源根的完整路径。
                    adapter["include"] = [relative]
                    adapter["exclude"] = []
                elif relative and relative != ".":
                    # 子目录：保留原来的文件类型过滤，只把它限制在这棵子树里。
                    adapter["include"] = [
                        _scope_pattern(relative, pattern)
                        for pattern in adapter["include"]
                    ]
                    # exclude 表达的是相对同一个（未改变的）源根的规则，照旧生效。
        overridden = (
            ProjectConfig.model_validate(data)
            if hasattr(ProjectConfig, "model_validate")
            else ProjectConfig.parse_obj(data)
        )
        # active_variant 是运行期 PrivateAttr，不参与 model_dump/parse；这里若不
        # 恢复，路径虽然仍在 pts 工作区，Runner/Manifest 的可观测变体却会变空。
        object.__setattr__(overridden, "_active_variant", self.variant)
        return overridden

    def _enclosing_source_root(self, source: Path) -> Optional[Path]:
        """选中项落在哪个已配置的源根里。

        **当前激活的根优先**，不按目录深度。多变体项目里 `paths.source` 已经被
        `for_variant()` 投影成激活变体的根；按深度挑会让 live 与 live/pts 这种
        嵌套布局出事：面板跑在 variant=live 时选中 live/pts 下的文件，会被判给
        pts 根，`relative_path` 比整目录跑法少一层 —— R14 换个形状又回来了。

        落在激活根之外、却属于另一个变体时**拒绝**而不是静默切换：那是两套
        不同的坐标，该由人明确指定 variant。
        """
        paths = self.config.paths
        active = paths.source
        if active is None and paths.sources:
            raise ValueError(
                "多变体项目必须先选定变体（for_variant）才能投影任务源路径："
                f"可选 {sorted(paths.sources)}。未选定时无法判断该用哪一套坐标。"
            )
        if active is not None:
            resolved = Path(active).resolve()
            if _contains(resolved, source):
                return resolved
        for name, candidate in (paths.sources or {}).items():
            if candidate is None:
                continue
            other = Path(candidate).resolve()
            if _contains(other, source):
                raise ValueError(
                    f"{source} 属于变体 {name!r}（{other}），但面板当前激活的源根是 "
                    f"{active}。两个变体是两套坐标；请用对应变体启动 Dashboard，"
                    f"不要让它静默按另一套坐标入库。"
                )
        return None

    @staticmethod
    def _validated_request(
        payload: Mapping[str, Any]
    ) -> tuple[str, BuildMode, Path, Optional[str]]:
        version = _validate_version(payload.get("version"))
        mode = _validate_mode(payload.get("mode"))
        source = Path(str(payload.get("source_path") or "")).expanduser().resolve(
            strict=True
        )
        if not source.is_file() and not source.is_dir():
            raise ValueError("source_path must be an existing file or directory")
        dotenv_path = TaskProfileStore._dotenv_path(payload.get("dotenv_path"))
        return version, mode, source, dotenv_path

    def _validate_variant(self, payload: Mapping[str, Any]) -> str:
        """确保请求体选择的资源变体与当前 TaskService 一致。"""
        raw = payload.get("variant")
        requested = (
            self.variant
            if raw is None or str(raw).strip() == ""
            else str(raw).strip()
        )
        if requested != self.variant:
            raise ValueError(
                f"request variant {requested!r} does not match active variant "
                f"{self.variant!r}"
            )
        return self.variant

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            self._tasks[task_id].update(changes)

    @staticmethod
    def _default_run_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-")
        return timestamp + uuid.uuid4().hex[:6]

    def shutdown(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=False)
