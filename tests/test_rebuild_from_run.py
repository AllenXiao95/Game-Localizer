"""基于父运行的增量重建（R01）。

`completed + QA failed` 不能按原 run_id resume（那条路只对执行状态 `failed` 开放），
而普通新运行**不会**复用父 checkpoint 里已成功但还没正式提交的机器译文 ——
2026-08-04 的运行提交 3 条人工修复之后，新计划仍有 1,427 条待翻译、只少了 4 条，
等于要重复付一整轮的钱。

最关键的一条不是「省钱」而是**安全**：复用父译文前必须逐条确认源文没变。
源文变了却复用，产出的是「译文本身合法、但翻的不是这句」的内容 ——
任何 QA 规则都发现不了。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
)
from localizer.application.batch_orchestrator import JsonCheckpoint
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import (
    IncompatibleParentRun,
    ProjectRunner,
)
from localizer.config import load_project_config
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.provider import ProviderResponse, ProviderUsage

PARENT = "parent-run"
CHILD = "child-run"
GRANDCHILD = "grandchild-run"


class _CountingProvider:
    """记录每一次请求实际送了哪些坐标。"""

    def __init__(self, *, fail: Sequence[str] = ()) -> None:
        self.requested: list = []
        # 按 logical_key 指定哪些条目「翻不出来」。
        self.fail = set(fail)

    def translate(self, prompt: str, batch) -> ProviderResponse:
        self.requested.extend(unit.stable_identity for unit in batch)
        lines = []
        for index, unit in enumerate(batch, 1):
            if unit.logical_key in self.fail:
                # 模型把俄文原样吐回来 —— 真机上这类会被 source_language_residue
                # 判失败，而不是悄悄当成合法译文。
                lines.append(f"[{index}] {unit.source_text}")
            else:
                lines.append(f"[{index}] 机器译文 {unit.logical_key}")
        return ProviderResponse(
            "\n".join([*lines, "---END---"]),
            finish_reason="stop",
            usage=ProviderUsage(10, 10),
        )


class _Project:
    """一个能跑真 ProjectRunner 的最小 gettext 项目。"""

    KEYS = ("a", "b", "c", "d")

    def __init__(self, root: Path) -> None:
        import polib

        self.root = root
        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        game = root / "game"
        game.mkdir(exist_ok=True)
        self.po_path = game / "menu.mo"
        self._write_source({key: f"Строка {key}" for key in self.KEYS})
        base["paths"] = {
            "source": str(game),
            "workspace": str(root / "ws"),
            "output": str(root / "out"),
        }
        base["languages"] = {"source": "ru-RU", "target": "zh-Hans"}
        base["glossary"]["file"] = str(ROOT / "tests" / "fixtures" / "scope-glossary.yaml")
        base["rules"]["file"] = str(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        base["prompt"] = {"template": str(ROOT / "projects" / "example" / "prompt.md")}
        base["tm"]["database"] = str(root / "tm.sqlite3")
        base["resources"]["adapters"] = [
            {"type": "gettext", "include": ["**/*.mo"],
             "options": {"layout": "keyed_source"}}
        ]
        base["provider"]["concurrency"] = 1
        # 这组测试不关心 token 计数精度，用保守估算即可；配了 tokenizer 会强制
        # 依赖可选的 transformers。
        base["provider"].pop("tokenizer", None)
        # release 用不加密的包，免得测试依赖压缩密码环境变量。
        base["build"]["encryption"] = "none"
        base["build"].pop("password_env", None)
        self.config_path = root / "project.yaml"
        self.config_path.write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def _write_source(self, entries) -> None:
        import polib

        catalog = polib.POFile()
        for key, text in sorted(entries.items()):
            catalog.append(polib.POEntry(msgid=key, msgstr=text))
        catalog.save_as_mofile(str(self.po_path))

    def change_source(self, key: str, text: str) -> None:
        entries = {name: f"Строка {name}" for name in self.KEYS}
        entries[key] = text
        self._write_source(entries)

    def config(self):
        return load_project_config(self.config_path)

    def runner(self, provider) -> ProjectRunner:
        return ProjectRunner(self.config(), provider=provider)

    def identity(self, key: str) -> str:
        return TranslationUnit(
            project_id=self.config().project.id,
            adapter_id="gettext",
            relative_path="menu.mo",
            logical_key=key,
            source_text="x",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        ).stable_identity


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_parent(self, *, fail=()):
        provider = _CountingProvider(fail=fail)
        result = self.project.runner(provider).run(
            mode=BuildMode.PREVIEW, run_id=PARENT
        )
        return provider, result

    def commit_human(self, key: str, translation: str) -> None:
        config = self.project.config()
        identity = self.project.identity(key)
        with SQLiteTranslationMemory(config.tm.database) as tm:
            unit = next(
                u
                for resource in ProjectRunner(config).plan().resources
                for u in resource.units
                if u.stable_identity == identity
            )
            tm.apply_human_review(
                [
                    TMEntry(
                        stable_identity=identity,
                        project_id=config.project.id,
                        adapter_id="gettext",
                        relative_path="menu.mo",
                        logical_key=key,
                        source_text=unit.source_text,
                        source_fingerprint=unit.source_fingerprint,
                        translation=translation,
                        **HUMAN_REVIEW_FIELDS,
                    )
                ]
            )


class ReuseTests(_Case):
    def test_all_failures_fixed_means_zero_provider_requests(self) -> None:
        """全部失败都由人工修复之后，重建必须做到零次 Provider 请求。"""
        parent_provider, parent = self.run_parent(fail=("a",))
        self.assertEqual(1, parent.failed_units)
        # 4 条全部送过模型（失败那条还被内容 QA 重试了一次，所以请求数是 5）。
        self.assertEqual(4, len(set(parent_provider.requested)))

        self.commit_human("a", "人工补上的译文")

        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual(
            [], child_provider.requested, "已成功的坐标不该再请求一次"
        )
        self.assertIsNotNone(child.rebuild)
        self.assertEqual(3, len(child.rebuild.reused))
        self.assertEqual((), child.rebuild.retry)
        self.assertEqual(1, len(child.rebuild.resolved_by_human))

    def test_only_unresolved_failures_go_back_to_the_model(self) -> None:
        parent_provider, _ = self.run_parent(fail=("a", "b"))
        self.commit_human("a", "只修了一条")

        child_provider = _CountingProvider()
        self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        # 只有仍未解决的 b 被送回模型；a 走人工 TM，c/d 复用父译文。
        self.assertEqual([self.project.identity("b")], child_provider.requested)

    def test_reused_translations_land_in_the_child_report(self) -> None:
        self.run_parent()
        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual([], child_provider.requested)
        self.assertEqual(4, len(child.rebuild.reused))
        # 子运行有自己完整的产物与报告。
        self.assertTrue(child.build.qa_json.is_file())
        self.assertIn(CHILD, str(child.build.output_root))
        child_checkpoint = JsonCheckpoint(
            self.project.root / "ws" / "runs" / CHILD / "checkpoint.json"
        )
        self.assertEqual(4, len(child_checkpoint.units))
        self.assertTrue(
            all(row.get("state") == "succeeded" for row in child_checkpoint.units.values())
        )

    def test_materialized_child_can_be_the_next_parent_without_provider_calls(self) -> None:
        self.run_parent()
        first_provider = _CountingProvider()
        first = self.project.runner(first_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual([], first_provider.requested)
        self.assertEqual(PARENT, first.rebuild.reuse_checkpoint_run_id)

        second_provider = _CountingProvider()
        second = self.project.runner(second_provider).rebuild_from_run(
            CHILD, mode=BuildMode.PREVIEW, run_id=GRANDCHILD
        )
        self.assertEqual([], second_provider.requested)
        self.assertEqual(CHILD, second.rebuild.parent_run_id)
        self.assertEqual(CHILD, second.rebuild.reuse_checkpoint_run_id)
        self.assertTrue(
            (self.project.root / "ws" / "runs" / GRANDCHILD / "checkpoint.json").is_file()
        )

    def test_historical_child_without_checkpoint_falls_back_to_ancestor(self) -> None:
        self.run_parent()
        historical = self.project.root / "ws" / "runs" / "historical-child"
        historical.mkdir(parents=True)
        (historical / "task-request.json").write_text(
            json.dumps({"parent_run_id": PARENT}), encoding="utf-8"
        )

        provider = _CountingProvider()
        result = self.project.runner(provider).rebuild_from_run(
            "historical-child", mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual([], provider.requested)
        self.assertEqual("historical-child", result.rebuild.parent_run_id)
        self.assertEqual(PARENT, result.rebuild.reuse_checkpoint_run_id)

    def test_reused_units_keep_machine_provenance(self) -> None:
        """复用的是父运行**这一轮**产出的机器译文，对 QualityGate 仍是零容忍。

        换个 run_id 就把它降格成存量债，等于给增量缺陷开了个后门。
        """
        self.run_parent(fail=("a",))
        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        report = json.loads(child.build.qa_json.read_text(encoding="utf-8"))
        provenances = {issue["provenance"] for issue in report["issues"]}
        self.assertNotIn("legacy_coordinate_exact", provenances)


class ImmutabilityTests(_Case):
    def test_parent_run_is_untouched(self) -> None:
        _, parent = self.run_parent(fail=("a",))
        parent_checkpoint = (
            self.project.root / "ws" / "runs" / PARENT / "checkpoint.json"
        )
        before_checkpoint = parent_checkpoint.read_bytes()
        before_report = parent.build.qa_json.read_bytes()

        self.commit_human("a", "人工补上的译文")
        self.project.runner(_CountingProvider()).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual(before_checkpoint, parent_checkpoint.read_bytes())
        self.assertEqual(before_report, parent.build.qa_json.read_bytes())

    def test_rebuilding_onto_the_parent_id_is_refused(self) -> None:
        self.run_parent()
        with self.assertRaises(ValueError) as ctx:
            self.project.runner(_CountingProvider()).rebuild_from_run(
                PARENT, mode=BuildMode.PREVIEW, run_id=PARENT
            )
        self.assertIn("immutable", str(ctx.exception))

    def test_missing_parent_is_a_clear_error(self) -> None:
        with self.assertRaises(IncompatibleParentRun) as ctx:
            self.project.runner(_CountingProvider()).rebuild_from_run(
                "no-such-run", mode=BuildMode.PREVIEW, run_id=CHILD
            )
        self.assertIn("没有可复用的 checkpoint.json", str(ctx.exception))


class SourceDriftTests(_Case):
    def test_changed_source_is_never_reused(self) -> None:
        """源文变了就必须重译。

        复用一条为旧源文产出的译文，产出的是「译文本身合法、但翻的不是这句」——
        任何 QA 规则都发现不了它。这是这条路径上唯一会静默产出错误内容的地方。
        """
        self.run_parent()
        self.project.change_source("c", "Совершенно другая строка")

        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        changed = self.project.identity("c")
        self.assertIn(changed, child_provider.requested, "改过源文的必须重译")
        self.assertIn(changed, child.rebuild.stale)
        self.assertNotIn(changed, child.rebuild.reused)
        # 其余三条照常复用。
        self.assertEqual(3, len(child.rebuild.reused))
        self.assertEqual([changed], child_provider.requested)

    def test_checkpoint_without_fingerprints_refuses_to_reuse(self) -> None:
        """老 checkpoint 没记指纹时**拒绝复用**，而不是赌它没变。

        fail-closed：多花一轮钱是可恢复的，静默产出错译不是。
        """
        self.run_parent()
        path = self.project.root / "ws" / "runs" / PARENT / "checkpoint.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["unit_fingerprints"], "父运行本来应该记了指纹")
        payload["unit_fingerprints"] = {}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        child_provider = _CountingProvider()
        child = self.project.runner(child_provider).rebuild_from_run(
            PARENT, mode=BuildMode.PREVIEW, run_id=CHILD
        )
        self.assertEqual({}, dict(child.rebuild.reused))
        self.assertEqual(4, len(child.rebuild.retry))
        self.assertEqual(4, len(child_provider.requested))


class ManifestTests(_Case):
    def test_release_manifest_records_the_parent(self) -> None:
        self.run_parent()
        child = self.project.runner(_CountingProvider()).rebuild_from_run(
            PARENT, mode=BuildMode.RELEASE, run_id=CHILD
        )
        self.assertIsNotNone(child.build.bundle)
        manifest = json.loads(
            child.build.bundle.manifest.read_text(encoding="utf-8")
        )
        rebuild = manifest["rebuild"]
        self.assertEqual(PARENT, rebuild["parent_run_id"])
        self.assertEqual(PARENT, rebuild["reuse_checkpoint_run_id"])
        self.assertEqual(4, rebuild["reused"])
        self.assertEqual(0, rebuild["retried"])


class CheckpointFingerprintTests(unittest.TestCase):
    def test_fingerprints_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            checkpoint = JsonCheckpoint(path)
            checkpoint.configure_run(
                translation_units_total=1,
                translation_files_total=1,
                unit_fingerprints={"sid-1": "fp-1"},
            )
            reloaded = JsonCheckpoint(path)
        self.assertEqual({"sid-1": "fp-1"}, reloaded.unit_fingerprints)

    def test_old_checkpoints_without_fingerprints_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "units": {},
                        "batches": [],
                        "metrics": {},
                        "workers": {},
                        "resources": {},
                    }
                ),
                encoding="utf-8",
            )
            checkpoint = JsonCheckpoint(path)
        self.assertEqual({}, checkpoint.unit_fingerprints)


if __name__ == "__main__":
    unittest.main()
