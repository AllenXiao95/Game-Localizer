from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localizer.application.batch_orchestrator import JsonCheckpoint
from localizer.application.local_build import BuildMode
from localizer.application.translation_evidence import (
    TranslationEvidenceStore,
    aggregate_execution_metrics,
    merge_execution_records,
    normalize_execution_record,
)
from tests.test_rebuild_from_run import (
    CHILD,
    GRANDCHILD,
    PARENT,
    _Case,
    _CountingProvider,
)


class TranslationEvidenceUnitTests(unittest.TestCase):
    def test_run_ids_are_deduplicated_before_aggregation(self) -> None:
        records = merge_execution_records(
            (
                {"run_id": "p", "requests": 4, "input_tokens": 40, "output_tokens": 8},
                {"run_id": "p", "requests": 4, "input_tokens": 40, "output_tokens": 8},
                {"run_id": "r", "requests": 1, "input_tokens": 5, "output_tokens": 2},
            )
        )
        self.assertEqual(["p", "r"], [item["run_id"] for item in records])
        metrics = aggregate_execution_metrics(records)
        self.assertEqual(5, metrics["requests"])
        self.assertEqual(45, metrics["input_tokens"])
        self.assertEqual(10, metrics["output_tokens"])

    def test_resource_paths_are_union_counted_across_runs(self) -> None:
        records = (
            normalize_execution_record(
                "p",
                {"requests": 1, "translation_units_total": 10},
                translation_files=("a.po", "b.po"),
            ),
            normalize_execution_record(
                "r",
                {"requests": 1, "translation_units_total": 2},
                translation_files=("b.po", "c.po"),
            ),
        )
        metrics = aggregate_execution_metrics(records)
        self.assertEqual(3, metrics["translation_files_total"])
        self.assertEqual(12, metrics["translation_units_total"])

    def test_sidecar_round_trips_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = TranslationEvidenceStore(Path(temp))
            saved = store.save(
                "child",
                (
                    {"run_id": "parent", "requests": 3, "input_tokens": 30},
                    {"run_id": "parent", "requests": 3, "input_tokens": 30},
                ),
            )
            loaded = store.load("child")
        self.assertEqual(saved, loaded)
        self.assertEqual(("parent",), tuple(item["run_id"] for item in loaded))


class ReleaseTranslationEvidenceE2ETests(_Case):
    def _checkpoint_metrics(self, run_id: str) -> dict:
        checkpoint = JsonCheckpoint(
            self.project.root / "ws" / "runs" / run_id / "checkpoint.json"
        )
        return dict(checkpoint.metrics)

    def _manifest(self, result) -> dict:
        self.assertIsNotNone(result.build.bundle)
        return json.loads(result.build.bundle.manifest.read_text(encoding="utf-8"))

    def test_direct_release_keeps_current_and_aggregate_metrics_equal(self) -> None:
        provider = _CountingProvider()
        result = self.project.runner(provider).run(
            mode=BuildMode.RELEASE,
            run_id="direct-release",
        )
        manifest = self._manifest(result)
        aggregate = manifest["translation_metrics"]
        current = manifest["translation_metrics_current_run"]
        evidence = manifest["translation_evidence_runs"]

        self.assertGreater(aggregate["requests"], 0)
        self.assertEqual(current["requests"], aggregate["requests"])
        self.assertEqual(current["input_tokens"], aggregate["input_tokens"])
        self.assertEqual(["direct-release"], [item["run_id"] for item in evidence])
        self.assertEqual("contributing_run_execution", manifest["translation_metrics_scope"])

    def test_zero_provider_release_inherits_preview_execution_evidence(self) -> None:
        self.run_parent()
        parent_metrics = self._checkpoint_metrics(PARENT)

        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT,
            mode=BuildMode.RELEASE,
            run_id=CHILD,
        )
        self.assertEqual([], child_provider.requested)
        manifest = self._manifest(child)

        self.assertEqual(0, manifest["translation_metrics_current_run"]["requests"])
        self.assertEqual(parent_metrics["requests"], manifest["translation_metrics"]["requests"])
        self.assertEqual(
            parent_metrics["input_tokens"], manifest["translation_metrics"]["input_tokens"]
        )
        self.assertEqual([PARENT], [item["run_id"] for item in manifest["translation_evidence_runs"]])
        self.assertIn(
            f"API 调用: {parent_metrics['requests']} 次",
            child.build.bundle.release_body,
        )
        self.assertNotIn("API 调用: 0 次", child.build.bundle.release_body)
        self.assertIn("涉及翻译资源: 1 个", child.build.bundle.release_body)

    def test_multi_generation_zero_work_rebuild_does_not_multiply_parent_metrics(self) -> None:
        self.run_parent()
        parent_metrics = self._checkpoint_metrics(PARENT)

        first = self.project.runner(_CountingProvider()).rebuild_from_run(
            PARENT,
            mode=BuildMode.PREVIEW,
            run_id=CHILD,
        )
        self.assertEqual(0, self._checkpoint_metrics(CHILD)["requests"])
        self.assertIsNone(first.build.bundle)

        grandchild = self.project.runner(_CountingProvider()).rebuild_from_run(
            CHILD,
            mode=BuildMode.RELEASE,
            run_id=GRANDCHILD,
        )
        manifest = self._manifest(grandchild)
        self.assertEqual([PARENT], [item["run_id"] for item in manifest["translation_evidence_runs"]])
        self.assertEqual(parent_metrics["requests"], manifest["translation_metrics"]["requests"])
        self.assertEqual(parent_metrics["input_tokens"], manifest["translation_metrics"]["input_tokens"])

    def test_child_provider_work_is_added_once_to_reused_parent_evidence(self) -> None:
        self.run_parent(fail=("b",))
        parent_metrics = self._checkpoint_metrics(PARENT)

        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT,
            mode=BuildMode.RELEASE,
            run_id=CHILD,
        )
        self.assertEqual([self.project.identity("b")], child_provider.requested)
        manifest = self._manifest(child)
        evidence = manifest["translation_evidence_runs"]
        ids = [item["run_id"] for item in evidence]

        self.assertEqual([PARENT, CHILD], ids)
        self.assertEqual(2, len(set(ids)))
        self.assertGreater(manifest["translation_metrics_current_run"]["requests"], 0)
        self.assertEqual(
            sum(item["requests"] for item in evidence),
            manifest["translation_metrics"]["requests"],
        )
        self.assertGreater(
            manifest["translation_metrics"]["requests"], parent_metrics["requests"]
        )


if __name__ == "__main__":
    unittest.main()
