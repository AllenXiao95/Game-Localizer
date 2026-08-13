"""一个游戏多个资源目录（正式服 / 测试服）共享 TM 与术语表。

WoT 有 `E:/Tanki`（正式服）与 `E:/Tanki_PT`（测试服）两套资源，旧 config.py 里
是 FOLDER_RUSSIA / FOLDER_PT_RUSSIA 两个常量。它们**必须共享**翻译记忆库与
术语表：一条译文在正式服定稿了，测试服不该再花钱翻一遍。

共享是**结构性**的，不靠配置开关：`stable_identity` 由 project_id + adapter_id +
relative_path + logical_key 构成，不含变体；而 lookup 比对 source_fingerprint，
所以测试服改过源文的条目自然不命中、会重新翻译。这组测试把这条不变量钉住。
"""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.artifact import ArtifactBuilder
from localizer.config.models import ProjectConfig
from localizer.domain.translation_unit import TranslationUnit


def _config(root: Path, paths: dict, *, build: Optional[dict] = None) -> ProjectConfig:
    for name in ("prompt.md", "glossary.yaml", "rules.yaml"):
        (root / name).write_text("schema_version: 1\n", encoding="utf-8")
    data = {
        "schema_version": 1,
        "project": {"id": "wot", "name": "WoT", "game_version": "1"},
        "paths": paths,
        "languages": {"source": "ru-RU", "target": "zh-Hans"},
        "resources": {"adapters": [{"type": "gettext"}]},
        "prompt": {"template": root / "prompt.md"},
        "glossary": {"file": root / "glossary.yaml"},
        "rules": {"file": root / "rules.yaml"},
        "provider": {
            "base_url": "https://p.invalid/v1",
            "api_key_env": "VARIANT_TEST_KEY",
            "model": "m",
        },
        "tm": {"database": root / "shared-tm.sqlite3"},
    }
    if build is not None:
        data["build"] = build
    return (
        ProjectConfig.model_validate(data)
        if hasattr(ProjectConfig, "model_validate")
        else ProjectConfig.parse_obj(data)
    )


class SingleSourceStaysUnchangedTests(unittest.TestCase):
    def test_projects_with_one_source_have_no_variant_concept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _config(root, {
                "source": root / "live",
                "workspace": root / "var",
                "output": root / "out",
            })
            projected = config.for_variant()
        # 单目录项目的布局必须完全不变，不能因为引入变体概念而多出一层目录。
        self.assertIs(config, projected)
        self.assertEqual("", projected.active_variant)
        self.assertEqual(root / "var", projected.paths.workspace)
        self.assertEqual(root / "out", projected.paths.output)

    def test_asking_for_a_variant_on_a_single_source_project_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _config(root, {
                "source": root / "live",
                "workspace": root / "var",
                "output": root / "out",
            })
            with self.assertRaises(ValueError):
                config.for_variant("pts")


class MultiSourceTests(unittest.TestCase):
    def _multi(self, root: Path, **extra) -> ProjectConfig:
        paths = {
            "sources": {"live": root / "Tanki", "pts": root / "Tanki_PT"},
            "workspace": root / "var",
            "output": root / "out",
        }
        paths.update(extra)
        return _config(root, paths)

    def test_variant_selects_the_source_and_isolates_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._multi(root)
            live = config.for_variant("live")
            pts = config.for_variant("pts")

        self.assertEqual(root / "Tanki", live.paths.source)
        self.assertEqual(root / "Tanki_PT", pts.paths.source)
        # 工作区与输出必须分开，否则两个变体的 run 会互相覆盖 checkpoint 与制品。
        self.assertEqual(root / "var" / "live", live.paths.workspace)
        self.assertEqual(root / "var" / "pts", pts.paths.workspace)
        self.assertNotEqual(live.paths.output, pts.paths.output)
        self.assertEqual("live", live.active_variant)

    def test_tm_and_glossary_are_shared_not_split(self) -> None:
        # 这是整件事的目的：分开的是运行现场，共享的是知识。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._multi(root)
            live = config.for_variant("live")
            pts = config.for_variant("pts")
        self.assertEqual(live.tm.database, pts.tm.database)
        self.assertEqual(live.glossary.file, pts.glossary.file)
        self.assertEqual(live.rules.file, pts.rules.file)
        self.assertEqual(live.prompt.template, pts.prompt.template)

    def test_release_identity_is_projected_with_the_resource_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _config(
                root,
                {
                    "sources": {"live": root / "Tanki", "pts": root / "Tanki_PT"},
                    "workspace": root / "var",
                    "output": root / "out",
                },
                build={
                    "variant": "ru",
                    "compatibility_metadata": {"enabled": True, "env": "RU"},
                    "variant_overrides": {
                        "live": {"variant": "ru", "compatibility_env": "RU"},
                        "pts": {"variant": "pt", "compatibility_env": "PT"},
                    },
                },
            )
            live = config.for_variant("live")
            pts = config.for_variant("pts").for_game_version("1.45.0.0")
        self.assertEqual(("ru", "RU"), (
            live.build.variant, live.build.compatibility_metadata.env
        ))
        self.assertEqual(("pt", "PT"), (
            pts.build.variant, pts.build.compatibility_metadata.env
        ))
        self.assertEqual("pts", pts.active_variant)
        self.assertEqual("1.45.0.0", pts.project.game_version)

    def test_pt_projection_builds_pt_named_legacy_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "resources"
            resources.mkdir()
            resource = resources / "messages.mo"
            resource.write_bytes(b"pt fixture")
            config = _config(
                root,
                {
                    "sources": {"live": root / "Tanki", "pts": root / "Tanki_PT"},
                    "workspace": root / "var",
                    "output": root / "out",
                },
                build={
                    "variant": "ru",
                    "compatibility_metadata": {"enabled": True, "env": "RU"},
                    "variant_overrides": {
                        "live": {"variant": "ru", "compatibility_env": "RU"},
                        "pts": {"variant": "pt", "compatibility_env": "PT"},
                    },
                },
            ).for_variant("pts")
            compatibility = (
                config.build.compatibility_metadata.model_dump()
                if hasattr(config.build.compatibility_metadata, "model_dump")
                else config.build.compatibility_metadata.dict()
            )
            bundle = ArtifactBuilder().build_release(
                project_id=config.project.id,
                run_id="pt-release",
                resource_root=resources,
                resource_paths=[resource],
                destination=root / "bundle",
                version="1.45.0.0",
                variant=config.build.variant,
                compatibility_metadata=compatibility,
            )
            metadata = json.loads(bundle.public_metadata.read_text(encoding="utf-8"))
        self.assertEqual("i18n_pt_v1.45.0.0.zip", bundle.artifact.name)
        self.assertEqual("pt-v1.45.0.0", bundle.release_slug)
        self.assertEqual("PT", metadata["env"])

    def test_release_override_cannot_name_an_unknown_resource_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                _config(
                    root,
                    {
                        "sources": {"live": root / "Tanki"},
                        "workspace": root / "var",
                        "output": root / "out",
                    },
                    build={
                        "variant_overrides": {
                            "pts": {"variant": "pt"},
                        }
                    },
                )

    def test_release_override_env_must_match_the_public_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                _config(
                    root,
                    {
                        "sources": {"pts": root / "Tanki_PT"},
                        "workspace": root / "var",
                        "output": root / "out",
                    },
                    build={
                        "variant": "ru",
                        "compatibility_metadata": {"enabled": True, "env": "RU"},
                        "variant_overrides": {
                            "pts": {"variant": "pt", "compatibility_env": "RU"},
                        },
                    },
                )

    def test_identical_coordinates_share_one_tm_row_across_variants(self) -> None:
        # 共享是结构性的：stable_identity 不含变体。
        def unit(source_text: str) -> TranslationUnit:
            return TranslationUnit(
                project_id="wot",
                adapter_id="gettext",
                relative_path="achievements.mo",
                logical_key="medalKolobanov",
                source_text=source_text,
                source_locale="ru-RU",
                target_locale="zh-Hans",
            )

        live = unit("Колобанов")
        pts = unit("Колобанов")
        self.assertEqual(live.stable_identity, pts.stable_identity)
        self.assertEqual(live.source_fingerprint, pts.source_fingerprint)

    def test_changed_source_text_falls_out_of_the_shared_hit(self) -> None:
        # 隔离同样是结构性的：测试服改了源文，指纹变，lookup 自然不命中。
        def unit(source_text: str) -> TranslationUnit:
            return TranslationUnit(
                project_id="wot",
                adapter_id="gettext",
                relative_path="achievements.mo",
                logical_key="medalKolobanov",
                source_text=source_text,
                source_locale="ru-RU",
                target_locale="zh-Hans",
            )

        live = unit("Колобанов")
        pts = unit("Колобанов (обновлено)")
        self.assertEqual(live.stable_identity, pts.stable_identity)
        self.assertNotEqual(live.source_fingerprint, pts.source_fingerprint)

    def test_ambiguous_selection_asks_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._multi(Path(temp))
            with self.assertRaises(ValueError) as ctx:
                config.for_variant()
        message = str(ctx.exception)
        self.assertIn("--variant", message)
        self.assertIn("live", message)

    def test_default_variant_resolves_without_an_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._multi(Path(temp), default_variant="live")
            self.assertEqual("live", config.for_variant().active_variant)

    def test_unknown_variant_lists_the_available_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._multi(Path(temp))
            with self.assertRaises(ValueError) as ctx:
                config.for_variant("sandbox")
        self.assertIn("pts", str(ctx.exception))

    def test_single_declared_variant_needs_no_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _config(root, {
                "sources": {"live": root / "Tanki"},
                "workspace": root / "var",
                "output": root / "out",
            })
            self.assertEqual("live", config.for_variant().active_variant)


class ConfigValidationTests(unittest.TestCase):
    def test_at_least_one_source_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(Exception):
                _config(root, {"workspace": root / "var", "output": root / "out"})

    def test_variant_names_must_be_path_safe(self) -> None:
        # 变体名会进工作区与输出路径。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for bad in ("../escape", "a/b", "", "-lead"):
                with self.subTest(name=bad), self.assertRaises(Exception):
                    _config(root, {
                        "sources": {bad: root / "x"},
                        "workspace": root / "var",
                        "output": root / "out",
                    })

    def test_default_variant_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(Exception):
                _config(root, {
                    "sources": {"live": root / "Tanki"},
                    "default_variant": "pts",
                    "workspace": root / "var",
                    "output": root / "out",
                })


class SharedTMEndToEndTests(unittest.TestCase):
    """跑一遍真实流水线，证明第二个变体不需要再花钱翻一遍。"""

    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def translate(self, prompt, units):
            from localizer.ports.provider import ProviderResponse

            self.calls += 1
            lines = [f"[{i}] 中文译文{i}" for i in range(1, len(units) + 1)]
            return ProviderResponse("\n".join([*lines, "---END---"]))

    def _project(self, root: Path) -> ProjectConfig:
        import polib

        for variant in ("Tanki", "Tanki_PT"):
            directory = root / variant
            directory.mkdir()
            catalog = polib.POFile()
            catalog.metadata = {"Content-Type": "text/plain; charset=UTF-8"}
            catalog.append(polib.POEntry(msgid="medal_kolobanov", msgstr="Колобанов"))
            catalog.append(polib.POEntry(msgid="medal_lavrinenko", msgstr="Лавриненко"))
            catalog.save(str(directory / "achievements.po"))
        (root / "prompt.md").write_text("翻译成简体中文。", encoding="utf-8")
        (root / "glossary.yaml").write_text(
            "schema_version: 1\nterms: []\n", encoding="utf-8"
        )
        (root / "rules.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        data = {
            "schema_version": 1,
            "project": {"id": "wot", "name": "WoT", "game_version": "1"},
            "paths": {
                "sources": {"live": root / "Tanki", "pts": root / "Tanki_PT"},
                "workspace": root / "var",
                "output": root / "out",
            },
            "languages": {"source": "ru-RU", "target": "zh-Hans"},
            "resources": {
                "adapters": [
                    {
                        "type": "gettext",
                        "include": ["**/*.po"],
                        "options": {"layout": "keyed_source"},
                    }
                ]
            },
            "prompt": {"template": root / "prompt.md"},
            "glossary": {"file": root / "glossary.yaml"},
            "rules": {"file": root / "rules.yaml"},
            "provider": {
                "base_url": "https://x.invalid/v1",
                "api_key_env": "VARIANT_E2E_KEY",
                "model": "m",
            },
            # 一个共享的 TM 文件，两个变体都写它。
            "tm": {"database": root / "shared.sqlite3"},
        }
        return (
            ProjectConfig.model_validate(data)
            if hasattr(ProjectConfig, "model_validate")
            else ProjectConfig.parse_obj(data)
        )

    def test_second_variant_reuses_the_first_variants_translations(self) -> None:
        from localizer.application.local_build import BuildMode
        from localizer.application.project_runner import ProjectRunner

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._project(root)

            live_provider = self.CountingProvider()
            live = ProjectRunner(
                config.for_variant("live"), provider=live_provider
            ).run(mode=BuildMode.RELEASE, run_id="r1")

            pts_provider = self.CountingProvider()
            pts = ProjectRunner(
                config.for_variant("pts"), provider=pts_provider
            ).run(mode=BuildMode.RELEASE, run_id="r1")

            outputs = sorted(x.name for x in (root / "out").iterdir())

        self.assertEqual(2, live.machine_successes)
        # 这是整件事的目的：第二个变体一次模型都不用调。
        self.assertEqual(0, pts_provider.calls, "测试服不该为同一批译文再花一次钱")
        self.assertEqual(2, pts.tm_hits)
        self.assertEqual(0, pts.machine_successes)
        # 运行现场仍然分开，两个变体的 run_id 可以同名而不互相覆盖。
        self.assertEqual(["live", "pts"], outputs)

    def test_variants_do_not_collide_on_the_same_run_id(self) -> None:
        from localizer.application.local_build import BuildMode
        from localizer.application.project_runner import ProjectRunner

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._project(root)
            for variant in ("live", "pts"):
                ProjectRunner(
                    config.for_variant(variant), provider=self.CountingProvider()
                ).run(mode=BuildMode.RELEASE, run_id="same-id")
            live_manifests = list((root / "out" / "live" / "release" / "same-id").glob("*.manifest.json"))
            pts_manifests = list((root / "out" / "pts" / "release" / "same-id").glob("*.manifest.json"))

        # release 拒绝覆盖同 run_id 的产物；变体隔离保证这条保护不会误伤另一个变体。
        self.assertTrue(live_manifests)
        self.assertTrue(pts_manifests)


if __name__ == "__main__":
    unittest.main()
