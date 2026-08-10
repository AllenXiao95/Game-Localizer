from __future__ import annotations

import re
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import polib

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import ProjectRunner
from localizer.config.models import ProjectConfig
from localizer.migrations.legacy_tm import LegacyTMSynchronizer
from localizer.ports.provider import ProviderResponse


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.resource_batches = []

    def translate(self, prompt, units):
        self.calls += 1
        self.resource_batches.append(
            tuple(sorted({unit.relative_path for unit in units}))
        )
        values = []
        for unit in units:
            token = re.search(r"\[PH_[0-9a-f]{8}_\d+\]", unit.source_text)
            suffix = f" {token.group(0)}" if token else ""
            values.append(f"[{len(values) + 1}] 你好{suffix}")
        return ProviderResponse("\n".join([*values, "---END---"]))


class ConcurrentFakeProvider(FakeProvider):
    def __init__(self, expected_parallelism: int) -> None:
        super().__init__()
        self.expected_parallelism = expected_parallelism
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._all_active = threading.Event()

    def translate(self, prompt, units):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= self.expected_parallelism:
                self._all_active.set()
        self._all_active.wait(timeout=2)
        try:
            return super().translate(prompt, units)
        finally:
            with self._lock:
                self.active -= 1


class ProjectRunnerE2ETests(unittest.TestCase):
    def test_provider_batches_do_not_cross_resource_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "source"
            source_root.mkdir()
            sources = (
                ("one.po", "Привет"),
                ("two.po", "Победа"),
                ("three.po", "Готов"),
                ("four.po", "Вперёд"),
            )
            for name, text in sources:
                catalog = polib.POFile()
                catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
                catalog.append(polib.POEntry(msgid=text, msgstr=""))
                catalog.save(str(source_root / name))
            prompt = root / "prompt.md"
            glossary = root / "glossary.yaml"
            rules = root / "rules.yaml"
            prompt.write_text("Translate.", encoding="utf-8")
            glossary.write_text("schema_version: 1\nterms: []\n", encoding="utf-8")
            rules.write_text(
                "schema_version: 1\ncyrillic:\n  exact_allowlist: []\n  mappings: {}\n  scopes: []\n",
                encoding="utf-8",
            )
            data = {
                "schema_version": 1,
                "project": {"id": "game", "name": "Game", "game_version": "1"},
                "paths": {
                    "source": source_root,
                    "workspace": root / "workspace",
                    "output": root / "output",
                },
                "languages": {"source": "ru-RU", "target": "zh-Hans"},
                "resources": {
                    "adapters": [{"type": "gettext", "include": ["**/*.po"]}]
                },
                "prompt": {"template": prompt},
                "glossary": {"file": glossary},
                "rules": {"file": rules},
                "provider": {
                    "base_url": "https://provider.invalid/v1",
                    "api_key_env": "UNUSED_TEST_KEY",
                    "model": "fake",
                },
                "tm": {"database": root / "tm.sqlite3"},
            }
            config = (
                ProjectConfig.model_validate(data)
                if hasattr(ProjectConfig, "model_validate")
                else ProjectConfig.parse_obj(data)
            )
            provider = ConcurrentFakeProvider(expected_parallelism=4)
            ProjectRunner(config, provider=provider).run(
                mode=BuildMode.PREVIEW, run_id="file-scoped"
            )
            self.assertEqual(4, provider.calls)
            self.assertEqual(4, provider.max_active)
            self.assertCountEqual(
                [(name,) for name, _text in sources], provider.resource_batches
            )

    def test_wot_keyed_catalog_hits_clean_shadow_tm_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "source"
            source_root.mkdir()
            source = source_root / "messages.mo"
            catalog = polib.POFile()
            catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
            catalog.append(polib.POEntry(msgid="menu/close", msgstr="Закрыть"))
            catalog.save_as_mofile(str(source))
            prompt = root / "prompt.md"
            glossary = root / "glossary.yaml"
            rules = root / "rules.yaml"
            prompt.write_text("Translate.", encoding="utf-8")
            glossary.write_text("schema_version: 1\nterms: []\n", encoding="utf-8")
            rules.write_text(
                "schema_version: 1\ncyrillic:\n  exact_allowlist: []\n  mappings: {}\n  scopes: []\n",
                encoding="utf-8",
            )
            database = root / "tm.sqlite3"
            legacy = root / "history_tm.json"
            legacy.write_text(
                json.dumps(
                    {
                        "messages.mo": {
                            "menu/close": {"ru": "Закрыть", "zh": "关闭"}
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with SQLiteTranslationMemory(database) as tm:
                LegacyTMSynchronizer(
                    tm,
                    project_id="game",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).sync(legacy)
            data = {
                "schema_version": 1,
                "project": {"id": "game", "name": "Game", "game_version": "1"},
                "paths": {
                    "source": source_root,
                    "workspace": root / "workspace",
                    "output": root / "output",
                },
                "languages": {"source": "ru-RU", "target": "zh-Hans"},
                "resources": {
                    "adapters": [
                        {
                            "type": "gettext",
                            "include": ["**/*.mo"],
                            "options": {"layout": "keyed_source"},
                        }
                    ]
                },
                "prompt": {"template": prompt},
                "glossary": {"file": glossary},
                "rules": {"file": rules},
                "provider": {
                    "base_url": "https://provider.invalid/v1",
                    "api_key_env": "UNUSED_TEST_KEY",
                    "model": "fake",
                },
                "tm": {"database": database},
            }
            config = (
                ProjectConfig.model_validate(data)
                if hasattr(ProjectConfig, "model_validate")
                else ProjectConfig.parse_obj(data)
            )
            provider = FakeProvider()
            result = ProjectRunner(config, provider=provider).run(
                mode=BuildMode.PREVIEW, run_id="shadow-preview"
            )
            self.assertEqual(1, result.tm_hits)
            self.assertEqual(0, provider.calls)
            self.assertTrue(result.build.quality_gate.passed)
            rendered = polib.mofile(str(result.build.rendered[0]))
            self.assertEqual("关闭", rendered[0].msgstr)

    def test_release_scans_translates_validates_renders_and_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "source"
            source_root.mkdir()
            source = source_root / "messages.po"
            catalog = polib.POFile()
            catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
            catalog.append(polib.POEntry(msgid="Привет %s", msgstr=""))
            catalog.save(str(source))
            prompt = root / "prompt.md"
            background = root / "background.md"
            glossary = root / "glossary.yaml"
            rules = root / "rules.yaml"
            prompt.write_text("Translate to Simplified Chinese.", encoding="utf-8")
            background.write_text("A game UI.", encoding="utf-8")
            glossary.write_text("schema_version: 1\nterms: []\n", encoding="utf-8")
            rules.write_text(
                "schema_version: 1\ncyrillic:\n  exact_allowlist: []\n  mappings: {}\n  scopes: []\n",
                encoding="utf-8",
            )
            config_data = {
                    "schema_version": 1,
                    "project": {"id": "game", "name": "Game", "game_version": "1"},
                    "paths": {
                        "source": source_root,
                        "workspace": root / "workspace",
                        "output": root / "output",
                    },
                    "languages": {"source": "ru-RU", "target": "zh-Hans"},
                    "resources": {
                        "adapters": [{"type": "gettext", "include": ["**/*.po"]}]
                    },
                    "prompt": {"template": prompt, "background": background},
                    "glossary": {"file": glossary},
                    "rules": {"file": rules},
                    "provider": {
                        "base_url": "https://provider.invalid/v1",
                        "api_key_env": "UNUSED_TEST_KEY",
                        "model": "fake",
                        "context_window": 64_000,
                        "max_output_tokens": 8_000,
                        "custom_parameters": {"top_p": 0.7},
                    },
                    "tm": {"database": root / "tm.sqlite3"},
                }
            config = (
                ProjectConfig.model_validate(config_data)
                if hasattr(ProjectConfig, "model_validate")
                else ProjectConfig.parse_obj(config_data)
            )
            provider = FakeProvider()
            result = ProjectRunner(config, provider=provider).run(
                mode=BuildMode.RELEASE, run_id="e2e-1"
            )
            self.assertEqual(1, provider.calls)
            self.assertEqual(1, result.machine_successes)
            self.assertEqual(0, result.failed_units)
            self.assertIsNotNone(result.build.bundle)
            self.assertEqual("game-e2e-1.zip", result.build.bundle.artifact.name)
            manifest = json.loads(result.build.bundle.manifest.read_text("utf-8"))
            self.assertEqual("1", manifest["game_version"])
            self.assertEqual("local", manifest["workflow_mode"])
            self.assertEqual("fake", manifest["provider"]["model"])
            self.assertEqual(64_000, manifest["provider"]["context_window"])
            self.assertEqual(8_000, manifest["provider"]["max_output_tokens"])
            self.assertEqual({"top_p": 0.7}, manifest["provider"]["custom_parameters"])
            rendered = polib.pofile(str(result.build.rendered[0]))
            self.assertEqual("你好 %s", rendered[0].msgstr)
            units = ProjectRunner(config, provider=provider)._resources()[0].units
            with SQLiteTranslationMemory(config.tm.database) as tm:
                self.assertTrue(tm.lookup(units[0].stable_identity).is_formal)


if __name__ == "__main__":
    unittest.main()
