from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.scan import ResourceScanner
from localizer.compat.legacy import LegacyAccessPolicy, LegacyPhase
from localizer.config import ConfigLoadError, load_project_config
from localizer.config.models import ProviderSection, ResourceAdapterSection
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO, AtomicWriteError
from localizer.infrastructure.dotenv import parse_dotenv, temporary_dotenv
from localizer.infrastructure.workspace import (
    DuplicateTargetError,
    RunWorkspace,
    WorkspaceBoundaryError,
)


class AtomicIOTests(unittest.TestCase):
    def test_json_round_trip_and_non_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            AtomicIO.write_json(target, {"中文": [1, 2, 3]})
            self.assertEqual({"中文": [1, 2, 3]}, json.loads(target.read_text("utf-8")))
            self.assertIn("中文", target.read_text("utf-8"))

    def test_replace_failure_keeps_old_bytes_and_removes_own_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            target.write_bytes(b"old-bytes")
            # 打桩点必须是 _replace_once（原语无关的接缝），不是 os.replace ——
            # Windows 上目标已存在时走的是 ReplaceFileW，打 os.replace 会静默空转。
            with mock.patch.object(AtomicIO, "_replace_once", side_effect=OSError("boom")):
                with self.assertRaises(AtomicWriteError):
                    AtomicIO.write_bytes(target, b"new-bytes")
            self.assertEqual(b"old-bytes", target.read_bytes())
            self.assertEqual([], list(Path(temp).glob(".state.json.*.tmp")))

    def test_flush_path_calls_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "value.txt"
            with mock.patch("localizer.infrastructure.atomic_io.os.fsync") as fsync:
                AtomicIO.write_text(target, "value")
            self.assertGreaterEqual(fsync.call_count, 1)

    # 退避阶梯只对 Windows 的 5/32/33 生效（_is_transient_windows_lock 在其他平台
    # 直接返回 False），所以这条只在 Windows 上有意义。CI 开始跑 tests/ 之后，
    # 不加这个守卫会让 ubuntu 那条腿必红。
    @unittest.skipUnless(os.name == "nt", "共享冲突退避是 Windows 专有语义")
    def test_windows_sharing_violation_retries_the_same_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "checkpoint.json"
            target.write_bytes(b"old")
            locked = PermissionError(13, "temporarily locked")
            locked.winerror = 5
            real_replace_once = AtomicIO._replace_once
            calls = 0

            def replace_after_lock(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise locked
                return real_replace_once(source, destination)

            with mock.patch.object(
                AtomicIO, "_replace_once", side_effect=replace_after_lock
            ), mock.patch("localizer.infrastructure.atomic_io.time.sleep") as sleep:
                AtomicIO.write_bytes(target, b"new")
            self.assertEqual(b"new", target.read_bytes())
            self.assertEqual(2, calls)
            sleep.assert_called_once_with(0.01)

    def test_windows_non_lock_replace_error_still_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "checkpoint.json"
            target.write_bytes(b"old")
            error = PermissionError(13, "not a sharing violation")
            error.winerror = 123
            with mock.patch.object(
                AtomicIO, "_replace_once", side_effect=error
            ) as replace, mock.patch(
                "localizer.infrastructure.atomic_io.time.sleep"
            ) as sleep:
                with self.assertRaises(AtomicWriteError):
                    AtomicIO.write_bytes(target, b"new")
            self.assertEqual(b"old", target.read_bytes())
            self.assertEqual(1, replace.call_count)
            sleep.assert_not_called()

    def test_duplicate_targets_fail_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "same"
            with self.assertRaises(AtomicWriteError):
                AtomicIO.assert_unique_targets([target, target])


class RunWorkspaceTests(unittest.TestCase):
    def test_cleanup_only_removes_registered_run_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = RunWorkspace(Path(temp), "run-1").create()
            owned = workspace.child("temp", "owned.tmp")
            foreign = workspace.child("temp", "foreign.tmp")
            owned.write_text("owned", encoding="utf-8")
            foreign.write_text("foreign", encoding="utf-8")
            workspace.register_temporary_file(owned)
            workspace.cleanup_owned_temporary_files()
            self.assertFalse(owned.exists())
            self.assertTrue(foreign.exists())

    def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = RunWorkspace(Path(temp), "run-1").create()
            with self.assertRaises(WorkspaceBoundaryError):
                workspace.register_temporary_file(Path(temp) / "outside.tmp")

    def test_duplicate_reserved_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = RunWorkspace(Path(temp), "run-1").create()
            target = workspace.child("preview", "same.mo")
            workspace.reserve_targets([target])
            with self.assertRaises(DuplicateTargetError):
                workspace.reserve_targets([target])


class ScannerAndDomainTests(unittest.TestCase):
    def test_read_only_scan_preserves_relative_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "a" / "same.mo"
            second = root / "b" / "same.mo"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            before = {path: path.stat().st_mtime_ns for path in (first, second)}
            result = ResourceScanner().scan(root, includes=("**/*.mo",))
            after = {path: path.stat().st_mtime_ns for path in (first, second)}
            self.assertEqual(before, after)
            self.assertEqual(["a/same.mo", "b/same.mo"], [item.relative_path for item in result.resources])

            units = [
                TranslationUnit(
                    project_id="p",
                    adapter_id="gettext",
                    relative_path=item.relative_path,
                    logical_key="key",
                    source_text="source",
                    source_locale="ru",
                    target_locale="zh",
                )
                for item in result.resources
            ]
            self.assertNotEqual(units[0].stable_identity, units[1].stable_identity)


class ConfigAndCompatibilityTests(unittest.TestCase):
    def test_provider_concurrency_must_be_positive(self) -> None:
        with self.assertRaises(Exception):
            ProviderSection(
                base_url="https://provider.invalid/v1",
                api_key_env="LOCALIZER_API_KEY",
                model="fake",
                concurrency=0,
            )

    def test_adapter_options_are_format_specific_and_strict(self) -> None:
        section = ResourceAdapterSection(
            type="gettext", options={"layout": "keyed_source"}
        )
        self.assertEqual("keyed_source", section.options["layout"])
        self.assertEqual("skip", section.options["empty_source"])
        with self.assertRaises(Exception):
            ResourceAdapterSection(
                type="gettext", options={"unknown_format_switch": True}
            )

    def test_dotenv_parser_is_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text(
                'DOTENV_TEST_TOKEN="secret=value#1"\nexport DOTENV_TEST_EMPTY=\n',
                encoding="utf-8",
            )
            self.assertEqual("secret=value#1", parse_dotenv(path)["DOTENV_TEST_TOKEN"])
            os.environ.pop("DOTENV_TEST_TOKEN", None)
            with temporary_dotenv([path]):
                self.assertEqual("secret=value#1", os.environ["DOTENV_TEST_TOKEN"])
            self.assertNotIn("DOTENV_TEST_TOKEN", os.environ)

    def test_config_auto_discovers_dotenv_only_up_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            config_dir = root / "projects" / "wot"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "project.yaml"
            config_path.write_text(
                (ROOT / "projects" / "example" / "project.yaml").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            dotenv = root / ".env"
            dotenv.write_text("AUTO_DISCOVER_TEST=loaded\n", encoding="utf-8")
            os.environ.pop("AUTO_DISCOVER_TEST", None)
            try:
                config = load_project_config(config_path)
                self.assertEqual("loaded", os.environ["AUTO_DISCOVER_TEST"])
                self.assertIn(dotenv.resolve(), config.environment.dotenv_files)
            finally:
                os.environ.pop("AUTO_DISCOVER_TEST", None)

    def test_example_config_is_strict_and_resolves_paths(self) -> None:
        config = load_project_config(ROOT / "projects" / "example" / "project.yaml")
        self.assertEqual("example-game", config.project.id)
        self.assertTrue(config.paths.workspace.is_absolute())
        self.assertTrue(config.cache.root.is_absolute())
        self.assertEqual(ROOT / "var" / "cache", config.cache.root)
        self.assertEqual(ROOT / "var" / "cache" / "tokenizers", config.cache.tokenizers)
        self.assertEqual("shared", config.cache.scope)
        self.assertEqual("provider-model-name", config.provider.model)
        self.assertIsNotNone(config.provider.tokenizer)
        self.assertEqual(
            "organization/tokenizer-repository", config.provider.tokenizer.model
        )
        self.assertNotEqual(config.provider.model, config.provider.tokenizer.model)
        self.assertEqual(32768, config.provider.context_window)
        self.assertEqual(4096, config.provider.max_output_tokens)
        self.assertEqual({}, config.provider.custom_parameters)
        self.assertEqual("quality_gate", config.tm.commit_policy)

    def test_tokenizer_is_optional_and_never_inferred_from_provider_model(self) -> None:
        source = (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        tokenizer_block = (
            "  # 可选；本地 tokenizer 身份独立配置，绝不从上面的 API 模型名自动推断。\n"
            "  tokenizer:\n"
            "    type: huggingface\n"
            "    model: organization/tokenizer-repository\n"
            "    revision: pinned-revision\n"
            "    local_files_only: false\n"
        )
        source = source.replace(tokenizer_block, "")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(source, encoding="utf-8")
            config = load_project_config(path)
        self.assertEqual("provider-model-name", config.provider.model)
        self.assertIsNone(config.provider.tokenizer)

    def test_config_rejects_inline_secret(self) -> None:
        source = (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        source = source.replace("api_key_env: LOCALIZER_API_KEY", "api_key_env: sk-real-secret")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ConfigLoadError):
                load_project_config(path)

    def test_provider_custom_parameters_accept_json_and_reject_owned_fields(self) -> None:
        source = (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        source = source.replace(
            "  custom_parameters: {}",
            "  custom_parameters:\n"
            "    top_p: 0.8\n"
            "    enable_thinking: false\n"
            "    thinking: {type: disabled}\n",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(source, encoding="utf-8")
            config = load_project_config(path)
        self.assertEqual(0.8, config.provider.custom_parameters["top_p"])
        self.assertFalse(config.provider.custom_parameters["enable_thinking"])
        self.assertEqual(
            {"type": "disabled"}, config.provider.custom_parameters["thinking"]
        )

        invalid = source.replace("    top_p: 0.8", "    messages: []")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaises(ConfigLoadError):
                load_project_config(path)

    def test_provider_token_limits_must_be_consistent(self) -> None:
        source = (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        source = source.replace("  max_output_tokens: 4096", "  max_output_tokens: 32768")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ConfigLoadError):
                load_project_config(path)

    def test_legacy_policy_blocks_unconstrained_save_after_shadow_start(self) -> None:
        LegacyAccessPolicy(LegacyPhase.M1_M2).validate_arguments(["--save-tm"])
        with self.assertRaises(RuntimeError):
            LegacyAccessPolicy(LegacyPhase.M3_SHADOW).validate_arguments(["--save-tm"])
        with self.assertRaises(RuntimeError):
            LegacyAccessPolicy(LegacyPhase.SQLITE_AUTHORITY).validate_arguments(["--save-tm"])



class DesignDocConfigBlocksParseTests(unittest.TestCase):
    """示例工作流配置块必须与严格 Schema 保持一致（评估 R04-①）。"""

    def _paratranz_workflow_block(self) -> str:
        return """workflow:
  mode: paratranz
  project_id: 1234
  token_env: PARATRANZ_TOKEN
  minimum_release_stage: 3
  sync:
    dry_run_by_default: true
    delete_policy: report_only
"""

    def test_section_ten_paratranz_block_loads(self) -> None:
        import yaml

        from localizer.config.loader import load_project_config

        block = yaml.safe_load(self._paratranz_workflow_block())
        self.assertEqual("paratranz", block["workflow"]["mode"])

        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        base["workflow"] = block["workflow"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.yaml"
            path.write_text(
                yaml.safe_dump(base, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            config = load_project_config(path)
        self.assertEqual(1234, config.workflow.project_id)
        self.assertEqual(3, config.workflow.minimum_release_stage)
        self.assertTrue(config.workflow.sync.dry_run_by_default)
        self.assertEqual("report_only", config.workflow.sync.delete_policy)

    def test_unknown_workflow_field_is_still_rejected(self) -> None:
        # 补字段不等于放宽 Schema：拼错的键必须照旧报错。
        from pydantic import ValidationError

        from localizer.config.models import WorkflowSection

        with self.assertRaises(ValidationError):
            WorkflowSection(mode="local", minimum_release_stages=3)

    def test_hidden_stage_is_not_a_release_threshold(self) -> None:
        from pydantic import ValidationError

        from localizer.config.models import WorkflowSection

        with self.assertRaises(ValidationError):
            WorkflowSection(mode="local", minimum_release_stage=-1)

if __name__ == "__main__":
    unittest.main()
