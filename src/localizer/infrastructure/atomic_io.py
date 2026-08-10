from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# Windows 上「原子替换」与「并发读」必须成对选型，单改一侧无效。三组对照实测
# （同一目标文件，读侧句柄份额 × 替换原语）：
#
#   读侧句柄份额                    os.replace        ReplaceFileW
#   SHARE_READ|WRITE（Python open） 失败 WinError=5   失败 GetLastError=32
#   SHARE_READ|WRITE|DELETE         失败 WinError=5   成功
#   无读句柄                        成功              成功
#
# 结论：`os.replace` 走的是 MoveFileExW，读侧**无论怎么开**都会被阻塞 ——
# 这不是「短暂」冲突而是 100% 命中，退避阶梯只是把必然冲突变成概率冲突。
# 只有 `ReplaceFileW` + 读侧共享 DELETE 这一组能真正成功，所以写侧用
# `_windows_replace()`、读侧用 `AtomicIO.read_bytes()`，两者缺一不可。
if os.name == "nt":  # pragma: no cover —— 平台分支，POSIX 上不导入
    import ctypes
    import ctypes.wintypes as _wintypes
    import msvcrt

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.ReplaceFileW.restype = _wintypes.BOOL
    _KERNEL32.ReplaceFileW.argtypes = [
        _wintypes.LPCWSTR,   # lpReplacedFileName
        _wintypes.LPCWSTR,   # lpReplacementFileName
        _wintypes.LPCWSTR,   # lpBackupFileName
        _wintypes.DWORD,     # dwReplaceFlags
        ctypes.c_void_p,     # lpExclude
        ctypes.c_void_p,     # lpReserved
    ]
    _KERNEL32.CreateFileW.restype = _wintypes.HANDLE
    _KERNEL32.CreateFileW.argtypes = [
        _wintypes.LPCWSTR,
        _wintypes.DWORD,
        _wintypes.DWORD,
        ctypes.c_void_p,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.HANDLE,
    ]

    _INVALID_HANDLE_VALUE = _wintypes.HANDLE(-1).value
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    # ACL/属性合并失败不该让整次替换失败：文件内容才是我们要的原子性。
    _REPLACEFILE_IGNORE_MERGE_ERRORS = 0x2


class AtomicWriteError(RuntimeError):
    pass


class CrossFilesystemError(AtomicWriteError):
    pass


class AtomicIO:
    """Crash-safe file writes whose temporary files belong to one operation."""

    # 读侧若没换成共享 DELETE 的句柄（例如杀毒软件、编辑器、外部脚本），
    # ReplaceFileW 仍会返回 32/33。退避阶梯保留作兜底，但既然主路径已经
    # 不再必然冲突，不需要再等 2 秒 —— 长阶梯的真实代价是 :132 的 RLock
    # 全程被持有，4 个 worker 会一起卡住。
    _WINDOWS_REPLACE_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)

    @staticmethod
    def write_bytes(target: Path, data: bytes) -> Path:
        destination = Path(target).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temp_path = Path(temp_name)
        replaced = False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            AtomicIO._assert_same_filesystem(temp_path, destination.parent)
            AtomicIO._replace_with_retry(temp_path, destination)
            replaced = True
            AtomicIO._fsync_directory(destination.parent)
            return destination
        except Exception as exc:
            if isinstance(exc, AtomicWriteError):
                raise
            raise AtomicWriteError(f"atomic write failed for {destination}: {exc}") from exc
        finally:
            if not replaced:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def write_text(target: Path, text: str, *, encoding: str = "utf-8") -> Path:
        return AtomicIO.write_bytes(target, text.encode(encoding))

    @staticmethod
    def write_json(target: Path, value: Any, *, indent: int = 2) -> Path:
        payload = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)
        return AtomicIO.write_text(target, payload + "\n")

    @staticmethod
    def replace_file(source: Path, target: Path) -> Path:
        source_path = Path(source).resolve(strict=True)
        destination = Path(target).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        AtomicIO._assert_same_filesystem(source_path, destination.parent)
        AtomicIO._replace_with_retry(source_path, destination)
        AtomicIO._fsync_directory(destination.parent)
        return destination

    @staticmethod
    def read_bytes(target: Path, *, tail_bytes: Optional[int] = None) -> bytes:
        """并发安全地读一个可能正在被原子替换的文件。

        Windows 上普通 `open()` 拿的是 SHARE_READ|SHARE_WRITE 句柄，**不共享
        DELETE**，写侧的 `ReplaceFileW` 会因此拿到 ERROR_SHARING_VIOLATION。
        观测面板、日志尾读这类「读正在被写的文件」的场景必须走这里，否则
        写侧的修复等于没做（见模块顶部的对照表）。POSIX 上就是普通读。

        `tail_bytes` 只读文件末尾 N 字节（日志尾读用）。
        """
        path = Path(target)
        if os.name != "nt":
            with path.open("rb") as stream:
                return AtomicIO._read_stream(stream, tail_bytes)
        handle = _KERNEL32.CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_READ_WRITE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            # 转成标准 OSError（含 errno/winerror），调用方的 except OSError 照常工作。
            raise ctypes.WinError(ctypes.get_last_error())
        # 句柄所有权移交给 fd，fdopen 关闭时一并释放。
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        with os.fdopen(descriptor, "rb") as stream:
            return AtomicIO._read_stream(stream, tail_bytes)

    @staticmethod
    def _read_stream(stream: Any, tail_bytes: Optional[int]) -> bytes:
        if tail_bytes is not None:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - tail_bytes))
        return stream.read()

    @staticmethod
    def read_text(target: Path, *, encoding: str = "utf-8") -> str:
        return AtomicIO.read_bytes(target).decode(encoding)

    @staticmethod
    def _replace_with_retry(source: Path, destination: Path) -> None:
        for delay in (*AtomicIO._WINDOWS_REPLACE_DELAYS, None):
            try:
                AtomicIO._replace_once(source, destination)
                return
            except OSError as exc:
                if delay is None or not AtomicIO._is_transient_windows_lock(exc):
                    raise
                time.sleep(delay)

    @staticmethod
    def _replace_once(source: Path, destination: Path) -> None:
        if os.name != "nt" or not destination.exists():
            # ReplaceFileW 要求被替换文件已存在；首次创建走普通 rename。
            os.replace(str(source), str(destination))
            return
        ctypes.set_last_error(0)
        if _KERNEL32.ReplaceFileW(
            str(destination),
            str(source),
            None,
            _REPLACEFILE_IGNORE_MERGE_ERRORS,
            None,
            None,
        ):
            return
        error = ctypes.get_last_error()
        if error not in {5, 32, 33}:
            # 非共享冲突（例如 ERROR_UNABLE_TO_MOVE_REPLACEMENT）退回旧原语，
            # 保证这次改动在任何情况下都不比原来更差。
            os.replace(str(source), str(destination))
            return
        raise ctypes.WinError(error)

    @staticmethod
    def _is_transient_windows_lock(exc: OSError) -> bool:
        if os.name != "nt":
            return False
        # 5: ACCESS_DENIED, 32: SHARING_VIOLATION, 33: LOCK_VIOLATION。
        winerror = getattr(exc, "winerror", None)
        return winerror in {5, 32, 33}

    @staticmethod
    def assert_unique_targets(targets: Iterable[Path]) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for target in targets:
            normalized = os.path.normcase(str(Path(target).resolve()))
            if normalized in seen:
                duplicates.add(normalized)
            seen.add(normalized)
        if duplicates:
            raise AtomicWriteError(
                "duplicate target paths in one plan: " + ", ".join(sorted(duplicates))
            )

    @staticmethod
    def _assert_same_filesystem(source: Path, target_directory: Path) -> None:
        source_device = source.stat().st_dev
        target_device = target_directory.stat().st_dev
        if source_device != target_device:
            raise CrossFilesystemError(
                f"atomic replace crosses filesystems: {source} -> {target_directory}"
            )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(str(directory), flags)
            os.fsync(descriptor)
        except OSError:
            # Windows and some filesystems do not allow directory fsync.
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)
