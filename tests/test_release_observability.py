"""发布侧的两个观测缺口（M6）。

`docs/framework-implementation.md` 的 M6 行原文：

> GitHub/R2 的对象级幂等跳过已生效，但 WebUI 尚未展示 `objects[].skipped`；
> Manifest 已携带本次请求/词条/Token metrics 并生成基础 GitHub Release 说明，
> 完整批次与增量谱系概览仍待补全。

这两条都是「数据早就在，只是没人把它拿出来」，但缺了之后各自会导致一个具体的
误判：

- `skipped` 不显示 → 一次补发全部对象都命中幂等跳过，面板照样显示 `ok`。
  操作者以为「重传成功了」，实际上一个字节都没动。
- 批次概览缺失 → 2026-08-04 那次 97/98 个失败全部来自**一个** 97 词条批次撞
  读超时；Manifest 里只有 requests/tokens 两个总量，完全看不出曾经缩过批、
  缩到多小。复盘只能去翻 workspace 里的 checkpoint。
- 谱系只到直接父运行 → 连续两代重建之后，「这个包到底基于哪几轮的钱」
  需要人手一层层去翻 task-request.json。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.artifact import _batch_lines, _lineage_lines
from localizer.application.batch_orchestrator import JsonCheckpoint

INDEX = ROOT / "src" / "localizer" / "web" / "static" / "index.html"


class BatchSummaryTests(unittest.TestCase):
    def _checkpoint(self) -> JsonCheckpoint:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        return JsonCheckpoint(Path(self._temp.name) / "checkpoint.json")

    def test_an_untouched_run_summarises_to_zeroes(self) -> None:
        summary = self._checkpoint().batch_summary()
        self.assertEqual(0, summary["planned"])
        self.assertEqual(0, summary["largest_batch"])

    def test_sizes_come_from_the_size_field_not_identities(self) -> None:
        """尺寸必须取 `size`，`identities` 只是老 checkpoint 的兜底。

        这条测试原来是**恒真**的：`record_batch` 在 planned 事件上两个字段都写，
        所以读哪个都一样，把实现改回 `len(identities)` 照样全绿。真正要守的是
        「planned 事件没有 identities 时仍然算得对」—— `identities` 是写放大的
        主要来源，随时可能被去掉，那天起「最小批次」会永远是 0 且不报错。
        """
        checkpoint = self._checkpoint()
        checkpoint.record_batch(["u"] * 97, "planned", batch_id="b1")
        # 模拟一条只带 size、不带 identities 的 planned 事件。
        checkpoint.batches[-1].pop("identities")
        checkpoint.record_batch(["u"] * 48, "planned", batch_id="b2")
        checkpoint.batches[-1].pop("identities")
        summary = checkpoint.batch_summary()
        self.assertEqual(97, summary["largest_batch"])
        self.assertEqual(48, summary["smallest_batch"])
        self.assertEqual(145, summary["units_planned"])

    def test_an_old_checkpoint_without_size_still_reports_sizes(self) -> None:
        """`size` 之前写的 checkpoint 只有 identities，不能因此退化成 0。"""
        checkpoint = self._checkpoint()
        checkpoint.record_batch(["u"] * 12, "planned", batch_id="b1")
        checkpoint.batches[-1].pop("size")
        self.assertEqual(12, checkpoint.batch_summary()["largest_batch"])

    def test_state_counts_are_unaffected_by_missing_identities(self) -> None:
        checkpoint = self._checkpoint()
        checkpoint.record_batch([f"u{i}" for i in range(97)], "planned", batch_id="b1")
        checkpoint.record_batch([f"u{i}" for i in range(97)], "split_required", batch_id="b1")
        checkpoint.record_batch([f"u{i}" for i in range(48)], "planned", batch_id="b2")
        checkpoint.record_batch([f"u{i}" for i in range(48)], "succeeded", batch_id="b2")
        summary = checkpoint.batch_summary()
        self.assertEqual(2, summary["planned"])
        self.assertEqual(1, summary["split_required"])
        self.assertEqual(1, summary["succeeded"])
        self.assertEqual(97, summary["largest_batch"])
        self.assertEqual(48, summary["smallest_batch"])
        self.assertEqual(145, summary["units_planned"])

    def test_summary_survives_a_reload(self) -> None:
        checkpoint = self._checkpoint()
        checkpoint.record_batch(["a", "b"], "planned", batch_id="b1")
        checkpoint.flush_now()
        reloaded = JsonCheckpoint(checkpoint.path)
        self.assertEqual(1, reloaded.batch_summary()["planned"])
        self.assertEqual(2, reloaded.batch_summary()["largest_batch"])


class ReleaseNoteTests(unittest.TestCase):
    def test_a_zero_provider_rebuild_prints_no_batch_section(self) -> None:
        """全部失败都已人工修复时 Provider 请求为 0，硬打一堆 0 只会稀释说明。"""
        self.assertEqual([], _batch_lines({}))
        self.assertEqual([], _batch_lines(None))
        self.assertEqual([], _batch_lines({"planned": 0, "largest_batch": 0}))

    def test_a_split_run_is_visible_in_the_notes(self) -> None:
        lines = _batch_lines(
            {
                "planned": 3,
                "succeeded": 2,
                "split_required": 1,
                "failed": 1,
                "largest_batch": 97,
                "smallest_batch": 48,
            }
        )
        joined = "\n".join(lines)
        self.assertIn("缩批次数: 1", joined)
        self.assertIn("48–97", joined)

    def test_a_uniform_run_does_not_claim_a_range(self) -> None:
        lines = _batch_lines(
            {"planned": 2, "largest_batch": 16, "smallest_batch": 16}
        )
        self.assertIn("批次规模: 16 词条", "\n".join(lines))

    def test_a_plain_run_prints_no_lineage(self) -> None:
        self.assertEqual([], _lineage_lines(None))

    def test_the_whole_ancestor_chain_is_printed(self) -> None:
        lines = _lineage_lines(
            {
                "parent_run_id": "gen2",
                "lineage": ["gen2", "gen1", "gen0"],
                "reused": 1333,
                "retried": 94,
                "resolved_by_human": 3,
            }
        )
        joined = "\n".join(lines)
        self.assertIn("gen2 ← gen1 ← gen0", joined)
        self.assertIn("1333", joined)
        self.assertIn("人工定稿: 3", joined)

    def test_lineage_without_a_chain_still_reports_the_counts(self) -> None:
        lines = _lineage_lines({"reused": 0, "retried": 5, "resolved_by_human": 0})
        self.assertEqual(1, len(lines))
        self.assertIn("重试: 5", lines[0])


class LineageWalkerTests(unittest.TestCase):
    def _runner(self, root: Path):
        from tests.test_rebuild_from_run import _Project

        project = _Project(root)
        from localizer.application.project_runner import ProjectRunner

        return ProjectRunner(project.config())

    def test_the_chain_stops_where_the_snapshot_stops(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self._runner(root)
            runs = runner.config.paths.workspace / "runs"
            for child, parent in (("c", "b"), ("b", "a")):
                (runs / child).mkdir(parents=True, exist_ok=True)
                (runs / child / "task-request.json").write_text(
                    json.dumps({"parent_run_id": parent}), encoding="utf-8"
                )
            (runs / "a").mkdir(parents=True, exist_ok=True)
            self.assertEqual(("c", "b", "a"), runner._run_lineage("c"))

    def test_a_cycle_does_not_hang(self) -> None:
        """谱系是外部文件写的，不能假设它一定是棵树。"""
        import json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self._runner(root)
            runs = runner.config.paths.workspace / "runs"
            for child, parent in (("x", "y"), ("y", "x")):
                (runs / child).mkdir(parents=True, exist_ok=True)
                (runs / child / "task-request.json").write_text(
                    json.dumps({"parent_run_id": parent}), encoding="utf-8"
                )
            self.assertEqual(("x", "y"), runner._run_lineage("x"))

    def test_unreadable_snapshots_do_not_raise(self) -> None:
        """谱系是说明性信息，不是闸门。读不了就到此为止。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self._runner(root)
            runs = runner.config.paths.workspace / "runs"
            (runs / "z").mkdir(parents=True, exist_ok=True)
            (runs / "z" / "task-request.json").write_text("{ not json", encoding="utf-8")
            self.assertEqual(("z",), runner._run_lineage("z"))


class DashboardShowsSkippedObjectsTests(unittest.TestCase):
    """面板对 `objects[].skipped` 的计算必须**真的跑一遍**。

    这一组原本只断言源码里出现过 `o.skipped`、"上传"、"跳过" —— 于是把
    `filter((o) => o.skipped)` 写成 `filter((o) => !o.skipped)`（上传与跳过
    完全对调）依然全绿，而面板会把「实际上传 2、跳过 1」显示成
    「上传 1、跳过 2」。那种断言测的是"这段代码提到过这个词"，不是"它算得对"。

    面板是纯静态单文件（server.py 的 `_static` 直出），没有构建步骤，所以
    这里从 index.html 里抠出 `publishSummary` 用 node 执行。没有 node 就跳过 ——
    宁可少一条测试，也不要退回那种恒真断言。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")

    def _run(self, targets):
        if not self.node:
            self.skipTest("需要 node 才能真正执行面板脚本")
        source = INDEX.read_text(encoding="utf-8")
        start = source.index("function publishSummary(")
        end = source.index("\nfunction renderTaskStatus()", start)
        script = (
            source[start:end]
            + "\nconsole.log(JSON.stringify("
            + json.dumps(targets, ensure_ascii=False)
            + ".map(publishSummary)));"
        )
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_moved_and_skipped_are_not_swapped(self) -> None:
        """3 个对象、跳过 1 个 → 上传 2、跳过 1。

        这条就是那次对调的直接判据：写反了它会得到「上传 1/跳过 2」。
        """
        (line,) = self._run(
            [
                {
                    "target": "github",
                    "status": "ok",
                    "objects": [
                        {"skipped": False},
                        {"skipped": False},
                        {"skipped": True},
                    ],
                }
            ]
        )
        self.assertEqual("github:ok(上传 2/跳过 1)", line)

    def test_a_fully_idempotent_republish_reads_as_zero_uploads(self) -> None:
        """全部命中幂等跳过时必须显示"上传 0" —— 这正是"看到 ok 却什么都没做"。"""
        (line,) = self._run(
            [
                {
                    "target": "r2",
                    "status": "ok",
                    "objects": [{"skipped": True}, {"skipped": True}],
                }
            ]
        )
        self.assertEqual("r2:ok(上传 0/跳过 2)", line)

    def test_a_first_publish_reads_as_zero_skips(self) -> None:
        (line,) = self._run(
            [
                {
                    "target": "r2",
                    "status": "ok",
                    "objects": [{"skipped": False}, {"skipped": False}],
                }
            ]
        )
        self.assertEqual("r2:ok(上传 2/跳过 0)", line)

    def test_a_failed_target_without_objects_keeps_the_old_format(self) -> None:
        # 失败的 target 没有 receipt；显示「上传 0/跳过 0」会让人以为它跑过了。
        (line,) = self._run([{"target": "oss", "status": "failed", "objects": []}])
        self.assertEqual("oss:failed", line)

    def test_a_failed_target_shows_the_actionable_reason(self) -> None:
        (line,) = self._run(
            [{
                "target": "github_release",
                "status": "failed",
                "error_message": "credential environment variable is unset",
                "objects": [],
            }]
        )
        self.assertEqual(
            "github_release:failed（credential environment variable is unset）",
            line,
        )

    def test_a_receipt_predating_the_field_counts_everything_as_uploaded(self) -> None:
        """更旧的回执有 objects 但没有 skipped 字段。

        undefined 当成"没跳过"是唯一不会误导的降级：老回执本来就没有幂等跳过。
        """
        (line,) = self._run(
            [{"target": "local", "status": "ok", "objects": [{}, {}]}]
        )
        self.assertEqual("local:ok(上传 2/跳过 0)", line)

    def test_multiple_targets_are_joined(self) -> None:
        lines = self._run(
            [
                {"target": "a", "status": "ok", "objects": [{"skipped": True}]},
                {"target": "b", "status": "failed", "objects": []},
            ]
        )
        self.assertEqual(["a:ok(上传 0/跳过 1)", "b:failed"], lines)

    def test_the_helper_is_a_top_level_function_not_inlined_again(self) -> None:
        """内联回 renderTaskStatus 就没法执行地测了 —— 那正是当初漏掉的原因。"""
        source = INDEX.read_text(encoding="utf-8")
        self.assertRegex(source, r"\nfunction publishSummary\(")
        self.assertIn("publishSummary(x)", source)


class ManifestCarriesTheSummaryTests(unittest.TestCase):
    """端到端：真跑一轮，Manifest 里必须有这两段。"""

    def test_release_manifest_has_batch_summary_and_metrics(self) -> None:
        import json

        from tests.test_rebuild_from_run import _CountingProvider, _Project
        from localizer.application.local_build import BuildMode

        with tempfile.TemporaryDirectory() as temp:
            project = _Project(Path(temp))
            result = project.runner(_CountingProvider()).run(
                mode=BuildMode.RELEASE, run_id="rel-1"
            )
            payload = json.loads(
                result.build.bundle.manifest.read_text(encoding="utf-8")
            )
        self.assertIn("batch_summary", payload)
        self.assertGreaterEqual(payload["batch_summary"]["planned"], 1)
        # 发布说明必须真的把它印出来，而不是只躺在 manifest 里。
        body = payload["release"]["body"]
        self.assertRegex(body, r"批次总数: \d+")
        self.assertRegex(body, r"批次规模: .*词条")


if __name__ == "__main__":
    unittest.main()


class LineageReachesTheReleaseNotesTests(unittest.TestCase):
    """R25 声称交付的是「整条祖先链可见」，但**没有一条断言证明它真的发生过**。

    `_run_lineage()` 与 `_lineage_lines()` 各自有单元测试，把两者接起来的那段
    （runner 把 lineage 写进 manifest → artifact 把它印进 release_body）却没有。
    对抗性审查实测：那段整个删掉，592 项照样全绿。
    """

    def test_two_generations_of_rebuild_print_the_whole_chain(self) -> None:
        from tests.test_rebuild_from_run import _CountingProvider, _Project
        from localizer.application.local_build import BuildMode

        with tempfile.TemporaryDirectory() as temp:
            project = _Project(Path(temp))
            project.runner(_CountingProvider(fail=("b",))).run(
                mode=BuildMode.PREVIEW, run_id="gen0"
            )
            project.runner(_CountingProvider()).rebuild_from_run(
                "gen0", mode=BuildMode.PREVIEW, run_id="gen1"
            )
            result = project.runner(_CountingProvider()).rebuild_from_run(
                "gen1", mode=BuildMode.RELEASE, run_id="gen2"
            )
            payload = json.loads(
                result.build.bundle.manifest.read_text(encoding="utf-8")
            )

        # manifest 里带整条链，不只是直接父运行。
        self.assertEqual(["gen1", "gen0"], payload["rebuild"]["lineage"])
        # 而且真的印进了发布说明 —— 这一段才是此前完全没被覆盖的接线。
        body = payload["release"]["body"]
        self.assertIn("增量谱系: gen1 ← gen0", body)
        self.assertRegex(body, r"复用父运行译文: \d+ 条")

    def test_a_plain_release_has_no_lineage_section(self) -> None:
        from tests.test_rebuild_from_run import _CountingProvider, _Project
        from localizer.application.local_build import BuildMode

        with tempfile.TemporaryDirectory() as temp:
            project = _Project(Path(temp))
            result = project.runner(_CountingProvider()).run(
                mode=BuildMode.RELEASE, run_id="plain"
            )
            payload = json.loads(
                result.build.bundle.manifest.read_text(encoding="utf-8")
            )
        self.assertNotIn("rebuild", payload)
        self.assertNotIn("增量谱系", payload["release"]["body"])
