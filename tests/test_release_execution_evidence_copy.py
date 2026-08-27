from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localizer.application.artifact import ArtifactBuilder


class ReleaseExecutionEvidenceCopyTests(unittest.TestCase):
    def _build(self, metadata: dict):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "resources"
        root.mkdir()
        resource = root / "ui.po"
        resource.write_text("msgid \\\"hello\\\"\nmsgstr \\\"你好\\\"\n", encoding="utf-8")
        output = Path(temp.name) / "release"
        return ArtifactBuilder().build_release(
            project_id="demo",
            run_id="release-copy-test",
            resource_root=root,
            resource_paths=[resource],
            destination=output,
            manifest_metadata=metadata,
            version="1.0.0",
            variant="RU",
            artifact_prefix="demo",
        )

    def test_contributing_run_metrics_are_labeled_as_execution_evidence(self) -> None:
        bundle = self._build(
            {
                "translation_metrics_scope": "contributing_run_execution",
                "translation_metrics": {
                    "requests": 3,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "translation_units_total": 12,
                    "translation_files_total": 1,
                },
                "translation_evidence_runs": [
                    {"run_id": "preview-a"},
                    {"run_id": "release-b"},
                ],
            }
        )
        body = bundle.release_body
        self.assertIn("翻译执行证据", body)
        self.assertIn("按贡献 Provider 运行聚合", body)
        self.assertIn("不代表最终制品的精确去重词条数或生命周期成本", body)
        self.assertIn("贡献 Provider 运行: 2 个", body)
        self.assertIn("API 调用: 3 次", body)
        self.assertIn("Provider 词条执行范围（累计）: 12 条", body)
        self.assertIn("涉及翻译资源: 1 个", body)
        self.assertIn("消耗 token: 120", body)
        self.assertNotIn("翻译条数: 12", body)

    def test_legacy_metrics_without_scope_keep_legacy_copy(self) -> None:
        bundle = self._build(
            {
                "translation_metrics": {
                    "requests": 1,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "translation_units_total": 4,
                }
            }
        )
        self.assertIn("API 调用: 1 次", bundle.release_body)
        self.assertIn("翻译条数: 4", bundle.release_body)
        self.assertNotIn("翻译执行证据", bundle.release_body)


if __name__ == "__main__":
    unittest.main()
