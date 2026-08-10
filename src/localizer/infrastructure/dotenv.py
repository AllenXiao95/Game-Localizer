"""最小、可审计的 dotenv 加载器。

只支持单行 KEY=VALUE / export KEY=VALUE；不做命令替换、变量展开或多行脚本解释。
异常只报告文件与行号，绝不把值写进日志。
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Tuple


_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MISSING = object()


def parse_dotenv(path: Path) -> Dict[str, str]:
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("dotenv path must be a file")
    if source.stat().st_size > 1024 * 1024:
        raise ValueError("dotenv file exceeds 1 MiB")
    try:
        lines = source.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read dotenv file: {source}") from exc
    values: Dict[str, str] = {}
    for line_number, original in enumerate(lines, 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid dotenv syntax at {source}:{line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"invalid dotenv key at {source}:{line_number}")
        try:
            values[key] = _parse_value(raw_value.strip())
        except ValueError as exc:
            raise ValueError(
                f"invalid dotenv value syntax at {source}:{line_number}"
            ) from exc
    return values


def _parse_value(raw: str) -> str:
    if not raw:
        return ""
    if raw.startswith('"'):
        if not raw.endswith('"'):
            raise ValueError("unterminated double quote")
        return str(json.loads(raw))
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise ValueError("unterminated single quote")
        return raw[1:-1]
    # 非引号值只把「空白 + #」视作注释；Token 中合法的 # 不会被截断。
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def merged_dotenv(paths: Iterable[Path]) -> Tuple[Tuple[Path, ...], Dict[str, str]]:
    ordered = []
    values: Dict[str, str] = {}
    seen = set()
    for raw_path in paths:
        path = Path(raw_path).resolve(strict=True)
        normalized = os.path.normcase(str(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(path)
        values.update(parse_dotenv(path))
    return tuple(ordered), values


def load_dotenv_files(paths: Iterable[Path], *, override: bool = False) -> Tuple[Path, ...]:
    ordered, values = merged_dotenv(paths)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return ordered


@contextmanager
def temporary_dotenv(
    paths: Iterable[Path], *, override: bool = False
) -> Iterator[Mapping[str, str]]:
    """为单个串行 Web 任务临时加载文件，结束后恢复进程环境。"""
    _ordered, values = merged_dotenv(paths)
    original = {}
    applied = {}
    for key, value in values.items():
        if override or key not in os.environ:
            original[key] = os.environ.get(key, _MISSING)
            os.environ[key] = value
            applied[key] = value
    try:
        # 仅供调用方检查变量名，禁止记录该映射的 value。
        yield applied
    finally:
        for key, old_value in original.items():
            if old_value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(old_value)
