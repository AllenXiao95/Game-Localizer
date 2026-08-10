from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import polib
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.resources.gettext import GettextAdapter
from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.config import load_project_config
from localizer.migrations.accepted_artifact import (
    AcceptedArtifactAdopter,
    AcceptedArtifactVerifier,
    ArtifactAdoptionRefused,
)


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_root = root / "source"
        self.source_root.mkdir()
        self.source = self.source_root / "menu.mo"
        catalog = polib.POFile()
        catalog.append(polib.POEntry(msgid="menu/hello", msgstr="Привет"))
        catalog.save_as_mofile(str(self.source))

        for name in ("prompt.md", "glossary.yaml", "rules.yaml"):
            (root / name).write_text(
                "schema_version: 1\nterms: []\n" if name == "glossary.yaml"
                else "schema_version: 1\n",
                encoding="utf-8",
            )
        raw = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        raw["project"] = {"id": "accepted-game", "name": "Accepted", "game_version": "1"}
        raw["paths"] = {
            "source": str(self.source_root),
            "workspace": str(root / "workspace"),
            "output": str(root / "output"),
        }
        raw["resources"]["adapters"] = [
            {
                "type": "gettext",
                "include": ["**/*.mo"],
                "options": {"layout": "keyed_source", "source_filter": "all"},
            }
        ]
        raw["prompt"] = {"template": str(root / "prompt.md")}
        raw["glossary"] = {"file": str(root / "glossary.yaml")}
        raw["rules"] = {"file": str(root / "rules.yaml")}
        raw["tm"] = {"database": str(root / "tm.sqlite3")}
        raw["build"]["encryption"] = "none"
        raw["build"].pop("password_env", None)
        raw["quality_gate"].pop("legacy_debt_baseline", None)
        self.config_path = root / "project.yaml"
        self.config_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self.config = load_project_config(self.config_path)

        self.release = root / "accepted"
        resources = self.release / "resources"
        resources.mkdir(parents=True)
        adapter = GettextAdapter(
            project_id=self.config.project.id,
            source_root=self.source_root,
            source_locale=self.config.languages.source,
            target_locale=self.config.languages.target,
            options={"layout": "keyed_source", "source_filter": "all"},
        )
        units = [replace(unit, translation="你好") for unit in adapter.extract(self.source)]
        self.accepted = resources / "menu.mo"
        adapter.render(units, self.source, self.accepted)
        artifact = self.release / "accepted.zip"
        artifact.write_bytes(b"accepted artifact fixture")

        source_hasher = sha256()
        source_hasher.update(b"menu.mo")
        source_hasher.update(self.source.read_bytes())
        descriptor = {
            "relative_path": "menu.mo",
            "sha256": sha256(self.accepted.read_bytes()).hexdigest(),
            "size": self.accepted.stat().st_size,
        }
        manifest = {
            "schema_version": 1,
            "project_id": self.config.project.id,
            "run_id": "accepted-run",
            "mode": "release",
            "quality_gate_passed": True,
            "game_version": "1",
            "created_at": "2026-08-11T00:00:00+00:00",
            "source_fingerprint": source_hasher.hexdigest(),
            "prompt_revision": sha256(self.config.prompt.template.read_bytes()).hexdigest(),
            "rules_revision": sha256(self.config.rules.file.read_bytes()).hexdigest(),
            "glossary_revision": sha256(self.config.glossary.file.read_bytes()).hexdigest(),
            "artifact": {
                "name": artifact.name,
                "sha256": sha256(artifact.read_bytes()).hexdigest(),
                "size": artifact.stat().st_size,
                "encryption": "none",
            },
            "files": [descriptor],
            "release": {"version": "1", "variant": "fixture", "slug": "fixture-v1"},
        }
        self.manifest = self.release / "accepted.manifest.json"
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def adopter(self, tm: SQLiteTranslationMemory) -> AcceptedArtifactAdopter:
        return AcceptedArtifactAdopter(self.config, tm, self.manifest)


class AcceptedArtifactBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_dry_run_maps_every_unit_without_mutating_tm(self) -> None:
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            report, entries = self.project.adopter(tm).analyze(accepted_by="owner")
            self.assertEqual("ready", report["status"])
            self.assertEqual(1, report["summary"]["accepted_units"])
            self.assertEqual("你好", entries[0].translation)
            self.assertIsNone(tm.lookup(entries[0].stable_identity))

    def test_apply_backs_up_and_writes_formal_human_rows(self) -> None:
        backup = self.project.root / "backup.sqlite3"
        report_path = self.project.root / "data-baseline.json"
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            report = self.project.adopter(tm).adopt(
                accepted_by="project-owner",
                backup_path=backup,
                report_path=report_path,
            )
            self.assertEqual("passed", report["status"])
            self.assertTrue(backup.is_file())
            self.assertTrue(report_path.is_file())
            row = tm.connection.execute("SELECT * FROM tm_entries").fetchone()
            self.assertEqual("你好", row["translation"])
            self.assertEqual("human", row["origin"])
            self.assertEqual("reviewed", row["review_state"])
            self.assertEqual("native", row["classification"])
            self.assertEqual(1, row["is_formal"])
            self.assertEqual(1, row["human_authored"])

    def test_source_fingerprint_mismatch_is_refused(self) -> None:
        catalog = polib.mofile(str(self.project.source))
        catalog[0].msgstr = "Изменено"
        catalog.save(str(self.project.source))
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            with self.assertRaisesRegex(ArtifactAdoptionRefused, "different source"):
                self.project.adopter(tm).analyze()

    def test_tampered_resource_is_refused(self) -> None:
        self.project.accepted.write_bytes(self.project.accepted.read_bytes() + b"tamper")
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            with self.assertRaisesRegex(ArtifactAdoptionRefused, "size mismatch"):
                self.project.adopter(tm).analyze()

    def test_remote_locked_human_row_refuses_the_whole_adoption(self) -> None:
        backup = self.project.root / "must-not-exist.sqlite3"
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            _report, entries = self.project.adopter(tm).analyze()
            accepted = entries[0]
            tm.upsert(
                replace(
                    accepted,
                    translation="远端定稿",
                    origin="paratranz",
                    review_state="locked",
                    stage=9,
                )
            )
            with self.assertRaisesRegex(ArtifactAdoptionRefused, "protected"):
                self.project.adopter(tm).adopt(
                    accepted_by="owner",
                    backup_path=backup,
                    report_path=self.project.root / "data.json",
                )
            row = tm.connection.execute("SELECT translation FROM tm_entries").fetchone()
            self.assertEqual("远端定稿", row["translation"])
            self.assertFalse(backup.exists())

    def test_offline_verification_reproduces_every_resource(self) -> None:
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            self.project.adopter(tm).adopt(
                accepted_by="owner",
                backup_path=self.project.root / "backup.sqlite3",
                report_path=self.project.root / "data.json",
            )
        report = AcceptedArtifactVerifier(
            self.project.config, self.project.manifest
        ).verify(
            run_id="golden-proof",
            report_path=self.project.root / "behavior.json",
        )
        self.assertEqual("passed", report["status"])
        self.assertEqual(0, report["summary"]["pending_units"])
        self.assertEqual(1, report["summary"]["resources_compared"])


class LargeHumanAttestationTests(unittest.TestCase):
    def test_project_wide_attestation_chunks_the_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                entries = [
                    TMEntry(
                        stable_identity=f"id-{index}",
                        project_id="game",
                        adapter_id="gettext",
                        relative_path="menu.mo",
                        logical_key=f"key-{index}",
                        source_text=f"source-{index}",
                        source_fingerprint=f"fp-{index}",
                        translation=f"译文-{index}",
                        origin="human",
                        review_state="reviewed",
                        match_scope="coordinate_exact",
                        classification="native",
                        quality_state="passed",
                        is_formal=True,
                        human_authored=True,
                    )
                    for index in range(1200)
                ]
                result = tm.apply_human_review(entries, reject_guarded=True)
                self.assertEqual(1200, len(result.written))


if __name__ == "__main__":
    unittest.main()
