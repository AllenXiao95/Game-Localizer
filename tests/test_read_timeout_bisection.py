"""读超时后有界缩批（R02）。

2026-08-04 真机 preview：1,431 条待翻译里 98 条失败，其中 **97 条来自单独一个
97 条批次连续三次撞 120 秒读超时**。那 97 条本身没有任何问题 —— 拆开重投就能过，
但当时的实现把整批判死。

同时必须守住反方向：429 / 5xx / 建连失败**不得**缩批。实测持续 429 下缩批会让
16 条批次打出 93 次请求（16,16,16,8,8,8,4,4,4,2,2,2,1,1,1…），93 秒内对一个正在
限流的端点连打 93 次，最终 16 条仍然全部失败。缩批本身不是问题，
**缩批顺带把重试预算也重置了**才是。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.providers.openai_compatible import (
    ReadTimeoutError,
    TransientProviderError,
)
from localizer.application.batch_orchestrator import BatchOrchestrator, JsonCheckpoint
from localizer.application.prompt import PromptComposer
from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.provider import ProviderResponse, ProviderUsage


def _units(count: int) -> tuple:
    return tuple(
        TranslationUnit(
            project_id="p",
            adapter_id="gettext",
            relative_path="ui.mo",
            logical_key=f"k{index}",
            source_text=f"Строка {index}",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        for index in range(count)
    )


def _response(texts: Sequence[str]) -> ProviderResponse:
    body = "\n".join(
        [*(f"[{i} ] {text}".replace(" ]", "]") for i, text in enumerate(texts, 1)),
         "---END---"]
    )
    return ProviderResponse(body, finish_reason="stop", usage=ProviderUsage(10, 10))


class _Provider:
    def __init__(self, script) -> None:
        self.script = script
        self.calls = []

    def translate(self, prompt: str, batch: Sequence[TranslationUnit]) -> ProviderResponse:
        self.calls.append(len(batch))
        result = self.script(len(self.calls), batch)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def batch_sizes(self):
        return tuple(self.calls)


class _Case(unittest.TestCase):
    def orchestrator(self, root: Path, provider, **changes) -> BatchOrchestrator:
        options = dict(sleep=lambda _s: None)
        options.update(changes)
        return BatchOrchestrator(
            provider,
            PromptComposer("Translate."),
            JsonCheckpoint(root / "checkpoint.json"),
            **options,
        )


class ReadTimeoutSplitsTests(_Case):
    def test_large_batch_read_timeout_bisects_instead_of_failing_all(self) -> None:
        """真机形态：整批读超时，拆成两半之后都成功。"""
        def script(call, batch):
            if len(batch) > 4:
                return ReadTimeoutError("timed out after 120s")
            return _response([f"译文{i}" for i in range(len(batch))])

        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(script)
            source = _units(8)
            result = self.orchestrator(Path(temp), provider).run(source)

        self.assertEqual((), result.failed, "拆开之后每一条都该成功")
        self.assertEqual(8, len([r for r in result.results if r.state == "succeeded"]))
        # 先按 8 条同尺寸重试到耗尽（1 + max_transient_retries），再二分成 4+4。
        self.assertEqual(8, provider.batch_sizes[0])
        self.assertIn(4, provider.batch_sizes)
        self.assertLess(max(provider.batch_sizes[-2:]), 8)

    def test_bisection_recurses_until_the_batch_is_small_enough(self) -> None:
        def script(call, batch):
            if len(batch) > 1:
                return ReadTimeoutError("timed out")
            return _response(["译文"])

        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(script)
            result = self.orchestrator(Path(temp), provider).run(_units(4))

        self.assertEqual((), result.failed)
        self.assertEqual({4, 2, 1}, set(provider.batch_sizes))

    def test_single_unit_read_timeout_still_fails(self) -> None:
        # 缩到 1 条还超时就是这条本身的问题，不能无限拆。
        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(lambda call, batch: ReadTimeoutError("timed out"))
            result = self.orchestrator(Path(temp), provider).run(_units(1))
        self.assertEqual(1, len(result.failed))
        self.assertEqual({1}, set(provider.batch_sizes))

    def test_split_is_recorded_as_split_required_not_failed(self) -> None:
        def script(call, batch):
            if len(batch) > 1:
                return ReadTimeoutError("timed out")
            return _response(["译文"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = JsonCheckpoint(root / "checkpoint.json")
            BatchOrchestrator(
                _Provider(script),
                PromptComposer("Translate."),
                checkpoint,
                sleep=lambda _s: None,
            ).run(_units(2))
            states = [event["state"] for event in checkpoint.batches]
        self.assertIn("split_required", states)
        reasons = [
            event["reason"] for event in checkpoint.batches
            if event["state"] == "split_required"
        ]
        self.assertTrue(any("read timeout" in reason for reason in reasons))


class RateLimitMustNotSplitTests(_Case):
    def test_429_exhaustion_fails_the_whole_batch_without_splitting(self) -> None:
        """反证：429 缩批会在一个正在限流的端点上打出更多请求。

        这条守的是**不做什么**。没有它，「读超时缩批」很容易被顺手推广成
        「所有瞬时错误都缩批」，那正是实测被证伪的做法。
        """
        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(lambda call, batch: TransientProviderError("429"))
            result = self.orchestrator(Path(temp), provider).run(_units(8))

        self.assertEqual(8, len(result.failed))
        # 只有同尺寸重试：1 次 + max_transient_retries(2) = 3 次，全是 8 条。
        self.assertEqual((8, 8, 8), provider.batch_sizes)

    def test_connection_error_is_not_a_read_timeout(self) -> None:
        # 建连失败说明端点整体不可用，拆开重投毫无意义。
        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(
                lambda call, batch: TransientProviderError("connection refused")
            )
            result = self.orchestrator(Path(temp), provider).run(_units(8))
        self.assertEqual(8, len(result.failed))
        self.assertEqual((8, 8, 8), provider.batch_sizes)


class BisectionBudgetTests(_Case):
    def test_budget_is_not_reset_by_recursion(self) -> None:
        """预算只减不增 —— 这正是旧实现的病根。

        每次递归 `_process` 都会把 `transient_attempt` 归零；如果缩批预算也跟着
        重置，深度就不受控。这里让**所有**尺寸都超时，断言请求数有界。
        """
        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(lambda call, batch: ReadTimeoutError("timed out"))
            result = self.orchestrator(
                Path(temp), provider, max_timeout_splits=2
            ).run(_units(8))

        self.assertEqual(8, len(result.failed))
        # 深度 2：8(×3) → 4(×3)+4(×3) → 2(×3)×4 = 3 + 6 + 12 = 21 次。
        # 若预算被重置，会一路拆到 1 条、请求数显著更多。
        self.assertEqual(21, len(provider.batch_sizes))
        self.assertNotIn(1, provider.batch_sizes, "预算耗尽后不该继续拆")

    def test_zero_budget_disables_splitting_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(lambda call, batch: ReadTimeoutError("timed out"))
            result = self.orchestrator(
                Path(temp), provider, max_timeout_splits=0
            ).run(_units(8))
        self.assertEqual(8, len(result.failed))
        self.assertEqual((8, 8, 8), provider.batch_sizes)

    def test_negative_budget_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                self.orchestrator(
                    Path(temp), _Provider(lambda *_: None), max_timeout_splits=-1
                )

    def test_successful_sub_batches_are_checkpointed_immediately(self) -> None:
        """拆出来成功的子批必须立刻落盘 —— 恢复时不能重复计费。"""
        def script(call, batch):
            if len(batch) > 2:
                return ReadTimeoutError("timed out")
            return _response([f"译文{i}" for i in range(len(batch))])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = _Provider(script)
            source = _units(4)
            self.orchestrator(root, provider).run(source)

            resumed = _Provider(
                lambda *_: AssertionError("恢复时不该再发任何请求")
            )
            result = self.orchestrator(root, resumed).run(source)
        self.assertEqual(0, result.requests)
        self.assertEqual((), resumed.batch_sizes)


class ProviderClassificationTests(unittest.TestCase):
    def test_read_timeout_is_a_transient_error(self) -> None:
        # 子类关系必须成立，否则既有的「瞬时错误退避重试」路径会漏掉它。
        self.assertTrue(issubclass(ReadTimeoutError, TransientProviderError))

    def test_socket_timeout_maps_to_read_timeout(self) -> None:
        import socket

        from localizer.adapters.providers.openai_compatible import (
            OpenAICompatibleProvider,
            OpenAICompatibleSettings,
        )

        def transport(url, headers, body, timeout):
            raise socket.timeout("timed out")

        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://example.invalid",
                model="m",
                api_key_env="R02_FAKE_KEY",
            ),
            transport=transport,
        )
        import os

        os.environ["R02_FAKE_KEY"] = "NOTAREALKEY"
        try:
            with self.assertRaises(ReadTimeoutError):
                provider.translate("prompt", _units(1))
        finally:
            del os.environ["R02_FAKE_KEY"]

    def test_connection_error_stays_a_plain_transient_error(self) -> None:
        import os

        from localizer.adapters.providers.openai_compatible import (
            OpenAICompatibleProvider,
            OpenAICompatibleSettings,
        )

        def transport(url, headers, body, timeout):
            raise ConnectionError("refused")

        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url="https://example.invalid",
                model="m",
                api_key_env="R02_FAKE_KEY",
            ),
            transport=transport,
        )
        os.environ["R02_FAKE_KEY"] = "NOTAREALKEY"
        try:
            with self.assertRaises(TransientProviderError) as ctx:
                provider.translate("prompt", _units(1))
            self.assertNotIsInstance(ctx.exception, ReadTimeoutError)
        finally:
            del os.environ["R02_FAKE_KEY"]


if __name__ == "__main__":
    unittest.main()
