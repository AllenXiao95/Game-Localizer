"""运行时 I/O 与观测面板刷新的可靠性。

两个实测出来的问题：

1. **写放大**：JsonCheckpoint 每条结果都整文件重写 + fsync + 原子 replace。
   实测 4001 条时单次 flush 从 2.7ms 涨到 12.2ms（文件 506 KB），
   累计写入约 989 MB —— O(n²)。

2. **读侧脆弱**：面板的 _read_json 没有重试。写侧 replace 的一瞬间目标路径
   短暂不可读；实测 4 worker 并发写 + 轮询读，4 秒内 256 次读取有 1 次读到空
   （0.4%）。面板每 5 秒刷一次，撞上就整块闪成「暂无数据」，看起来像运行挂了。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.batch_orchestrator import JsonCheckpoint, UnitResult
from localizer.web.collector import _read_json


class CheckpointWriteCoalescingTests(unittest.TestCase):
    def _checkpoint(self, temp: Path, **kwargs) -> JsonCheckpoint:
        return JsonCheckpoint(temp / "checkpoint.json", **kwargs)

    def test_high_frequency_results_are_coalesced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = self._checkpoint(root, min_flush_interval=10.0)
            for index in range(200):
                checkpoint.record_result(
                    UnitResult(f"id-{index}", f"译文{index}", "succeeded")
                )
            # 合并窗口内只应落盘一次（第一条），其余挂起。
            on_disk = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertLess(
                len(on_disk["units"]), 200,
                msg="逐条落盘会把整轮 I/O 放大成几百 MB",
            )
            self.assertTrue(checkpoint._pending_flush)  # noqa: SLF001

    def test_finalize_never_loses_the_last_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = self._checkpoint(root, min_flush_interval=10.0)
            for index in range(200):
                checkpoint.record_result(
                    UnitResult(f"id-{index}", f"译文{index}", "succeeded")
                )
            checkpoint.finalize()
            on_disk = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
        # 丢窗口只意味着恢复时重译几条，但收尾必须补齐 —— 否则每次运行都白扔一截。
        self.assertEqual(200, len(on_disk["units"]))

    def test_batch_level_events_are_never_deferred(self) -> None:
        # 面板靠批次状态展示进度。逐条结果可以合并，批次转换不行，
        # 否则进度会滞后整整一个批次，看起来像卡住了。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = self._checkpoint(root, min_flush_interval=10.0)
            checkpoint.record_result(UnitResult("id-0", "x", "succeeded"))
            checkpoint.record_result(UnitResult("id-1", "y", "succeeded"))
            checkpoint.record_batch(["id-0", "id-1"], "succeeded")
            on_disk = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
        self.assertEqual(2, len(on_disk["units"]))
        self.assertTrue(on_disk["batches"])

    def test_resume_still_sees_flushed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._checkpoint(root, min_flush_interval=10.0)
            first.record_result(UnitResult("id-0", "已完成", "succeeded"))
            first.finalize()
            resumed = self._checkpoint(root)
        self.assertEqual("已完成", resumed.succeeded("id-0"))

    def test_zero_interval_keeps_the_old_write_every_time_behaviour(self) -> None:
        # 需要逐条持久化的场景（比如调试）仍可显式关掉合并。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = self._checkpoint(root, min_flush_interval=0.0)
            for index in range(20):
                checkpoint.record_result(UnitResult(f"id-{index}", "x", "succeeded"))
            on_disk = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
        self.assertEqual(20, len(on_disk["units"]))


class DashboardReadResilienceTests(unittest.TestCase):
    def test_transient_replace_window_does_not_surface_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            checkpoint = JsonCheckpoint(path, min_flush_interval=0.0)
            for index in range(300):
                checkpoint.record_result(UnitResult(f"seed-{index}", "预置" * 8, "succeeded"))

            stop = threading.Event()
            misses = {"count": 0, "total": 0}

            def writer() -> None:
                index = 0
                while not stop.is_set():
                    checkpoint.record_result(
                        UnitResult(f"w-{index}", "译文" * 8, "succeeded")
                    )
                    index += 1

            def reader() -> None:
                while not stop.is_set():
                    misses["total"] += 1
                    if _read_json(path) is None:
                        misses["count"] += 1
                    time.sleep(0.002)

            threads = [threading.Thread(target=writer) for _ in range(4)]
            threads.append(threading.Thread(target=reader))
            for thread in threads:
                thread.start()
            time.sleep(1.5)
            stop.set()
            for thread in threads:
                thread.join(timeout=10)

        self.assertGreater(misses["total"], 10, "读取次数太少，这条断言没有意义")
        self.assertEqual(
            0, misses["count"],
            msg=f"{misses['count']}/{misses['total']} 次读取落空；"
                f"面板会闪成「暂无数据」，看起来像运行挂了",
        )

    def test_a_genuinely_absent_file_still_reports_missing(self) -> None:
        # 重试是为了扛住替换窗口，不能把真正的缺失也吞掉。
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(_read_json(Path(temp) / "nope.json"))

    def test_corrupt_content_still_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(_read_json(path))

    def test_retry_budget_is_bounded(self) -> None:
        # 重试不能把面板的一次刷新拖成秒级。
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "absent.json"
            start = time.perf_counter()
            _read_json(path)
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)



class CheckpointWriteAmplificationTests(unittest.TestCase):
    """checkpoint 写放大（评估 R06，部分修复）。

    实测（10 文件、batch=16）修复前后：

        词条    flush 次数        累计写入
        1000    441 -> 161     72.8 -> 19.0 MB
        2000    801 -> 281    256.4 -> 63.0 MB
        4000   1521 -> 521    958.3 -> 227.5 MB

    两处改动：identities 只在 `planned` 事件写一次（消费方只用 len）；
    submitted/received/parsed/validated 四个纯观测态从 force 降为合并写。

    **仍然是二次增长**（比值 3.3~3.6，2.0=线性）：根因是每次 flush 都整份重写
    `batches` 数组。彻底解决需要把事件流拆成 append-only JSONL，那是独立的一步。
    这组测试守住已经拿到的那部分，防止回退。
    """

    def _run(self, units: int, files: int, batch: int = 16):
        import json as _json

        from localizer.infrastructure.atomic_io import AtomicIO

        written = {"bytes": 0, "flushes": 0}
        original = AtomicIO.write_json

        def counting(target, value, **kwargs):
            payload = _json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            written["bytes"] += len(payload.encode("utf-8"))
            written["flushes"] += 1
            return original(target, value, **kwargs)

        AtomicIO.write_json = staticmethod(counting)
        try:
            with tempfile.TemporaryDirectory() as temp:
                checkpoint = JsonCheckpoint(Path(temp) / "checkpoint.json")
                per = units // files
                resource_units = {
                    f"f{i}.mo": [f"id-{i}-{j}" for j in range(per)] for i in range(files)
                }
                checkpoint.configure_run(
                    translation_units_total=units,
                    translation_files_total=files,
                    resource_units=resource_units,
                )
                for path, ids in resource_units.items():
                    checkpoint.start_resource("w0", path, ids)
                    for start in range(0, len(ids), batch):
                        chunk = ids[start : start + batch]
                        batch_id = checkpoint.start_batch(
                            chunk, resource_path=path, worker_id="w0"
                        )
                        for state in ("submitted", "received", "parsed", "validated",
                                      "succeeded"):
                            checkpoint.record_batch(
                                chunk, state, batch_id=batch_id,
                                resource_path=path, worker_id="w0",
                            )
                        for identity in chunk:
                            checkpoint.record_result(
                                UnitResult(identity, "译文", "succeeded", ())
                            )
                    checkpoint.complete_resource("w0", path)
                checkpoint.finalize()
        finally:
            AtomicIO.write_json = staticmethod(original)
        return written

    def test_pure_observation_states_do_not_force_a_flush(self) -> None:
        # 复刻 20260802 规模：96 文件 × 6 次强制批次状态曾贡献 769 次 flush 里的 576 次。
        written = self._run(768, 96)
        self.assertLess(
            written["flushes"], 500,
            "纯观测态又开始强制落盘了：96 文件规模下 flush 次数应在 400 上下",
        )

    def test_identities_are_written_once_per_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = JsonCheckpoint(Path(temp) / "checkpoint.json")
            batch_id = checkpoint.start_batch(
                ["a", "b", "c"], resource_path="f.mo", worker_id="w0"
            )
            for state in ("submitted", "succeeded"):
                checkpoint.record_batch(
                    ["a", "b", "c"], state, batch_id=batch_id,
                    resource_path="f.mo", worker_id="w0",
                )
        with_identities = [e for e in checkpoint.batches if "identities" in e]
        self.assertEqual(1, len(with_identities))
        self.assertEqual("planned", with_identities[0]["state"])
        # 消费方靠 size，所以每条事件都必须有它。
        self.assertTrue(all(e.get("size") == 3 for e in checkpoint.batches))

    def test_dashboard_reads_size_from_both_old_and_new_events(self) -> None:
        from localizer.web.collector import _batch_size

        self.assertEqual(3, _batch_size({"size": 3}))
        # schema v1/v2 的旧 checkpoint 只有 identities。
        self.assertEqual(2, _batch_size({"identities": ["a", "b"]}))
        self.assertEqual(0, _batch_size({}))

    def test_write_volume_stayed_down(self) -> None:
        written = self._run(2000, 10)
        megabytes = written["bytes"] / 1048576
        # 修复前 256.4 MB，修复后 63.0 MB。留一倍余量防止环境抖动误报。
        self.assertLess(megabytes, 130, f"写放大回退了：{megabytes:.1f} MB")

if __name__ == "__main__":
    unittest.main()
