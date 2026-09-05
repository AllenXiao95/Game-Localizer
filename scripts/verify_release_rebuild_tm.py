"""Safely verify #55 against a real WebUI run without touching production state.

Usage (from the repository root after installing the branch):

    python scripts/verify_release_rebuild_tm.py projects/<project>/project.yaml \
        --parent-run-id <published-or-preview-run> [--variant <name>]

The verifier:

1. reads and reports the selected run's WebUI mode/kind/lineage when available;
2. reads the production TM only for diagnostic counts;
3. makes a SQLite backup into a temporary sandbox using the backup API;
4. copies only the selected parent run workspace into that sandbox;
5. forces artifact encryption off in the sandbox;
6. refuses to continue if the rebuild would require any Provider request;
7. performs a release rebuild with a Provider implementation that raises if called;
8. runs a fresh translation plan and verifies reused machine translations became TM hits;
9. removes the sandbox automatically unless --keep is supplied.

Production TM, workspace, output and published artifacts are never written.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.application.batch_orchestrator import JsonCheckpoint
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import ProjectRunner
from localizer.config import load_project_config
from localizer.config.models import ProjectConfig
from localizer.web.tasks import TaskService


class _NoProvider:
    """Hard fail if the verification accidentally tries to spend a Provider request."""

    def translate(self, *_args, **_kwargs):
        raise RuntimeError(
            "verification aborted: rebuild attempted a Provider request; "
            "choose a parent run whose current plan is fully reusable"
        )


def _model_data(config: ProjectConfig) -> dict[str, Any]:
    if hasattr(config, "model_dump"):
        return config.model_dump()
    return config.dict()


def _validate_config(data: dict[str, Any]) -> ProjectConfig:
    if hasattr(ProjectConfig, "model_validate"):
        return ProjectConfig.model_validate(data)
    return ProjectConfig.parse_obj(data)


def _run_metadata(parent_path: Path) -> dict[str, Any]:
    """Read enough immutable run evidence to distinguish direct release from rebuild."""
    request_path = parent_path / "task-request.json"
    request: dict[str, Any] = {}
    if request_path.is_file():
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid task snapshot: {request_path}")
        request = payload

    lineage_path = parent_path / ProjectRunner.LINEAGE_FILENAME
    lineage: dict[str, Any] = {}
    if lineage_path.is_file():
        payload = json.loads(lineage_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            lineage = payload

    parent = str(
        request.get("parent_run_id")
        or lineage.get("parent_run_id")
        or ""
    ).strip()
    kind = str(request.get("kind") or ("rebuild" if parent else "run")).strip()
    return {
        "kind": kind,
        "mode": str(request.get("mode") or "").strip() or None,
        "status": str(request.get("status") or "").strip() or None,
        "parent_run_id": parent or None,
        "reuse_checkpoint_run_id": (
            str(lineage.get("reuse_checkpoint_run_id") or "").strip() or None
        ),
        "has_task_request": request_path.is_file(),
        "has_run_lineage": lineage_path.is_file(),
    }


def _task_config(base: ProjectConfig, parent_path: Path) -> ProjectConfig:
    """Recreate the same dynamic source/version projection the WebUI used."""
    request_path = parent_path / "task-request.json"
    if not request_path.is_file():
        return base
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid task snapshot: {request_path}")

    snapshot_variant = str(payload.get("variant") or "").strip()
    active_variant = str(base.active_variant or "")
    if snapshot_variant and snapshot_variant != active_variant:
        raise RuntimeError(
            f"run belongs to variant {snapshot_variant!r}, current config is "
            f"{active_variant!r}; pass the matching --variant"
        )

    version = str(payload.get("version") or base.project.game_version).strip()
    source_raw = str(payload.get("source_path") or "").strip()
    if not source_raw:
        return base.for_game_version(version)
    source = Path(source_raw).expanduser().resolve(strict=True)

    service = TaskService(base)
    try:
        return service._overridden_config(source, version)
    finally:
        service.shutdown()


def _sandbox_config(live: ProjectConfig, root: Path) -> ProjectConfig:
    data = _model_data(live)
    data["paths"]["workspace"] = root / "workspace"
    data["paths"]["output"] = root / "output"
    data["tm"]["database"] = root / "tm.sqlite3"
    # The verification is about TM authority, not archive encryption. Never require or
    # read the production package password for this disposable release artifact.
    data["build"]["encryption"] = "none"
    data["build"].pop("password_env", None)
    sandbox = _validate_config(data)
    object.__setattr__(sandbox, "_active_variant", live.active_variant)
    return sandbox


def _backup_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"production TM does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _production_tm_diagnostics(
    live: ProjectConfig,
    parent_run_id: str,
    checkpoint: JsonCheckpoint,
) -> dict[str, int]:
    """Read-only evidence: did the selected run already become formal in production TM?"""
    succeeded = tuple(
        identity
        for identity, row in checkpoint.units.items()
        if row.get("state") == "succeeded"
    )
    with SQLiteTranslationMemory(live.tm.database, read_only=True) as tm:
        rows = tm.rows_for(succeeded)
    return {
        "checkpoint_succeeded": len(succeeded),
        "rows_present": len(rows),
        "formal_rows": sum(1 for row in rows.values() if bool(row.get("is_formal"))),
        "rows_attributed_to_selected_run": sum(
            1 for row in rows.values() if row.get("run_id") == parent_run_id
        ),
        "formal_rows_attributed_to_selected_run": sum(
            1
            for row in rows.values()
            if bool(row.get("is_formal")) and row.get("run_id") == parent_run_id
        ),
    }


def _verify(config_path: Path, parent_run_id: str, variant: str | None, root: Path) -> dict:
    base = load_project_config(config_path).for_variant(variant)
    original_parent = Path(base.paths.workspace) / "runs" / parent_run_id
    if not original_parent.is_dir():
        raise FileNotFoundError(f"parent run does not exist: {original_parent}")
    checkpoint_path = original_parent / "checkpoint.json"
    if not checkpoint_path.is_file():
        raise RuntimeError(
            "selected parent has no checkpoint.json; choose the materialized run that "
            "contains the translations you want to verify"
        )

    run_metadata = _run_metadata(original_parent)
    live = _task_config(base, original_parent)
    production_checkpoint = JsonCheckpoint(checkpoint_path)
    production_tm = _production_tm_diagnostics(
        live, parent_run_id, production_checkpoint
    )

    sandbox = _sandbox_config(live, root)
    _backup_sqlite(Path(live.tm.database), Path(sandbox.tm.database))

    sandbox_parent = Path(sandbox.paths.workspace) / "runs" / parent_run_id
    sandbox_parent.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(original_parent, sandbox_parent)

    runner = ProjectRunner(sandbox, provider=_NoProvider())
    before = runner.plan()
    reuse_checkpoint_run_id, parent_checkpoint = runner._resolve_parent_checkpoint(
        parent_run_id
    )
    rebuild = runner._plan_rebuild(
        parent_run_id,
        reuse_checkpoint_run_id,
        parent_checkpoint,
        before,
    )

    # Hard safety boundary: the verifier is allowed to test reuse only. It must never
    # turn into an accidental paid translation run on real source material.
    if rebuild.retry:
        raise RuntimeError(
            f"verification refused before execution: {len(rebuild.retry)} units would "
            "require Provider work; choose a fully reusable parent or repair it first"
        )
    if not rebuild.reused:
        raise RuntimeError(
            "verification has nothing to exercise: current plan contains no reusable "
            "parent machine candidates"
        )

    run_id = "verify-issue55-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    release = runner.rebuild_from_run(
        parent_run_id,
        mode=BuildMode.RELEASE,
        run_id=run_id,
        plan=before,
    )
    if release.machine_successes != 0:
        raise AssertionError(
            f"expected zero new Provider successes, got {release.machine_successes}"
        )

    after = ProjectRunner(sandbox, provider=_NoProvider()).plan()
    with SQLiteTranslationMemory(sandbox.tm.database, read_only=True) as tm:
        rows = tm.rows_for(tuple(rebuild.reused))
    formal_reused = sum(
        1
        for identity in rebuild.reused
        if rows.get(identity)
        and bool(rows[identity].get("is_formal"))
        and rows[identity].get("run_id") == run_id
    )

    result = {
        "sandbox": str(root),
        "parent_run_id": parent_run_id,
        "affected_run": run_metadata,
        "production_tm_read_only": production_tm,
        "verification_release_run_id": run_id,
        "before": {
            "extracted_units": before.extracted_units,
            "tm_hits": before.tm_hits,
            "pending_units": len(before.pending),
        },
        "release": {
            "reused": len(rebuild.reused),
            "retried": len(rebuild.retry),
            "machine_successes": release.machine_successes,
            "formal_reused_rows": formal_reused,
        },
        "after": {
            "extracted_units": after.extracted_units,
            "tm_hits": after.tm_hits,
            "pending_units": len(after.pending),
        },
    }

    expected_hits = before.tm_hits + len(rebuild.reused)
    if formal_reused != len(rebuild.reused):
        raise AssertionError(
            f"only {formal_reused}/{len(rebuild.reused)} reused rows became formal"
        )
    if len(after.pending) != 0:
        raise AssertionError(
            f"fresh plan still has {len(after.pending)} pending units after release rebuild"
        )
    if after.tm_hits < expected_hits:
        raise AssertionError(
            f"fresh plan has {after.tm_hits} TM hits; expected at least {expected_hits}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reversible real-project verification for issue #55"
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("--parent-run-id", required=True)
    parser.add_argument("--variant", default=None)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the sandbox directory for inspection instead of deleting it",
    )
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve(strict=True)

    if args.keep:
        root = Path(tempfile.mkdtemp(prefix="game-localizer-issue55-verify-"))
        try:
            result = _verify(config_path, args.parent_run_id, args.variant, root)
        except Exception:
            print(json.dumps({"sandbox": str(root), "kept": True}, indent=2))
            raise
        result["sandbox_kept"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="game-localizer-issue55-verify-") as temp:
        result = _verify(
            config_path,
            args.parent_run_id,
            args.variant,
            Path(temp),
        )
        result["sandbox_kept"] = False
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
