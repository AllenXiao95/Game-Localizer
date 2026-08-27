from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localizer.application.batch_orchestrator import JsonCheckpoint, UnitResult
from localizer.application.project_runner import ProjectRunner
from localizer.domain.translation_unit import TranslationUnit


def _unit(path: str, key: str, work: int) -> TranslationUnit:
    return TranslationUnit(
        project_id="queue-test",
        adapter_id="gettext",
        relative_path=path,
        logical_key=key,
        source_text="x" * work,
        source_locale="ru-RU",
        target_locale="zh-Hans",
    )


def _makespan(workloads, workers: int = 2) -> int:
    loads = [0] * workers
    for work in workloads:
        index = min(range(workers), key=lambda item: (loads[item], item))
        loads[index] += work
    return max(loads)


class ResourceQueueSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.checkpoint = JsonCheckpoint(Path(self._temp.name) / "checkpoint.json")

    @staticmethod
    def _groups(specs):
        # ResourceBuild 本身不参与排序判据；这里只需保持和 ProjectRunner 相同的
        # `(resource, group)` 形状，验证排序不会偷改 execution group。
        return tuple(
            (object(), tuple(_unit(path, f"{path}-{index}", work) for index, work in enumerate(works)))
            for path, works in specs
        )

    def test_largest_remaining_resource_is_queued_first(self) -> None:
        groups = self._groups(
            (("a.po", (10,)), ("b.po", (10,)), ("c.po", (10,)), ("d.po", (100,)))
        )
        ordered = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=len,
        )
        self.assertEqual(
            ["d.po", "a.po", "b.po", "c.po"],
            [item.relative_path for item in ordered],
        )
        self.assertEqual([100, 10, 10, 10], [item.estimated_work for item in ordered])

    def test_checkpoint_succeeded_units_are_excluded_from_score_not_execution_group(self) -> None:
        groups = self._groups(
            (("a.po", (995, 5)), ("b.po", (180,)))
        )
        succeeded = groups[0][1][0]
        self.checkpoint.record_result(
            UnitResult(succeeded.stable_identity, "already translated", "succeeded")
        )

        ordered = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=len,
        )
        self.assertEqual(["b.po", "a.po"], [item.relative_path for item in ordered])
        a_item = next(item for item in ordered if item.relative_path == "a.po")
        self.assertEqual(5, a_item.estimated_work)
        self.assertEqual(1, a_item.remaining_units)
        # 排序视图忽略已完成单元，但真正交给 BatchOrchestrator 的 group 必须仍完整。
        self.assertEqual(2, len(a_item.group))
        self.assertEqual(succeeded.stable_identity, a_item.group[0].stable_identity)

    def test_equal_workloads_use_stable_path_tie_breaker(self) -> None:
        groups = self._groups(
            (("z.po", (10,)), ("a.po", (10,)), ("m.po", (10,)))
        )
        first = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=len,
        )
        second = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=len,
        )
        self.assertEqual(
            ["a.po", "m.po", "z.po"],
            [item.relative_path for item in first],
        )
        self.assertEqual(
            [item.relative_path for item in first],
            [item.relative_path for item in second],
        )
        # 排序后仍保留 discovery/resource 的原始 index，供结果按原逻辑归位。
        self.assertEqual([1, 2, 0], [item.original_index for item in first])

    def test_token_counter_failure_falls_back_to_remaining_unit_count(self) -> None:
        groups = self._groups(
            (("a.po", (50, 50, 50)), ("b.po", (500,)))
        )

        def broken_counter(_text: str) -> int:
            raise RuntimeError("counter unavailable")

        ordered = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=broken_counter,
        )
        self.assertEqual(["a.po", "b.po"], [item.relative_path for item in ordered])
        self.assertEqual([3, 1], [item.estimated_work for item in ordered])
        self.assertTrue(all(item.estimate_kind == "pending_units" for item in ordered))

    def test_target_fixture_has_lower_makespan_than_fifo(self) -> None:
        groups = self._groups(
            (("a.po", (10,)), ("b.po", (10,)), ("c.po", (10,)), ("d.po", (100,)))
        )
        ordered = ProjectRunner._ordered_resource_queue(
            groups,
            checkpoint=self.checkpoint,
            token_counter=len,
        )
        fifo_work = [10, 10, 10, 100]
        largest_first_work = [item.estimated_work for item in ordered]
        self.assertEqual(110, _makespan(fifo_work))
        self.assertEqual(100, _makespan(largest_first_work))
        self.assertLess(_makespan(largest_first_work), _makespan(fifo_work))


if __name__ == "__main__":
    unittest.main()
