"""checkpoint 落盘不得有权力终止一轮已经付费的翻译（评估 R01 / R05）。

真机故障链路（`docs/preview-validation-20260802.md` §7）：
    面板轮询持有读句柄 → 写侧 os.replace 撞 WinError 5 → `_flush` 上抛
    → `run()` 的 except 调 `fail_resource` → `fail_resource` 内部再 `_flush`
    **抛第二个异常把首因彻底吞掉** → 整轮 preview 中止 → `finalize()` 在
    `with` 块之后，永远到不了，合并窗口里已成功的结果被丢弃。

这个文件把那条链路的每一环各钉一条断言。R05 那半（原语选型）在 Windows 上
用真实读句柄验证，在 POSIX 上跳过 —— 但 R01 那半是纯逻辑，两个平台都跑。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.batch_orchestrator import JsonCheckpoint, UnitResult
from localizer.infrastructure.atomic_io import AtomicIO, AtomicWriteError

WINDOWS = os.name == "nt"


def _dashboard_style_reader(path: Path):
    """复刻面板读法：普通 `open()` = SHARE_READ|SHARE_WRITE，**不含 DELETE**。"""
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
    ]
    handle = k32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
    if handle == wt.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return k32, handle


class ReplacePrimitiveTests(unittest.TestCase):
    """R05：Windows 上 os.replace 是错误的原语。

    三组对照（读侧句柄份额 × 原语）实测：
        SHARE_READ|WRITE        os.replace 失败 WinError=5 / ReplaceFileW 失败 32
        SHARE_READ|WRITE|DELETE os.replace **仍失败 WinError=5** / ReplaceFileW 成功
        无读句柄                两者都成功
    所以「读侧共享 DELETE」与「写侧用 ReplaceFileW」必须同时成立，缺一无效。
    """

    @unittest.skipUnless(WINDOWS, "replace 语义差异只在 Windows 上存在")
    def test_write_succeeds_while_a_share_delete_reader_holds_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "checkpoint.json"
            AtomicIO.write_json(target, {"round": 0})
            # AtomicIO.read_bytes 拿的是共享 DELETE 的句柄；持有期间写侧必须成功。
            handle_kept_open = AtomicIO  # 见下：用真实句柄而不是读完即关
            del handle_kept_open
            import ctypes.wintypes as wt

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.restype = wt.HANDLE
            k32.CreateFileW.argtypes = [
                wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                wt.DWORD, wt.DWORD, wt.HANDLE,
            ]
            reader = k32.CreateFileW(
                str(target), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0, None
            )
            self.assertNotEqual(wt.HANDLE(-1).value, reader)
            try:
                AtomicIO.write_json(target, {"round": 1})
            finally:
                k32.CloseHandle(reader)
            self.assertEqual({"round": 1}, json.loads(target.read_text("utf-8")))

    @unittest.skipUnless(WINDOWS, "replace 语义差异只在 Windows 上存在")
    def test_plain_os_replace_would_have_failed_under_the_same_reader(self) -> None:
        # 反证：说明上一条测的是真效果，不是「这个场景本来就不冲突」。
        import ctypes.wintypes as wt

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "x.json"
            target.write_text("old", encoding="utf-8")
            source = Path(temp) / "x.tmp"
            source.write_text("new", encoding="utf-8")
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.restype = wt.HANDLE
            k32.CreateFileW.argtypes = [
                wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                wt.DWORD, wt.DWORD, wt.HANDLE,
            ]
            reader = k32.CreateFileW(
                str(target), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0, None
            )
            try:
                with self.assertRaises(OSError) as ctx:
                    os.replace(str(source), str(target))
                self.assertEqual(5, getattr(ctx.exception, "winerror", None))
            finally:
                k32.CloseHandle(reader)

    def test_read_bytes_matches_plain_read_including_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "log.txt"
            target.write_bytes(b"0123456789")
            self.assertEqual(b"0123456789", AtomicIO.read_bytes(target))
            self.assertEqual(b"789", AtomicIO.read_bytes(target, tail_bytes=3))
            # tail 超过文件长度就是整份内容，不能抛也不能截断。
            self.assertEqual(b"0123456789", AtomicIO.read_bytes(target, tail_bytes=99))

    def test_missing_file_still_raises_oserror(self) -> None:
        # 调用方的 `except OSError` 依赖这一点；换成 ctypes 后不能变成别的异常。
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(OSError):
                AtomicIO.read_bytes(Path(temp) / "nope.json")


class _ExplodingIO:
    """把 AtomicIO.write_json 换成必定失败，用来驱动降级路径。"""

    def __init__(self, monkeypatched_module, failures: int) -> None:
        self.module = monkeypatched_module
        self.remaining = failures
        self.original = monkeypatched_module.AtomicIO.write_json

    def __enter__(self):
        def failing(path, value, **kwargs):
            if self.remaining > 0:
                self.remaining -= 1
                raise AtomicWriteError(f"atomic write failed for {path}: [WinError 5]")
            return self.original(path, value, **kwargs)

        self.module.AtomicIO.write_json = staticmethod(failing)
        return self

    def __exit__(self, *exc):
        self.module.AtomicIO.write_json = staticmethod(self.original)
        return False


class CheckpointToleratesFlushFailureTests(unittest.TestCase):
    """R01：checkpoint 是进度优化，不该有权力终止运行。"""

    def _checkpoint(self, temp: str) -> JsonCheckpoint:
        # min_flush_interval=0：否则 record_result 会被合并窗口挡下，
        # 根本走不到落盘，测试就变成空转的（第一版正是如此）。
        checkpoint = JsonCheckpoint(Path(temp) / "checkpoint.json", min_flush_interval=0)
        checkpoint.configure_run(
            translation_units_total=2,
            translation_files_total=1,
            resource_units={"a.mo": ["id-1", "id-2"]},
        )
        return checkpoint

    def test_a_single_flush_failure_does_not_abort_the_run(self) -> None:
        from localizer.application import batch_orchestrator as module

        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(temp)
            with _ExplodingIO(module, failures=1):
                # 不抛 —— 这就是本条的全部意义。
                checkpoint.record_batch(
                    ["id-1"], "planned", resource_path="a.mo", batch_id="b1"
                )
            self.assertTrue(checkpoint.metrics.get("checkpoint_degraded"))
            self.assertEqual(1, checkpoint.metrics.get("checkpoint_flush_failures"))

    def test_state_is_not_lost_and_the_next_flush_catches_up(self) -> None:
        from localizer.application import batch_orchestrator as module

        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(temp)
            with _ExplodingIO(module, failures=1) as io:
                checkpoint.record_result(UnitResult("id-1", "译文一", "succeeded", ()))
                # 先证明这次落盘真的被尝试过并失败了 —— 否则下面的断言全是空转。
                self.assertEqual(0, io.remaining)
            self.assertTrue(checkpoint.metrics.get("checkpoint_degraded"))
            # 失败那次必须保持 pending，否则下一个合并窗口会以为已经写过。
            self.assertTrue(checkpoint._pending_flush)
            checkpoint.finalize()
            on_disk = json.loads(
                (Path(temp) / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual("译文一", on_disk["units"]["id-1"]["translation"])
            self.assertFalse(checkpoint.metrics.get("checkpoint_degraded"))
            self.assertTrue(checkpoint.metrics.get("checkpoint_recovered_after_failures"))

    def test_persistent_failure_still_escalates(self) -> None:
        # 宽容不等于永远不报：磁盘满/目录被删这类真实故障必须最终抛出。
        from localizer.application import batch_orchestrator as module

        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(temp)
            with _ExplodingIO(module, failures=999):
                with self.assertRaises(AtomicWriteError):
                    for index in range(JsonCheckpoint._MAX_CONSECUTIVE_FLUSH_FAILURES):
                        checkpoint.record_batch(
                            ["id-1"], "planned", resource_path="a.mo",
                            batch_id=f"b{index}",
                        )

    def test_fail_resource_never_masks_the_original_exception(self) -> None:
        """异常路径上的落盘失败不得产生第二个异常。

        原来 `fail_resource` 内部的 `_flush(force=True)` 会抛出，调用方的
        `raise` 根本执行不到，用户只看到「存取被拒」，真正的首因永远丢失。
        """
        from localizer.application import batch_orchestrator as module

        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(temp)
            with _ExplodingIO(module, failures=999):
                try:
                    raise RuntimeError("首因：provider 返回了编号错乱的批次")
                except RuntimeError as first_cause:
                    # 即使已经超过升级阈值，这条路径也必须静默。
                    for _ in range(JsonCheckpoint._MAX_CONSECUTIVE_FLUSH_FAILURES + 3):
                        checkpoint.fail_resource(
                            "translation-0", "a.mo", str(first_cause)
                        )
            self.assertEqual("failed", checkpoint.resources["a.mo"]["state"])

    def test_finalize_is_tolerant_too(self) -> None:
        from localizer.application import batch_orchestrator as module

        with tempfile.TemporaryDirectory() as temp:
            checkpoint = self._checkpoint(temp)
            checkpoint.record_result(UnitResult("id-1", "译文一", "succeeded", ()))
            with _ExplodingIO(module, failures=999):
                checkpoint.finalize()  # 收尾不制造第二个异常


class FinalizeRunsOnTheFailurePathTests(unittest.TestCase):
    """R01-③：`finalize()` 必须在 try/finally 里，不能是直线代码。"""

    def test_project_runner_calls_finalize_from_a_finally_block(self) -> None:
        import ast

        source = (SRC / "localizer/application/project_runner.py").read_text("utf-8")
        tree = ast.parse(source)

        def calls_finalize(nodes) -> bool:
            return any(
                isinstance(node, ast.Attribute) and node.attr == "finalize"
                for statement in nodes
                for node in ast.walk(statement)
            )

        in_finally = any(
            isinstance(node, ast.Try) and calls_finalize(node.finalbody)
            for node in ast.walk(tree)
        )
        self.assertTrue(
            in_finally,
            "checkpoint.finalize() 不在 finally 里：worker 抛出时它永远不会执行，"
            "合并窗口内已经付费的译文会被丢弃",
        )


if __name__ == "__main__":
    unittest.main()
