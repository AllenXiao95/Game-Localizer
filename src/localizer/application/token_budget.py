from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence, Tuple

from localizer.application.prompt import PromptComposer
from localizer.domain.translation_unit import TranslationUnit


TokenCounter = Callable[[str], int]


_ASCII_RUN = re.compile(r"[\x00-\x7f]+|[^\x00-\x7f]")


def conservative_token_count(text: str) -> int:
    """不依赖具体 tokenizer 的保守计数。

    ASCII 连续片段按约 4 字符/token 估算；非 ASCII 字符逐个计数。它不是计费
    统计，只用于未配置本地 tokenizer 时避免重新退化成固定条数分批。
    """

    if not text:
        return 0
    total = 0
    for match in _ASCII_RUN.finditer(text):
        value = match.group(0)
        if value.isascii():
            total += max(1, math.ceil(len(value) / 4))
        else:
            total += 1
    return total


@dataclass(frozen=True)
class BatchTokenEstimate:
    input_tokens: int
    output_tokens: int
    input_budget: int
    output_budget: int

    @property
    def fits(self) -> bool:
        return (
            self.input_tokens <= self.input_budget
            and self.output_tokens <= self.output_budget
        )


class TokenBudgetBatchPlanner:
    """按完整 Prompt 与预计响应 Token 预算生成文件内批次。"""

    # 防止异常短文本在百万级上下文下形成无界 JSON/编号响应。它不是用户调优项，
    # 也不替代 context_window/max_output_tokens 的预算决策。
    MAX_UNITS_PER_BATCH = 256

    def __init__(
        self,
        composer: PromptComposer,
        *,
        context_window: int,
        max_output_tokens: int,
        count_tokens: TokenCounter = conservative_token_count,
    ) -> None:
        if context_window <= 0:
            raise ValueError("context_window must be positive")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if max_output_tokens >= context_window:
            raise ValueError("max_output_tokens must be smaller than context_window")
        self.composer = composer
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.count_tokens = count_tokens

        available_input = context_window - max_output_tokens
        # 给 chat wrapper、服务商侧特殊 token 和 tokenizer 误差留空间。上下文很小时
        # 不允许安全余量吃掉超过可用输入的 1/4；超长上下文最多固定留 4096。
        self.safety_margin_tokens = min(
            4096,
            max(32, context_window // 100),
            max(1, available_input // 4),
        )
        self.input_budget = available_input - self.safety_margin_tokens
        output_margin = min(
            512,
            max(8, max_output_tokens // 20),
            max(1, max_output_tokens // 4),
        )
        self.output_budget = max_output_tokens - output_margin

    def estimate(self, units: Sequence[TranslationUnit]) -> BatchTokenEstimate:
        prompt = self.composer.compose(units)
        # 译文不可在请求前精确获得。以源文构造同协议响应，再保守增加 20%，覆盖
        # 目标语言膨胀、编号和结束哨兵；正式计费仍以 Provider usage 为准。
        response_shape = "\n".join(
            [
                *(f"[{index}] {unit.source_text}" for index, unit in enumerate(units, 1)),
                "---END---",
            ]
        )
        return BatchTokenEstimate(
            input_tokens=self.count_tokens(prompt),
            output_tokens=math.ceil(self.count_tokens(response_shape) * 1.2),
            input_budget=self.input_budget,
            output_budget=self.output_budget,
        )

    def plan(
        self, units: Sequence[TranslationUnit]
    ) -> Tuple[Tuple[TranslationUnit, ...], ...]:
        """返回保持输入顺序的动态批次。

        每批先尝试内部熔断上限，再用二分找到预算内的最大前缀，避免对大型文件
        逐条重复编码完整 Prompt/背景/术语表。单条本身超预算时仍单独返回，由编排器
        在调用 Provider 前写入 failed/QA，而不是丢失该词条或发送已知超限请求。
        """

        planned = []
        cursor = 0
        total = len(units)
        while cursor < total:
            maximum = min(self.MAX_UNITS_PER_BATCH, total - cursor)
            candidate = tuple(units[cursor: cursor + maximum])
            if self.estimate(candidate).fits:
                size = maximum
            else:
                low, high = 1, maximum - 1
                size = 1
                while low <= high:
                    middle = (low + high) // 2
                    probe = tuple(units[cursor: cursor + middle])
                    if self.estimate(probe).fits:
                        size = middle
                        low = middle + 1
                    else:
                        high = middle - 1
            planned.append(tuple(units[cursor: cursor + size]))
            cursor += size
        return tuple(planned)
