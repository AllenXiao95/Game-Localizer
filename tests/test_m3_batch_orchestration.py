from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import re
import types
from pathlib import Path
from typing import Optional, Sequence
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.providers.openai_compatible import (
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    TransientProviderError,
)
from localizer.application.batch_orchestrator import BatchOrchestrator, JsonCheckpoint
from localizer.application.prompt import PromptComposer
from localizer.domain.translation_unit import TranslationUnit
from localizer.config.models import TokenizerSection
from localizer.infrastructure.token_counting import HuggingFaceTokenCounter
from localizer.ports.provider import ProviderResponse, ProviderUsage


def units(count: int) -> tuple[TranslationUnit, ...]:
    return tuple(
        TranslationUnit(
            project_id="test",
            adapter_id="gettext",
            relative_path="locale/messages.mo",
            logical_key=str(index),
            source_text=f"Source {index}",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        for index in range(count)
    )


def response(
    values: Sequence[str], *, finish_reason: Optional[str] = None
) -> ProviderResponse:
    text = "\n".join(
        [*(f"[{index}] {value}" for index, value in enumerate(values, 1)), "---END---"]
    )
    return ProviderResponse(
        text,
        finish_reason=finish_reason,
        usage=ProviderUsage(input_tokens=10, output_tokens=5),
    )


class ScriptedProvider:
    def __init__(self, script) -> None:
        self.script = script
        self.calls = []

    def translate(self, prompt: str, batch: Sequence[TranslationUnit]) -> ProviderResponse:
        self.calls.append((prompt, tuple(item.stable_identity for item in batch)))
        result = self.script(len(self.calls), batch)
        if isinstance(result, Exception):
            raise result
        return result


class BatchOrchestratorTests(unittest.TestCase):
    def orchestrator(self, root: Path, provider: ScriptedProvider) -> BatchOrchestrator:
        return BatchOrchestrator(
            provider,
            PromptComposer("Translate."),
            JsonCheckpoint(root / "checkpoint.json"),
            sleep=lambda _: None,
        )

    def test_transient_errors_retry_same_batch_twice_then_checkpoint(self) -> None:
        def script(call, batch):
            if call <= 2:
                return TransientProviderError("429")
            return response([f"译文 {index}" for index in range(len(batch))])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = ScriptedProvider(script)
            source_units = units(2)
            result = self.orchestrator(root, provider).run(source_units)
            self.assertEqual(3, result.requests)
            self.assertFalse(result.failed)
            self.assertEqual(provider.calls[0][1], provider.calls[1][1])
            # A resumed run consumes the durable checkpoint and makes no paid request.
            resumed_provider = ScriptedProvider(lambda *_: AssertionError("must not submit"))
            resumed = self.orchestrator(root, resumed_provider).run(source_units)
            self.assertEqual(0, resumed.requests)
            self.assertEqual([], resumed_provider.calls)

    def test_checkpoint_records_file_worker_batch_and_token_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = ScriptedProvider(
                lambda call, batch: response(["成功"] * len(batch))
            )
            checkpoint = JsonCheckpoint(root / "checkpoint.json")
            checkpoint.configure_run(
                translation_units_total=2, translation_files_total=1
            )
            result = BatchOrchestrator(
                provider,
                PromptComposer("Translate."),
                checkpoint,
                sleep=lambda _: None,
            ).run(
                units(2),
                resource_path="locale/messages.mo",
                worker_id="translation-0",
            )
            self.assertFalse(result.failed)
            payload = json.loads((root / "checkpoint.json").read_text("utf-8"))
            self.assertEqual(3, payload["schema_version"])
            self.assertEqual(1, payload["metrics"]["requests"])
            self.assertEqual(10, payload["metrics"]["input_tokens"])
            self.assertEqual(5, payload["metrics"]["output_tokens"])
            self.assertEqual(
                ["locale/messages.mo"], payload["metrics"]["completed_files"]
            )
            batch_ids = {
                event["batch_id"]
                for event in payload["batches"]
                if event["batch_id"]
            }
            self.assertEqual(1, len(batch_ids))
            self.assertTrue(
                all(
                    event["resource_path"] == "locale/messages.mo"
                    for event in payload["batches"]
                )
            )
            self.assertEqual("idle", payload["workers"]["translation-0"]["state"])
            resource = payload["resources"]["locale/messages.mo"]
            self.assertEqual("completed", resource["state"])
            self.assertEqual(2, resource["units_succeeded"])
            self.assertEqual(1, resource["requests"])
            self.assertEqual(15, resource["input_tokens"] + resource["output_tokens"])

    def test_numbering_gets_one_repair_attempt_then_bounded_split(self) -> None:
        source_units = units(4)

        def script(call, batch):
            if len(batch) == 4:
                return ProviderResponse(
                    "[1] bad\n[3] jump\n---END---",
                    usage=ProviderUsage(input_tokens=10, output_tokens=5),
                )
            return response(["成功"] * len(batch))

        with tempfile.TemporaryDirectory() as temp:
            provider = ScriptedProvider(script)
            result = self.orchestrator(Path(temp), provider).run(source_units)
            self.assertFalse(result.failed)
            self.assertEqual([4, 4, 2, 2], [len(call[1]) for call in provider.calls])
            self.assertIn("protocol repair attempt", provider.calls[1][0])
            checkpoint = json.loads((Path(temp) / "checkpoint.json").read_text("utf-8"))
            self.assertEqual(40, checkpoint["metrics"]["input_tokens"])
            self.assertEqual(20, checkpoint["metrics"]["output_tokens"])
            self.assertEqual(
                4,
                sum(
                    event["state"] == "received"
                    for event in checkpoint["batches"]
                ),
            )

    def test_length_truncation_splits_immediately_without_same_size_retry(self) -> None:
        source_units = units(4)

        def script(call, batch):
            if len(batch) == 4:
                return response(["截断"] * 4, finish_reason="length")
            return response(["成功"] * len(batch))

        with tempfile.TemporaryDirectory() as temp:
            provider = ScriptedProvider(script)
            result = self.orchestrator(Path(temp), provider).run(source_units)
            self.assertFalse(result.failed)
            self.assertEqual([4, 2, 2], [len(call[1]) for call in provider.calls])

    def test_content_qa_retries_only_failed_units_and_never_uses_source_as_success(self) -> None:
        source_units = units(2)

        def script(call, batch):
            if len(batch) == 2:
                return response(["合格", "仍有 Кириллица"])
            return response(["再次 Кириллица"])

        with tempfile.TemporaryDirectory() as temp:
            provider = ScriptedProvider(script)
            result = self.orchestrator(Path(temp), provider).run(source_units)
            self.assertEqual([2, 1], [len(call[1]) for call in provider.calls])
            self.assertEqual("succeeded", result.results[0].state)
            self.assertEqual("failed", result.results[1].state)
            self.assertIsNone(result.results[1].translation)
            checkpoint = json.loads((Path(temp) / "checkpoint.json").read_text("utf-8"))
            self.assertEqual("failed", checkpoint["units"][source_units[1].stable_identity]["state"])

    def test_placeholders_are_masked_before_provider_and_restored_after_validation(self) -> None:
        source_unit = TranslationUnit(
            project_id="test",
            adapter_id="gettext",
            relative_path="locale/messages.mo",
            logical_key="placeholder",
            source_text="Привет %s",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )

        def script(call, batch):
            token = re.search(r"\[PH_[0-9a-f]{8}_\d+\]", batch[0].source_text).group(0)
            return response([f"你好 {token}"])

        with tempfile.TemporaryDirectory() as temp:
            provider = ScriptedProvider(script)
            result = self.orchestrator(Path(temp), provider).run([source_unit])
            self.assertIn("[PH_", provider.calls[0][0])
            self.assertNotIn("Привет %s", provider.calls[0][0])
            self.assertEqual("你好 %s", result.results[0].translation)

    def test_batch_size_grows_with_context_window(self) -> None:
        source_units = units(30)

        def run_with(context_window: int):
            provider = ScriptedProvider(
                lambda call, batch: response(["成功"] * len(batch))
            )
            with tempfile.TemporaryDirectory() as temp:
                result = BatchOrchestrator(
                    provider,
                    PromptComposer("Translate."),
                    JsonCheckpoint(Path(temp) / "checkpoint.json"),
                    context_window=context_window,
                    max_output_tokens=300,
                    token_counter=len,
                    sleep=lambda _: None,
                ).run(source_units)
            self.assertFalse(result.failed)
            return [len(call[1]) for call in provider.calls]

        small = run_with(600)
        large = run_with(1_200)
        self.assertGreater(len(small), len(large))
        self.assertGreater(max(large), max(small))

    def test_batch_size_tracks_text_prompt_background_and_glossary_tokens(self) -> None:
        source_units = units(30)

        def planned_sizes(composer: PromptComposer):
            provider = ScriptedProvider(
                lambda call, batch: response(["成功"] * len(batch))
            )
            with tempfile.TemporaryDirectory() as temp:
                result = BatchOrchestrator(
                    provider,
                    composer,
                    JsonCheckpoint(Path(temp) / "checkpoint.json"),
                    context_window=1_200,
                    max_output_tokens=800,
                    token_counter=len,
                    sleep=lambda _: None,
                ).run(source_units)
            self.assertFalse(result.failed)
            return [len(call[1]) for call in provider.calls]

        compact = planned_sizes(PromptComposer("T"))
        prompt_heavy = planned_sizes(PromptComposer("T" * 100))
        background_heavy = planned_sizes(PromptComposer("T", "B" * 100))
        glossary_heavy = planned_sizes(PromptComposer("T", "", "G" * 100))
        for expanded in (prompt_heavy, background_heavy, glossary_heavy):
            self.assertGreater(max(compact), max(expanded))
            self.assertLess(len(compact), len(expanded))

    def test_longer_units_produce_smaller_batches(self) -> None:
        def sized_units(length: int) -> tuple[TranslationUnit, ...]:
            return tuple(
                TranslationUnit(
                    project_id="test",
                    adapter_id="gettext",
                    relative_path="locale/messages.mo",
                    logical_key=str(index),
                    source_text="Ж" * length,
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                )
                for index in range(30)
            )

        def planned_sizes(source_units):
            provider = ScriptedProvider(
                lambda call, batch: response(["成功"] * len(batch))
            )
            with tempfile.TemporaryDirectory() as temp:
                BatchOrchestrator(
                    provider,
                    PromptComposer("Translate."),
                    JsonCheckpoint(Path(temp) / "checkpoint.json"),
                    context_window=5_000,
                    max_output_tokens=1_000,
                    token_counter=len,
                    sleep=lambda _: None,
                ).run(source_units)
            return [len(call[1]) for call in provider.calls]

        short = planned_sizes(sized_units(8))
        long = planned_sizes(sized_units(100))
        self.assertGreater(max(short), max(long))
        self.assertLess(len(short), len(long))

    def test_internal_unit_cap_is_only_a_defensive_fuse(self) -> None:
        provider = ScriptedProvider(
            lambda call, batch: response(["成功"] * len(batch))
        )
        with tempfile.TemporaryDirectory() as temp:
            result = BatchOrchestrator(
                provider,
                PromptComposer("T"),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                context_window=1_000_000,
                max_output_tokens=100_000,
                token_counter=len,
                sleep=lambda _: None,
            ).run(units(300))
        self.assertFalse(result.failed)
        self.assertEqual([256, 44], [len(call[1]) for call in provider.calls])

    def test_single_unit_over_budget_fails_without_provider_request(self) -> None:
        provider = ScriptedProvider(lambda *_: AssertionError("must not submit"))
        oversized = TranslationUnit(
            project_id="test",
            adapter_id="gettext",
            relative_path="locale/messages.mo",
            logical_key="oversized",
            source_text="Ж" * 2_000,
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        with tempfile.TemporaryDirectory() as temp:
            result = BatchOrchestrator(
                provider,
                PromptComposer("Translate."),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                context_window=500,
                max_output_tokens=100,
                token_counter=len,
                sleep=lambda _: None,
            ).run([oversized])
        self.assertEqual([], provider.calls)
        self.assertEqual(1, len(result.failed))
        self.assertIn("exceeds provider token budget", result.failed[0].issues[0].message)

    def test_request_budget_stops_submission_and_archives_remaining_failures(self) -> None:
        provider = ScriptedProvider(
            lambda call, batch: TransientProviderError("timeout")
        )
        with tempfile.TemporaryDirectory() as temp:
            orchestrator = BatchOrchestrator(
                provider,
                PromptComposer("Translate."),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                max_requests=1,
                sleep=lambda _: None,
            )
            result = orchestrator.run(units(2))
            self.assertEqual(1, result.requests)
            self.assertEqual(2, len(result.failed))
            self.assertTrue(
                all(item.issues[0].code == "budget_exceeded" for item in result.failed)
            )


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_uses_environment_secret_and_explicit_output_limit(self) -> None:
        captured = {}

        def transport(url, headers, body, timeout):
            captured.update(
                url=url,
                headers=headers,
                payload=json.loads(body.decode("utf-8")),
                timeout=timeout,
            )
            return {
                "id": "request-1",
                "model": "test-model",
                "choices": [
                    {"message": {"content": "[1] 译文\n---END---"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

        variable = "LOCALIZER_TEST_PROVIDER_KEY"
        os.environ[variable] = "secret-value"
        try:
            provider = OpenAICompatibleProvider(
                OpenAICompatibleSettings(
                    base_url="https://provider.invalid/v1",
                    api_key_env=variable,
                    model="test-model",
                    max_output_tokens=321,
                ),
                transport=transport,
            )
            result = provider.translate("prompt", units(1))
        finally:
            os.environ.pop(variable, None)
        self.assertEqual("Bearer secret-value", captured["headers"]["Authorization"])
        self.assertEqual(321, captured["payload"]["max_tokens"])
        self.assertEqual(2, result.usage.output_tokens)

    def test_custom_json_parameters_are_merged_without_overriding_core_fields(self) -> None:
        captured = {}

        def transport(url, headers, body, timeout):
            captured["payload"] = json.loads(body.decode("utf-8"))
            return {
                "choices": [
                    {"message": {"content": "[1] 译文\n---END---"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

        variable = "LOCALIZER_TEST_PROVIDER_KEY"
        os.environ[variable] = "secret-value"
        try:
            provider = OpenAICompatibleProvider(
                OpenAICompatibleSettings(
                    base_url="https://provider.invalid/v1",
                    api_key_env=variable,
                    model="test-model",
                    custom_parameters={
                        "top_p": 0.8,
                        "enable_thinking": False,
                        "thinking": {"type": "disabled"},
                        "stop": ["<END>"],
                    },
                ),
                transport=transport,
            )
            provider.translate("prompt", units(1))
        finally:
            os.environ.pop(variable, None)
        self.assertEqual(0.8, captured["payload"]["top_p"])
        self.assertFalse(captured["payload"]["enable_thinking"])
        self.assertEqual({"type": "disabled"}, captured["payload"]["thinking"])
        self.assertEqual(["<END>"], captured["payload"]["stop"])
        self.assertEqual("test-model", captured["payload"]["model"])

    def test_custom_parameters_reject_core_overrides_and_credentials(self) -> None:
        base = {
            "base_url": "https://provider.invalid/v1",
            "api_key_env": "LOCALIZER_TEST_PROVIDER_KEY",
            "model": "test-model",
        }
        with self.assertRaisesRegex(ValueError, "framework-owned"):
            OpenAICompatibleSettings(
                **base, custom_parameters={"messages": [{"role": "system"}]}
            )
        with self.assertRaisesRegex(ValueError, "credential"):
            OpenAICompatibleSettings(
                **base, custom_parameters={"vendor": {"api_key": "inline"}}
            )


class TokenCountingTests(unittest.TestCase):
    def test_huggingface_counter_is_lazy_cached_and_uses_the_canonical_cache(self) -> None:
        calls = []

        class FakeTokenizer:
            def encode(self, text, *, add_special_tokens):
                self.last_special_tokens = add_special_tokens
                return text.split()

        fake_tokenizer = FakeTokenizer()

        class FakeAutoTokenizer:
            @staticmethod
            def from_pretrained(model, **kwargs):
                calls.append((model, kwargs))
                return fake_tokenizer

        module = types.SimpleNamespace(AutoTokenizer=FakeAutoTokenizer)
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            sys.modules, {"transformers": module}
        ):
            cache = Path(temp) / "tokenizers"
            snapshot = (
                cache
                / "models--vendor--tokenizer"
                / "snapshots"
                / "pinned"
            )
            snapshot.mkdir(parents=True)
            (snapshot / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
            counter = HuggingFaceTokenCounter(
                TokenizerSection(
                    model="vendor/tokenizer",
                    revision="pinned",
                    local_files_only=True,
                ),
                cache,
            )
            self.assertEqual([], calls)
            self.assertEqual(3, counter("one two three"))
            self.assertEqual(2, counter("four five"))
        self.assertEqual(1, len(calls))
        self.assertEqual(str(snapshot.resolve()), calls[0][0])
        self.assertTrue(calls[0][1]["local_files_only"])
        self.assertNotIn("revision", calls[0][1])
        self.assertNotIn("cache_dir", calls[0][1])
        self.assertFalse(fake_tokenizer.last_special_tokens)

    @unittest.skipUnless(
        importlib.util.find_spec("transformers"),
        "这条断言的是 transformers 装好之后「快照不完整」的报错；没装时先撞到的是"
        "「transformers 未安装」，两条错误是不同的诊断。装可选依赖："
        "pip install -e '.[tokenizer-huggingface]'",
    )
    def test_local_only_counter_fails_clearly_for_incomplete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "tokenizers"
            snapshot = (
                cache
                / "models--vendor--tokenizer"
                / "snapshots"
                / "incomplete"
            )
            snapshot.mkdir(parents=True)
            (snapshot / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            counter = HuggingFaceTokenCounter(
                TokenizerSection(
                    model="vendor/tokenizer",
                    revision="incomplete",
                    local_files_only=True,
                ),
                cache,
            )
            with self.assertRaisesRegex(
                RuntimeError, "local tokenizer snapshot is unavailable or incomplete"
            ):
                counter.warm_up()


if __name__ == "__main__":
    unittest.main()


class ResumeRevalidatesSourceFingerprintsTests(unittest.TestCase):
    """普通 resume 必须和 rebuild 一样逐条校验源文指纹（对抗性审查 HIGH）。

    `_process` 直接 `checkpoint.succeeded(identity)` 取旧译文，从不看指纹；
    而 `_plan_rebuild` 是逐条校验的 —— 两条路不对称。R15 把可恢复态放宽到
    `queued`/`running` 之后这条更要命：被恢复的 run 往往是几小时甚至几天前被杀
    的，中间客户端更新过源文的概率远高于原来「刚刚 failed」的场景。

    后果是最难发现的那一类：**译文本身完全合法**，任何 QA 规则都不会报，
    它只是翻的不是现在这句源文。
    """

    def _unit(self, source_text: str) -> TranslationUnit:
        return TranslationUnit(
            project_id="test",
            adapter_id="gettext",
            relative_path="locale/messages.mo",
            logical_key="k",
            source_text=source_text,
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )

    def _run(self, root: Path, unit: TranslationUnit, provider: ScriptedProvider):
        checkpoint = JsonCheckpoint(root / "checkpoint.json")
        checkpoint.configure_run(
            translation_units_total=1,
            translation_files_total=1,
            unit_fingerprints={unit.stable_identity: unit.source_fingerprint},
        )
        result = BatchOrchestrator(
            provider,
            PromptComposer("Translate."),
            checkpoint,
            sleep=lambda _: None,
        ).run([unit])
        checkpoint.finalize()
        return checkpoint, result

    def test_a_changed_source_invalidates_the_cached_translation(self) -> None:
        first = self._unit("Source 0")
        # stable_identity 不含 source_text，所以改源文之后坐标不变、指纹变。
        second = self._unit("Совершенно другой текст")
        self.assertEqual(first.stable_identity, second.stable_identity)
        self.assertNotEqual(first.source_fingerprint, second.source_fingerprint)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = ScriptedProvider(lambda call, batch: response(["旧译文"]))
            self._run(root, first, provider)
            self.assertEqual(1, len(provider.calls))

            resumed = ScriptedProvider(lambda call, batch: response(["新译文"]))
            checkpoint, result = self._run(root, second, resumed)
            # 必须重译，而不是把为旧源文产出的译文当成功搬过来。
            self.assertEqual(1, len(resumed.calls), "源文变了却没有重新请求")
            self.assertEqual("新译文", result.results[0].translation)
            self.assertEqual(1, checkpoint.metrics["stale_source_invalidated"])

    def test_an_unchanged_source_still_resumes_for_free(self) -> None:
        """对照组：别把「作废过期译文」做成「每次恢复都重跑」。"""
        unit = self._unit("Source 0")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._run(root, unit, ScriptedProvider(lambda call, batch: response(["译文"])))

            def never(call, batch):
                raise AssertionError("未改动的源文不该重新请求")

            checkpoint, result = self._run(root, unit, ScriptedProvider(never))
            self.assertEqual("译文", result.results[0].translation)
            self.assertNotIn("stale_source_invalidated", checkpoint.metrics)

    def test_a_checkpoint_without_fingerprints_is_left_alone(self) -> None:
        """老 checkpoint 没有 unit_fingerprints —— 不能因此把它整个作废。

        第一次带指纹跑的时候，库里没有旧值可比，只能记下来；真正的校验从
        第二次开始。
        """
        unit = self._unit("Source 0")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = JsonCheckpoint(root / "checkpoint.json")
            checkpoint.configure_run(
                translation_units_total=1, translation_files_total=1
            )
            BatchOrchestrator(
                ScriptedProvider(lambda call, batch: response(["译文"])),
                PromptComposer("Translate."),
                checkpoint,
                sleep=lambda _: None,
            ).run([unit])
            checkpoint.finalize()

            def never(call, batch):
                raise AssertionError("首次记录指纹不该导致重译")

            _checkpoint, result = self._run(root, unit, ScriptedProvider(never))
            self.assertEqual("译文", result.results[0].translation)

    def test_only_the_drifted_unit_is_invalidated(self) -> None:
        """一条源文改了不该连累整批。"""
        keep = TranslationUnit(
            project_id="test",
            adapter_id="gettext",
            relative_path="locale/messages.mo",
            logical_key="keep",
            source_text="Stable",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        before = self._unit("Source 0")
        after = self._unit("Changed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = JsonCheckpoint(root / "checkpoint.json")
            checkpoint.configure_run(
                translation_units_total=2,
                translation_files_total=1,
                unit_fingerprints={
                    unit.stable_identity: unit.source_fingerprint
                    for unit in (keep, before)
                },
            )
            BatchOrchestrator(
                ScriptedProvider(lambda call, batch: response(["A", "B"])),
                PromptComposer("Translate."),
                checkpoint,
                sleep=lambda _: None,
            ).run([keep, before])
            checkpoint.finalize()

            resumed = JsonCheckpoint(root / "checkpoint.json")
            resumed.configure_run(
                translation_units_total=2,
                translation_files_total=1,
                unit_fingerprints={
                    unit.stable_identity: unit.source_fingerprint
                    for unit in (keep, after)
                },
            )
            self.assertEqual(1, resumed.metrics["stale_source_invalidated"])
            self.assertIsNotNone(resumed.succeeded(keep.stable_identity))
            self.assertIsNone(resumed.succeeded(after.stable_identity))
