from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional, Set

from .atomic_io import AtomicIO


class WorkspaceBoundaryError(RuntimeError):
    pass


class DuplicateTargetError(RuntimeError):
    pass


# run_id 会被直接拼进工作区与输出目录的路径，必须是路径安全的单段标识符。
# 收紧到显式白名单而不是黑名单几个危险字符：`..` 能穿越，`feature/x` 这类
# CI 常见写法会在 tempfile.mkstemp 处炸出难懂的 FileNotFoundError，
# 冒号、通配符、控制字符在 Windows 上各有各的坑。
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def validate_run_id(run_id: str) -> str:
    """确认 run_id 可以安全地作为单层目录名使用。"""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        raise ValueError(
            f"invalid run_id {run_id!r}: must be 1-64 chars of "
            f"letters, digits, dot, dash or underscore, and start with "
            f"a letter or digit"
        )
    return run_id


class RunWorkspace:
    """Owns all mutable files created by one run."""

    SUBDIRECTORIES = (
        "batches",
        "provider-responses",
        "preview",
        "reports",
        "temp",
    )

    def __init__(self, workspace_root: Path, run_id: str) -> None:
        validate_run_id(run_id)
        self.workspace_root = Path(workspace_root).resolve()
        self.run_id = run_id
        self.path = self.workspace_root / "runs" / run_id
        self._owned_temporary_files: Set[Path] = set()
        self._reserved_targets: Set[str] = set()

    def create(self) -> "RunWorkspace":
        self.path.mkdir(parents=True, exist_ok=False)
        for name in self.SUBDIRECTORIES:
            (self.path / name).mkdir()
        return self

    def open_existing(self) -> "RunWorkspace":
        if not self.path.is_dir():
            raise FileNotFoundError(self.path)
        return self

    def child(self, *parts: str) -> Path:
        candidate = (self.path.joinpath(*parts)).resolve()
        self._assert_inside(candidate)
        return candidate

    def register_temporary_file(self, path: Path) -> Path:
        candidate = Path(path).resolve()
        self._assert_inside(candidate)
        self._owned_temporary_files.add(candidate)
        return candidate

    def reserve_targets(self, targets: Iterable[Path]) -> None:
        for target in targets:
            normalized = os.path.normcase(str(Path(target).resolve()))
            if normalized in self._reserved_targets:
                raise DuplicateTargetError(f"duplicate target path: {target}")
            self._reserved_targets.add(normalized)

    def write_snapshot(self, name: str, value: object) -> Path:
        if Path(name).name != name:
            raise WorkspaceBoundaryError(f"snapshot name must not contain a path: {name}")
        return AtomicIO.write_json(self.child(name), value)

    def cleanup_owned_temporary_files(self) -> None:
        for path in tuple(self._owned_temporary_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            finally:
                self._owned_temporary_files.discard(path)

    def _assert_inside(self, candidate: Path) -> None:
        try:
            candidate.relative_to(self.path)
        except ValueError as exc:
            raise WorkspaceBoundaryError(
                f"path escapes run workspace {self.path}: {candidate}"
            ) from exc
