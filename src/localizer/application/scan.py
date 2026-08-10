from __future__ import annotations

import fnmatch
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class ScannedResource:
    relative_path: str
    absolute_path: Path
    size: int
    suffix: str
    adapter_hint: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["absolute_path"] = str(self.absolute_path)
        return data


@dataclass(frozen=True)
class ScanResult:
    root: Path
    resources: Tuple[ScannedResource, ...]
    ignored_symlinks: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "resource_count": len(self.resources),
            "resources": [item.to_dict() for item in self.resources],
            "ignored_symlinks": list(self.ignored_symlinks),
        }


class ResourceScanner:
    ADAPTER_HINTS = {
        ".mo": "gettext",
        ".po": "gettext",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".csv": "csv",
        ".ssv": "csv",
        ".properties": "properties",
        ".lang": "properties",
        ".ini": "ini",
    }

    def scan(
        self,
        root: Path,
        *,
        includes: Sequence[str] = ("**/*",),
        excludes: Sequence[str] = (),
    ) -> ScanResult:
        scan_root = Path(root).resolve(strict=True)
        if not scan_root.is_dir():
            raise NotADirectoryError(scan_root)

        resources: List[ScannedResource] = []
        ignored_symlinks: List[str] = []
        for directory, dir_names, file_names in os.walk(scan_root, followlinks=False):
            directory_path = Path(directory)
            retained_dirs = []
            for name in dir_names:
                candidate = directory_path / name
                relative = candidate.relative_to(scan_root).as_posix()
                if candidate.is_symlink():
                    ignored_symlinks.append(relative)
                elif not self._matches(relative + "/", excludes):
                    retained_dirs.append(name)
            dir_names[:] = retained_dirs

            for name in file_names:
                candidate = directory_path / name
                relative = candidate.relative_to(scan_root).as_posix()
                if candidate.is_symlink():
                    ignored_symlinks.append(relative)
                    continue
                if not self._matches(relative, includes) or self._matches(relative, excludes):
                    continue
                suffix = candidate.suffix.lower()
                adapter_hint = self.ADAPTER_HINTS.get(suffix, "unknown")
                resources.append(
                    ScannedResource(
                        relative_path=relative,
                        absolute_path=candidate.resolve(),
                        size=candidate.stat().st_size,
                        suffix=suffix,
                        adapter_hint=adapter_hint,
                    )
                )

        resources.sort(key=lambda item: item.relative_path)
        ignored_symlinks.sort()
        return ScanResult(scan_root, tuple(resources), tuple(ignored_symlinks))

    @staticmethod
    def _matches(path: str, patterns: Iterable[str]) -> bool:
        normalized = path.replace("\\", "/")
        for pattern in patterns:
            candidate = pattern.replace("\\", "/")
            if candidate == "**/*":
                return True
            if fnmatch.fnmatchcase(normalized, candidate):
                return True
            if candidate.startswith("**/") and fnmatch.fnmatchcase(normalized, candidate[3:]):
                return True
        return False
