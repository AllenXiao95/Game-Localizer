from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import polib

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.resources.gettext import GettextAdapter
from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.application.artifact import ReleaseBundle
from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    ResourceBuild,
)
from localizer.application.quality_gate import QualityGateError


def build_mo(path: Path) -> None:
    catalog = polib.POFile()
    catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
    catalog.append(polib.POEntry(msgid="Привет %s", msgstr="旧译 %s"))
    catalog.save_as_mofile(str(path))


def staged_entry(unit, run_id: str) -> TMEntry:
    return TMEntry(
        stable_identity=unit.stable_identity,
        project_id=unit.project_id,
        adapter_id=unit.adapter_id,
        relative_path=unit.relative_path,
        logical_key=unit.logical_key,
        source_text=unit.source_text,
        source_fingerprint=unit.source_fingerprint,
        translation="你好 %s",
        origin="machine",
        review_state="unreviewed",
        match_scope="coordinate_exact",
        run_id=run_id,
        quality_state="passed",
        is_formal=False,
    )


class LocalBuildPipelineTests(unittest.TestCase):
    def fixture(self, root: Path):
        source_root = root / "source"
        source = source_root / "locale" / "messages.mo"
        source.parent.mkdir(parents=True)
        build_mo(source)
        adapter = GettextAdapter(
            project_id="wot",
            source_root=source_root,
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        units = tuple(adapter.extract(source))
        return source, adapter, units

    def test_preview_writes_reports_and_output_but_no_manifest_or_formal_tm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, adapter, source_units = self.fixture(root)
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                tm.upsert(staged_entry(source_units[0], "run-preview"))
                result = LocalBuildPipeline().build(
                    [ResourceBuild(adapter, source, source_units)],
                    {source_units[0].stable_identity: "仍有 Кириллица"},
                    mode=BuildMode.PREVIEW,
                    project_id="wot",
                    run_id="run-preview",
                    output_root=root / "output",
                    tm=tm,
                )
                self.assertIsNone(result.bundle)
                self.assertTrue(result.rendered[0].exists())
                self.assertTrue(result.qa_json.exists())
                self.assertGreater(
                    json.loads(result.qa_json.read_text("utf-8"))["summary"]["error_count"],
                    0,
                )
                self.assertIsNone(tm.lookup(source_units[0].stable_identity))
            self.assertEqual([], list((root / "output").rglob("*.manifest.json")))

    def test_release_is_zero_tolerance_and_does_not_render_failed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, adapter, source_units = self.fixture(root)
            with self.assertRaises(QualityGateError):
                LocalBuildPipeline().build(
                    [ResourceBuild(adapter, source, source_units)],
                    {source_units[0].stable_identity: "缺少占位符"},
                    mode=BuildMode.RELEASE,
                    project_id="wot",
                    run_id="blocked",
                    output_root=root / "output",
                )
            self.assertEqual([], list((root / "output").rglob("*.zip")))
            self.assertEqual([], list((root / "output").rglob("*.manifest.json")))

    def test_reviewed_glossary_violation_blocks_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, adapter, source_units = self.fixture(root)
            pipeline = LocalBuildPipeline(
                glossary_terms=[
                    GlossaryTerm(
                        source="Привет",
                        target="您好",
                        status="reviewed",
                        provenance="human",
                    )
                ]
            )
            with self.assertRaises(QualityGateError):
                pipeline.build(
                    [ResourceBuild(adapter, source, source_units)],
                    {source_units[0].stable_identity: "你好 %s"},
                    mode=BuildMode.RELEASE,
                    project_id="wot",
                    run_id="glossary-blocked",
                    output_root=root / "output",
                )
            report = next((root / "output").rglob("qa-report.json"))
            codes = {
                issue["code"]
                for issue in json.loads(report.read_text("utf-8"))["issues"]
            }
            self.assertIn("glossary_violation", codes)

    def test_release_builds_verified_bundle_then_promotes_tm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, adapter, source_units = self.fixture(root)
            identity = source_units[0].stable_identity
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                tm.upsert(staged_entry(source_units[0], "release-1"))
                result = LocalBuildPipeline().build(
                    [ResourceBuild(adapter, source, source_units)],
                    {identity: "你好 %s"},
                    mode=BuildMode.RELEASE,
                    project_id="wot",
                    run_id="release-1",
                    output_root=root / "output",
                    tm=tm,
                )
                self.assertIsNotNone(result.bundle)
                result.bundle.verify()
                loaded = ReleaseBundle.load(result.bundle.manifest)
                loaded.verify()
                self.assertTrue(tm.lookup(identity).is_formal)
                with zipfile.ZipFile(result.bundle.artifact) as archive:
                    self.assertEqual(["locale/messages.mo"], archive.namelist())


if __name__ == "__main__":
    unittest.main()
