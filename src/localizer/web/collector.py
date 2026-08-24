"""把磁盘上的运行状态收集成可 JSON 序列化的结构。

所有方法都是只读的：不创建目录、不写文件、不改数据库。任何缺失的产物都降级为
`available: false` 加一句原因，而不是抛异常——面板必须能在流水线跑到一半、
或者游戏资源目录根本不存在的机器上打开。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from localizer.config.models import ProjectConfig
from localizer.infrastructure.atomic_io import AtomicIO

# 展示用的流水线阶段。RunStatus 是运行态枚举，这里是它在面板上的固定骨架：
# 即使某个阶段还没跑到，也要显示出来，让人知道整条链路长什么样。
PIPELINE = (
    ("scan", "资源扫描", "只读遍历游戏资源，产出扫描清单", "M1"),
    ("extract", "词条提取", "Adapter 把资源解析为标准词条", "M2"),
    ("tm_lookup", "TM 命中", "坐标精确命中 + 已审核全局命中", "M3"),
    ("translate", "模型翻译", "未命中词条分批送 Provider，含缩批与断点", "M3"),
    ("qa", "QA 校验", "占位符、术语、源语言残留、同源异译", "M4"),
    ("gate", "QualityGate", "release 零容忍；preview 只记录不晋升", "M4"),
    ("build", "构建制品", "回编译资源 + ZIP + Manifest", "M4"),
    ("publish", "发布", "Local / GitHub Release / R2", "M6"),
)

# QA 记录里出现过的 code -> 中文说明。未知 code 原样显示。
QA_CODE_LABELS = {
    "placeholder_mismatch": "占位符集合与源文不一致",
    "untranslated": "译文与源文完全相同",
    "invalid_control_character": "含 NUL 控制字符",
    "glossary_violation": "缺少已审核术语的标准译名",
    "same_source_inconsistency": "同一源文在本次运行内有多个译法",
    "source_language_residue": "未获规则允许的源语言残留",
    "placeholder_variant_residue": "占位符 token 以变体形式残留在译文里",
    "empty_translation": "译文为空",
}

_HIDDEN_RUN_FILES = {"qa-review-index.json", "owner.lock"}


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _read_json(path: Path, *, attempts: int = 10, delay: float = 0.02) -> Optional[Any]:
    """读 JSON，对「正在被原子替换」这种瞬时窗口做重试。"""
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return json.loads(AtomicIO.read_text(path))
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    del last_error
    return None


def _batch_size(batch: Any) -> int:
    size = batch.get("size")
    if isinstance(size, int):
        return size
    return len(batch.get("identities") or [])


def _missing(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


@dataclass(frozen=True)
class RunRef:
    run_id: str
    workspace_dir: Optional[Path]
    preview_dir: Optional[Path]
    release_dir: Optional[Path]

    @property
    def modes(self) -> List[str]:
        modes = []
        if self.preview_dir is not None:
            modes.append("preview")
        if self.release_dir is not None:
            modes.append("release")
        return modes


class DashboardCollector:
    """从 ProjectConfig 指向的目录里读取运行现场。"""

    def __init__(self, config: ProjectConfig, config_path: Path, repo_root: Path) -> None:
        self.config = config
        self.config_path = Path(config_path).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.workspace = Path(config.paths.workspace)
        self.output = Path(config.paths.output)

    def overview(self) -> Dict[str, Any]:
        return {
            "project": {
                "id": self.config.project.id,
                "name": self.config.project.name,
                "game_version": self.config.project.game_version,
                "source_locale": self.config.languages.source,
                "target_locale": self.config.languages.target,
                "workflow_mode": self.config.workflow.mode,
                "release_channel": self.config.build.release_channel,
                "release_variant": self.config.build.variant or "",
                "release_env": self.config.build.compatibility_metadata.env or "",
                "config_path": str(self.config_path),
                "active_variant": self.config.active_variant,
                "variants": self._variants(),
            },
            "paths": {
                "source": self._path_status(self.config.paths.source),
                "workspace": self._path_status(self.workspace),
                "output": self._path_status(self.output),
                "tm_database": self._path_status(self.config.tm.database),
                "cache_root": self._path_status(self.config.cache.root),
                "tokenizers": self._path_status(self.config.cache.tokenizers),
            },
            "provider": {
                "type": self.config.provider.type,
                "model": self.config.provider.model,
                "base_url": self.config.provider.base_url,
                "api_key_env": self.config.provider.api_key_env,
                "concurrency": self.config.provider.concurrency,
                "max_output_tokens": self.config.provider.max_output_tokens,
                "context_window": self.config.provider.context_window,
                "custom_parameter_keys": sorted(self.config.provider.custom_parameters),
                "tokenizer": (
                    {
                        "type": self.config.provider.tokenizer.type,
                        "model": self.config.provider.tokenizer.model,
                        "revision": self.config.provider.tokenizer.revision,
                        "local_files_only": self.config.provider.tokenizer.local_files_only,
                        "cache_dir": str(self.config.cache.tokenizers),
                    }
                    if self.config.provider.tokenizer is not None
                    else None
                ),
            },
            "environment": {
                "auto_discover": self.config.environment.auto_discover,
                "override_existing": self.config.environment.override_existing,
                "dotenv_files": [str(path) for path in self.config.environment.dotenv_files],
            },
            "publish_targets": [
                {"type": target.type, "destination": str(target.destination or "")}
                for target in self.config.publish.targets
            ],
            "publish_security": self._publish_security(),
            "pipeline": [
                {"key": key, "label": label, "detail": detail, "milestone": ms}
                for key, label, detail, ms in PIPELINE
            ],
            "tm": self.tm_summary(),
            "legacy_baseline": self.legacy_baseline(),
        }

    def _variants(self) -> List[Dict[str, Any]]:
        active = self.config.active_variant
        return [
            {"name": name, "active": name == active, **self._path_status(path)}
            for name, path in sorted(self.config.paths.variants.items())
        ]

    def _publish_security(self) -> Dict[str, Any]:
        remote_targets = [
            target.type for target in self.config.publish.targets if target.type != "local"
        ]
        security = self.config.security
        if not remote_targets:
            state = "not_configured"
            message = "未配置远端发布目标；当前只会生成或复制本地制品。"
        elif not security.remote_publishing_allowed:
            state = "blocked"
            message = (
                "已显式声明凭据需要轮换，但轮换日期或审计记录尚不完整；"
                "本地目标仍会执行，远端目标会被治理闸门拒绝。"
            )
        elif security.credential_rotation_required:
            state = "ready"
            message = "凭据轮换要求及审计记录已完成，远端发布治理检查通过。"
        else:
            state = "ready"
            message = (
                "未声明凭据泄露或强制轮换事件，不启用轮换拦截；"
                "发布时仍会校验环境凭据、Provider 权限及上传结果。"
            )
        return {
            "state": state,
            "message": message,
            "remote_targets": remote_targets,
            "credential_rotation_required": security.credential_rotation_required,
            "credential_rotation_completed": bool(
                security.credential_rotation_completed_at and security.rotation_record
            ),
        }

    def _path_status(self, path: Optional[Path]) -> Dict[str, Any]:
        if path is None:
            return {"path": "", "exists": False, "configured": False}
        resolved = Path(path)
        try:
            exists = resolved.exists()
        except OSError:
            exists = False
        return {"path": str(resolved), "exists": exists, "configured": True}

    def tm_summary(self) -> Dict[str, Any]:
        database = Path(self.config.tm.database)
        if not database.is_file():
            return _missing(f"SQLite TM 尚未创建：{database}")
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return _missing(f"无法以只读方式打开 TM：{exc}")
        try:
            connection.row_factory = sqlite3.Row
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata")
            }
            total = connection.execute("SELECT COUNT(*) AS n FROM tm_entries").fetchone()["n"]

            def group(column: str) -> Dict[str, int]:
                rows = connection.execute(
                    f"SELECT {column} AS k, COUNT(*) AS n FROM tm_entries GROUP BY {column}"
                )
                return {str(row["k"]): row["n"] for row in rows}

            legacy_sync = [
                dict(row)
                for row in connection.execute(
                    "SELECT source_path, source_hash, imported_count, synced_at"
                    " FROM legacy_sync ORDER BY synced_at DESC"
                )
            ]
            return {
                "available": True,
                "database": str(database),
                "authoritative": str(metadata.get("authoritative", "false")).lower() == "true",
                "schema_version": metadata.get("schema_version"),
                "total": total,
                "by_origin": group("origin"),
                "by_review_state": group("review_state"),
                "by_classification": group("classification"),
                "by_quality_state": group("quality_state"),
                "formal": connection.execute(
                    "SELECT COUNT(*) AS n FROM tm_entries WHERE is_formal = 1"
                ).fetchone()["n"],
                "legacy_sync": legacy_sync,
            }
        except sqlite3.Error as exc:
            return _missing(f"读取 TM 失败：{exc}")
        finally:
            connection.close()

    def legacy_baseline(self) -> Dict[str, Any]:
        stats = self.repo_root / "audit/baseline/tm_classification_stats.json"
        payload = _read_json(stats)
        if payload is None:
            return _missing(f"未找到分类基线：{stats}")
        return {"available": True, "source": str(stats), **payload}

    def list_runs(self) -> List[Dict[str, Any]]:
        refs: Dict[str, Dict[str, Optional[Path]]] = {}

        def note(run_id: str, key: str, path: Path) -> None:
            refs.setdefault(
                run_id,
                {"workspace_dir": None, "preview_dir": None, "release_dir": None},
            )
            refs[run_id][key] = path

        runs_root = self.workspace / "runs"
        if runs_root.is_dir():
            for child in runs_root.iterdir():
                if child.is_dir():
                    note(child.name, "workspace_dir", child)
        for mode in ("preview", "release"):
            mode_root = self.output / mode
            if mode_root.is_dir():
                for child in mode_root.iterdir():
                    if child.is_dir():
                        note(child.name, f"{mode}_dir", child)

        summaries = []
        for run_id, paths in refs.items():
            ref = RunRef(
                run_id, paths["workspace_dir"], paths["preview_dir"], paths["release_dir"]
            )
            summaries.append(self._run_summary(ref))
        summaries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return summaries

    def _resolve_run(self, run_id: str) -> Optional[RunRef]:
        if not run_id or any(token in run_id for token in ("/", "\\", "..")):
            return None
        workspace_dir = self.workspace / "runs" / run_id
        preview_dir = self.output / "preview" / run_id
        release_dir = self.output / "release" / run_id
        ref = RunRef(
            run_id,
            workspace_dir if workspace_dir.is_dir() else None,
            preview_dir if preview_dir.is_dir() else None,
            release_dir if release_dir.is_dir() else None,
        )
        if ref.workspace_dir is None and ref.preview_dir is None and ref.release_dir is None:
            return None
        return ref

    def _run_summary(self, ref: RunRef) -> Dict[str, Any]:
        progress = self._progress(ref)
        qa = self._qa_summary(ref)
        artifact = self._artifact(ref)
        publish = self._publish_summary(ref)
        task = self._task_summary(ref)
        timestamps = [
            _utc_iso(path.stat().st_mtime)
            for path in (ref.workspace_dir, ref.preview_dir, ref.release_dir)
            if path is not None
        ]
        return {
            "run_id": ref.run_id,
            "modes": ref.modes,
            "updated_at": max(timestamps) if timestamps else None,
            "stage": self._current_stage(ref, progress, qa, artifact, publish),
            "progress": progress,
            "qa": qa,
            "artifact": artifact,
            "publish": publish,
            "task": task,
        }

    @staticmethod
    def _task_summary(ref: RunRef) -> Optional[Dict[str, Any]]:
        if ref.workspace_dir is None:
            return None
        payload = _read_json(ref.workspace_dir / "task-request.json")
        if not isinstance(payload, dict):
            return None
        result = payload.get("result")
        rebuild = result.get("rebuild") if isinstance(result, dict) else None
        return {
            "kind": str(payload.get("kind") or "run"),
            "status": payload.get("status"),
            "version": payload.get("version"),
            "source_path": payload.get("source_path"),
            "mode": payload.get("mode"),
            "variant": payload.get("variant"),
            "parent_run_id": payload.get("parent_run_id"),
            "rebuild": rebuild if isinstance(rebuild, dict) else None,
            "error": payload.get("error") if isinstance(payload.get("error"), dict) else None,
            "confirmation": (
                payload.get("confirmation")
                if isinstance(payload.get("confirmation"), dict)
                else None
            ),
        }

    @staticmethod
    def _publish_summary(ref: RunRef) -> Dict[str, Any]:
        """读取 run 级发布终态，让流水线刷新/重启后仍能恢复到 Publish。"""
        if ref.workspace_dir is None:
            return _missing("尚无发布记录")
        path = ref.workspace_dir / "publish-result.json"
        if not path.is_file():
            return _missing("尚无 publish-result.json")
        payload = _read_json(path)
        if not isinstance(payload, dict):
            return _missing(f"publish-result.json 无法解析：{path}")
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        error = payload.get("error")
        return {
            "available": True,
            "source": str(path),
            "task_id": payload.get("task_id"),
            "status": str(payload.get("status") or "unknown"),
            "passed": result.get("passed") if "passed" in result else None,
            "targets": result.get("targets") if isinstance(result.get("targets"), list) else [],
            "manifest": result.get("manifest") or payload.get("manifest"),
            "error": error if isinstance(error, dict) else None,
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
        }

    def run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        ref = self._resolve_run(run_id)
        if ref is None:
            return None
        detail = self._run_summary(ref)
        detail["paths"] = {
            "workspace": str(ref.workspace_dir) if ref.workspace_dir else None,
            "preview": str(ref.preview_dir) if ref.preview_dir else None,
            "release": str(ref.release_dir) if ref.release_dir else None,
        }
        detail["batches"] = self._batches(ref)
        detail["runtime"] = self._runtime(ref)
        detail["files"] = self._run_files(ref)
        return detail

    def _checkpoint(self, ref: RunRef) -> Optional[Any]:
        if ref.workspace_dir is None:
            return None
        return _read_json(ref.workspace_dir / "checkpoint.json")

    def _progress(self, ref: RunRef) -> Dict[str, Any]:
        payload = self._checkpoint(ref)
        if not isinstance(payload, dict):
            return _missing("本次运行没有 checkpoint.json（可能全部命中 TM，未调用模型）")
        units = payload.get("units") or {}
        states = Counter(str((item or {}).get("state", "unknown")) for item in units.values())
        total = sum(states.values())
        done = states.get("succeeded", 0)
        return {
            "available": True,
            "total": total,
            "succeeded": done,
            "failed": total - done,
            "by_state": dict(states),
            "percent": round(done * 100 / total, 1) if total else 0.0,
        }

    def _batches(self, ref: RunRef) -> Dict[str, Any]:
        payload = self._checkpoint(ref)
        if not isinstance(payload, dict):
            return _missing("无 checkpoint.json")
        batches = payload.get("batches") or []
        grouped: Dict[str, dict] = {}
        recent_events = []
        for index, batch in enumerate(batches):
            if not isinstance(batch, dict):
                continue
            batch_id = str(batch.get("batch_id") or f"legacy-{index}")
            row = grouped.setdefault(
                batch_id,
                {
                    "index": index,
                    "batch_id": batch_id,
                    "state": "unknown",
                    "reason": "",
                    "size": _batch_size(batch),
                    "resource_path": batch.get("resource_path", ""),
                    "worker_id": batch.get("worker_id", ""),
                    "attempts": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "updated_at": batch.get("timestamp"),
                },
            )
            row["state"] = batch.get("state", "unknown")
            row["reason"] = batch.get("reason", "") or row["reason"]
            row["resource_path"] = batch.get("resource_path", "") or row["resource_path"]
            row["worker_id"] = batch.get("worker_id", "") or row["worker_id"]
            row["updated_at"] = batch.get("timestamp") or row["updated_at"]
            row["input_tokens"] += int(batch.get("input_tokens") or 0)
            row["output_tokens"] += int(batch.get("output_tokens") or 0)
            if batch.get("state") == "submitted":
                row["attempts"] += 1
            recent_events.append(
                {
                    "index": index,
                    "batch_id": batch_id,
                    "state": batch.get("state", "unknown"),
                    "resource_path": batch.get("resource_path", ""),
                    "worker_id": batch.get("worker_id", ""),
                    "size": _batch_size(batch),
                    "input_tokens": int(batch.get("input_tokens") or 0),
                    "output_tokens": int(batch.get("output_tokens") or 0),
                    "reason": batch.get("reason", ""),
                    "timestamp": batch.get("timestamp"),
                }
            )
        rows = sorted(grouped.values(), key=lambda item: item["index"], reverse=True)
        return {
            "available": True,
            "total": len(rows),
            "by_state": dict(Counter(row["state"] for row in rows)),
            "rows": rows,
            "recent_events": list(reversed(recent_events[-100:])),
        }

    def _runtime(self, ref: RunRef) -> Dict[str, Any]:
        payload = self._checkpoint(ref)
        if not isinstance(payload, dict):
            return _missing("尚无 checkpoint.json")
        metrics = payload.get("metrics") or {}
        workers = payload.get("workers") or {}
        resources = payload.get("resources") or {}
        completed_files = metrics.get("completed_files") or []
        resource_rows = []
        for relative_path, raw in resources.items():
            value = dict(raw or {})
            total = int(value.get("units_total") or 0)
            succeeded = int(value.get("units_succeeded") or 0)
            failed = int(value.get("units_failed") or 0)
            processed = succeeded + failed
            resource_rows.append(
                {
                    "relative_path": relative_path,
                    **value,
                    "units_total": total,
                    "units_succeeded": succeeded,
                    "units_failed": failed,
                    "units_processed": processed,
                    "percent": round(processed * 100 / total, 1) if total else 0.0,
                    "input_tokens": int(value.get("input_tokens") or 0),
                    "output_tokens": int(value.get("output_tokens") or 0),
                    "requests": int(value.get("requests") or 0),
                    "batches_total": int(value.get("batches_total") or 0),
                }
            )
        state_order = {
            "running": 0,
            "failed": 1,
            "completed_with_failures": 2,
            "queued": 3,
            "completed": 4,
        }
        resource_rows.sort(
            key=lambda item: (
                state_order.get(str(item.get("state")), 9),
                str(item["relative_path"]),
            )
        )
        resource_states = Counter(str(item.get("state", "unknown")) for item in resource_rows)
        return {
            "available": bool(metrics or workers or resources),
            "requests": int(metrics.get("requests") or 0),
            "input_tokens": int(metrics.get("input_tokens") or 0),
            "output_tokens": int(metrics.get("output_tokens") or 0),
            "total_tokens": int(metrics.get("input_tokens") or 0)
            + int(metrics.get("output_tokens") or 0),
            "translation_units_total": int(metrics.get("translation_units_total") or 0),
            "translation_files_total": int(metrics.get("translation_files_total") or 0),
            "translation_files_completed": len(completed_files),
            "translation_files_running": resource_states.get("running", 0),
            "translation_files_queued": resource_states.get("queued", 0),
            "translation_files_failed": resource_states.get("failed", 0)
            + resource_states.get("completed_with_failures", 0),
            "files_by_state": dict(resource_states),
            "checkpoint_degraded": bool(metrics.get("checkpoint_degraded")),
            "checkpoint_flush_failures": int(metrics.get("checkpoint_flush_failures") or 0),
            "checkpoint_last_error": str(metrics.get("checkpoint_last_error") or ""),
            "workers": [
                {"worker_id": worker_id, **dict(value or {})}
                for worker_id, value in sorted(workers.items())
            ],
            "files": resource_rows,
        }

    def _qa_path(self, ref: RunRef) -> Optional[Path]:
        for directory in (ref.release_dir, ref.preview_dir):
            if directory is None:
                continue
            candidate = directory / "reports" / "qa-report.json"
            if candidate.is_file():
                return candidate
        return None

    def _qa_summary(self, ref: RunRef) -> Dict[str, Any]:
        path = self._qa_path(ref)
        if path is None:
            return _missing("尚未产出 qa-report.json")
        payload = _read_json(path)
        if not isinstance(payload, dict):
            return _missing(f"qa-report.json 无法解析：{path}")
        issues = payload.get("issues") or []
        summary = payload.get("summary") or {}
        return {
            "available": True,
            "source": str(path),
            "passed": bool(summary.get("passed")),
            "error_count": summary.get("error_count", 0),
            "failed_unit_count": summary.get("failed_unit_count", 0),
            "issue_total": len(issues),
            "by_severity": dict(Counter(str(i.get("severity", "unknown")) for i in issues)),
            "by_code": dict(Counter(str(i.get("code", "unknown")) for i in issues)),
        }

    def qa_issues(
        self,
        run_id: str,
        *,
        severity: Optional[str] = None,
        code: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Optional[Dict[str, Any]]:
        ref = self._resolve_run(run_id)
        if ref is None:
            return None
        path = self._qa_path(ref)
        if path is None:
            return {"available": False, "reason": "尚未产出 qa-report.json", "issues": []}
        payload = _read_json(path)
        if not isinstance(payload, dict):
            return {"available": False, "reason": "qa-report.json 无法解析", "issues": []}

        rows: List[Dict[str, Any]] = []
        needle = (query or "").strip().lower()
        for issue in payload.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if severity and str(issue.get("severity")) != severity:
                continue
            if code and str(issue.get("code")) != code:
                continue
            if needle:
                haystack = " ".join(
                    str(issue.get(field, ""))
                    for field in ("message", "relative_path", "stable_identity", "code")
                ).lower()
                if needle not in haystack:
                    continue
            rows.append(
                {
                    "severity": issue.get("severity"),
                    "code": issue.get("code"),
                    "label": QA_CODE_LABELS.get(str(issue.get("code")), ""),
                    "message": issue.get("message"),
                    "relative_path": issue.get("relative_path"),
                    "stable_identity": issue.get("stable_identity"),
                    "details": issue.get("details") or {},
                    "provenance": issue.get("provenance", "unknown"),
                }
            )
        window = rows[offset : offset + max(1, min(limit, 1000))]
        return {"available": True, "total": len(rows), "offset": offset, "issues": window}

    def _artifact(self, ref: RunRef) -> Dict[str, Any]:
        if ref.release_dir is None:
            return _missing("尚无 release 产物（preview 不生成正式 Manifest）")
        manifests = sorted(ref.release_dir.glob("*.manifest.json"))
        if not manifests:
            return _missing("release 目录下没有 Manifest")
        payload = _read_json(manifests[0])
        if not isinstance(payload, dict):
            return _missing(f"Manifest 无法解析：{manifests[0]}")
        artifact = payload.get("artifact") or {}
        artifact_path = manifests[0].parent / str(artifact.get("name", ""))
        return {
            "available": True,
            "manifest": str(manifests[0]),
            "name": artifact.get("name"),
            "sha256": artifact.get("sha256"),
            "size": artifact.get("size"),
            "exists": artifact_path.is_file(),
            "created_at": payload.get("created_at"),
            "quality_gate_passed": bool(payload.get("quality_gate_passed")),
            "file_count": len(payload.get("files") or []),
            "metadata": {
                key: value
                for key, value in payload.items()
                if key not in {"schema_version", "artifact", "files"}
            },
        }

    def _run_files(self, ref: RunRef) -> List[Dict[str, Any]]:
        allowed = {".json", ".csv", ".txt", ".log", ".md", ".yaml", ".yml"}
        rows: List[Dict[str, Any]] = []
        for root in (ref.workspace_dir, ref.preview_dir, ref.release_dir):
            if root is None:
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in allowed:
                    continue
                if path.name in _HIDDEN_RUN_FILES:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rows.append(
                    {
                        "path": str(path),
                        "name": str(path.relative_to(root)).replace("\\", "/"),
                        "root": str(root),
                        "size": stat.st_size,
                        "modified_at": _utc_iso(stat.st_mtime),
                    }
                )
        return rows

    def read_text_file(self, raw_path: str, *, tail_bytes: int = 262144) -> Optional[Dict[str, Any]]:
        try:
            target = Path(raw_path).resolve()
        except (OSError, ValueError):
            return None
        roots = [p for p in (self.workspace.resolve(), self.output.resolve()) if p.exists()]
        if not any(self._is_within(target, root) for root in roots):
            return None
        if not target.is_file() or target.suffix.lower() not in {
            ".json", ".csv", ".txt", ".log", ".md", ".yaml", ".yml"
        }:
            return None
        try:
            size = target.stat().st_size
            raw = AtomicIO.read_bytes(target, tail_bytes=tail_bytes)
        except OSError as exc:
            return {"path": str(target), "truncated": False, "text": f"<读取失败：{exc}>"}
        return {
            "path": str(target),
            "size": size,
            "truncated": size > tail_bytes,
            "text": raw.decode("utf-8", errors="replace"),
        }

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    def _current_stage(
        self,
        ref: RunRef,
        progress: Dict[str, Any],
        qa: Dict[str, Any],
        artifact: Dict[str, Any],
        publish: Dict[str, Any],
    ) -> Dict[str, Any]:
        """由磁盘证据反推当前阶段；发布终态以 run 级回执为准。"""
        if publish.get("available"):
            status = publish.get("status")
            passed = publish.get("passed")
            if status in {"queued", "running"}:
                return {"key": "publish", "state": "running", "note": "发布任务正在执行"}
            if status == "completed" and passed is True:
                targets = publish.get("targets") or []
                return {
                    "key": "publish",
                    "state": "done",
                    "note": f"发布完成：{len(targets)} 个 target 已处理",
                }
            error = publish.get("error") or {}
            if status == "failed":
                note = str(error.get("message") or "发布任务执行失败")
            elif status == "completed" and passed is False:
                failed = sum(
                    1
                    for target in publish.get("targets") or []
                    if str((target or {}).get("status"))
                    not in {"ok", "succeeded", "success"}
                )
                note = f"发布未完全成功：{failed} 个 target 失败"
            else:
                note = f"发布状态异常：{status}"
            return {"key": "publish", "state": "blocked", "note": note}
        if artifact.get("available"):
            return {"key": "build", "state": "done", "note": "已产出正式制品与 Manifest"}
        if qa.get("available"):
            if not qa.get("passed"):
                return {
                    "key": "gate",
                    "state": "blocked",
                    "note": f"QualityGate 未通过：error={qa.get('error_count')} "
                    f"failed_units={qa.get('failed_unit_count')}",
                }
            return {"key": "gate", "state": "done", "note": "QualityGate 通过"}
        if progress.get("available"):
            if progress.get("failed"):
                return {
                    "key": "translate",
                    "state": "running",
                    "note": f"{progress.get('succeeded')}/{progress.get('total')} 成功，"
                    f"{progress.get('failed')} 条待重试或已失败",
                }
            return {"key": "translate", "state": "running", "note": "批次进行中"}
        if ref.workspace_dir is not None:
            return {"key": "scan", "state": "running", "note": "工作区已创建，尚无批次记录"}
        return {"key": "scan", "state": "unknown", "note": "无可用证据"}
