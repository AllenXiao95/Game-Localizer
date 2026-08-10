"""§7.7 四类规则模型里此前缺失的两类（R12）。

`PlaceholderRule` 与 `ValidationRule` 先于 `FilterRule` / `NormalizationRule`
落地。loader 对未知顶层键直接报错，防止拼错或尚未支持的规则被静默忽略。

两类规则的风险形状完全不同，实现上也因此不对称：

- `FilterRule` 是**减法**：把词条移出翻译范围。风险是「悄悄少译一片」，
  所以每条规则强制要求 `reason`，且必须至少给一个匹配条件 —— 一条什么都不写的
  规则会匹配全部词条，把整个项目静默跳过。计划里单列 `filtered_units`，
  不许混进 `tm_hits` 或 `embedded`。
- `NormalizationRule` 是**改写**：它动的是最终写进产物的文本。风险是不收敛
  （每跑一次多加一个空格），以及让「从产物反算 QA」的复核与真实构建产生分歧
  （这正是 `mappings_empty` 这个标志存在的原因）。所以这里做到不动点并对
  不收敛显式报错，而不是默默取第一次的结果。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


class RuleDefinitionError(ValueError):
    """规则本身写错了。加载期就要炸，不能带进运行期。"""


@dataclass(frozen=True)
class FilterRule:
    """把匹配的文件或词条移出翻译范围。

    四个条件是**合取**：都给就必须都命中。这与「多条规则之间是析取」共同构成
    「精确表达一小撮例外」的能力，而不是一把能扫掉半个项目的大扫帚。
    """

    id: str
    reason: str
    adapter_id: Optional[str] = None
    path_glob: Optional[str] = None
    key_pattern: Optional[str] = None
    source_pattern: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.id:
            raise RuleDefinitionError("filter rule requires a non-empty id")
        if not (self.reason or "").strip():
            raise RuleDefinitionError(
                f"filter rule {self.id!r} requires a reason: 跳过词条是不可见的减法，"
                f"没有理由就无法在下一次审计时判断它还该不该在"
            )
        if not any(
            (self.adapter_id, self.path_glob, self.key_pattern, self.source_pattern)
        ):
            raise RuleDefinitionError(
                f"filter rule {self.id!r} has no condition and would skip every unit"
            )
        for field in ("key_pattern", "source_pattern"):
            pattern = getattr(self, field)
            if pattern is None:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuleDefinitionError(
                    f"filter rule {self.id!r}: invalid {field}: {exc}"
                ) from exc

    def matches(
        self, *, adapter_id: str, relative_path: str, logical_key: str, source_text: str
    ) -> bool:
        if self.adapter_id and self.adapter_id != adapter_id:
            return False
        if self.path_glob and not fnmatch.fnmatchcase(relative_path, self.path_glob):
            return False
        if self.key_pattern and not re.fullmatch(self.key_pattern, logical_key):
            return False
        if self.source_pattern and not re.fullmatch(
            self.source_pattern, source_text, re.DOTALL
        ):
            return False
        return True


class FilterRuleSet:
    """多条 FilterRule 的析取。命中时返回是哪一条命中的，便于解释。"""

    def __init__(self, rules: Sequence[FilterRule] = ()) -> None:
        self.rules: Tuple[FilterRule, ...] = tuple(rules)
        duplicates = {
            rule.id for rule in self.rules if [r.id for r in self.rules].count(rule.id) > 1
        }
        if duplicates:
            raise RuleDefinitionError(
                f"duplicate filter rule ids: {', '.join(sorted(duplicates))}"
            )

    def __bool__(self) -> bool:
        return bool(self.rules)

    def match(self, unit) -> Optional[FilterRule]:
        for rule in self.rules:
            if rule.matches(
                adapter_id=unit.adapter_id,
                relative_path=unit.relative_path,
                logical_key=unit.logical_key,
                source_text=unit.source_text or "",
            ):
                return rule
        return None


@dataclass(frozen=True)
class NormalizationRule:
    """确定性格式修复。作用于译文，在任何判据之前。"""

    id: str
    pattern: str
    replacement: str
    adapter_id: Optional[str] = None
    path_glob: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.id:
            raise RuleDefinitionError("normalization rule requires a non-empty id")
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise RuleDefinitionError(
                f"normalization rule {self.id!r}: invalid pattern: {exc}"
            ) from exc
        if compiled.match(""):
            # 空匹配的 re.sub 会在每个字符间隙插入替换文本，"确定性格式修复"
            # 变成"把译文炸开"。这类规则一定是写错了。
            raise RuleDefinitionError(
                f"normalization rule {self.id!r}: pattern matches the empty string; "
                f"re.sub would insert the replacement between every character"
            )
        object.__setattr__(self, "_compiled", compiled)

    def applies_to(self, adapter_id: str, relative_path: str) -> bool:
        if self.adapter_id and self.adapter_id != adapter_id:
            return False
        if self.path_glob and not fnmatch.fnmatchcase(relative_path, self.path_glob):
            return False
        return True

    def apply(self, text: str) -> str:
        try:
            return self._compiled.sub(self.replacement, text)  # type: ignore[attr-defined]
        except re.error as exc:  # 替换模板里的坏反向引用只有 sub 时才暴露
            raise RuleDefinitionError(
                f"normalization rule {self.id!r}: invalid replacement: {exc}"
            ) from exc


# 迭代上限。它是**防挂住的兜底**，不是收敛判据 —— 这个区别是这段代码的全部要点。
#
# 原来是固定 3 轮 + 收尾一轮，于是「收不收敛」变成了「4 次以内到不动点没有」：
# 同一套规则、同一次运行，`collapse-spaces` 对 8 个空格的译文绿灯、9 个空格红灯
# （每轮减半，9 个需要 4 次以上）。更糟的是不收敛时返回的是**中间态**，
# preview 产物里写进去的既不是原文也不是不动点。
#
# 现在的判据是真正的终止条件：不动点 / 环 / 无界增长。上限只在规则病态到
# 需要上千轮时才生效，那种规则本来就该改（`  `→` ` 应该写成 ` {2,}`→` `）。
_MAX_NORMALIZATION_PASSES = 1024

# 文本增长到这个倍数（或这么多字符）就判为无界增长。环检测对增长型规则无效：
# `a`→`aa` 每轮都是新文本，永远不会重复。
_MAX_GROWTH_FACTOR = 4
_MIN_GROWTH_HEADROOM = 4096


class NormalizationRuleSet:
    def __init__(self, rules: Sequence[NormalizationRule] = ()) -> None:
        self.rules: Tuple[NormalizationRule, ...] = tuple(rules)
        duplicates = {
            rule.id
            for rule in self.rules
            if [r.id for r in self.rules].count(rule.id) > 1
        }
        if duplicates:
            raise RuleDefinitionError(
                f"duplicate normalization rule ids: {', '.join(sorted(duplicates))}"
            )

    def __bool__(self) -> bool:
        return bool(self.rules)

    def apply(self, text: str, *, adapter_id: str, relative_path: str):
        """迭代到不动点。返回 `(文本, 是否收敛)`。

        收敛判据是**真正的终止条件**，不是轮数：

        - 一轮之后没变 → 不动点，返回它；
        - 出现过的文本又出现 → 规则集在打转（`A→B`、`B→A`）；
        - 文本长到原文的数倍 → 无界增长（`a`→`aa`）。环检测对它无效，
          因为每轮都是新文本。

        不收敛时返回**原文**，不返回中间态。中间态既不是操作者写的那句，也不是
        规则想要的那句；把它写进产物意味着 preview 与 release 可能得到不同的
        文本，而两者都自称通过了同一套判据。调用方据此报 `normalization_unstable`。
        """
        active = [
            rule for rule in self.rules if rule.applies_to(adapter_id, relative_path)
        ]
        if not active:
            return text, True
        limit = max(len(text) * _MAX_GROWTH_FACTOR, len(text) + _MIN_GROWTH_HEADROOM)
        # 判据的完备性来自「不动点 + 环 + 增长」这三条，不来自轮数：不增长的
        # 规则集在长度受限的字符串空间里只有有限个状态，要么到不动点，要么重复
        # 出现某个中间态被环检测抓住。所以这里给足预算，宁可多跑几轮也不要把
        # 一个真会收敛的规则集误判成不稳定 —— 常见规则 1~2 轮就停，代价是零。
        budget = min(max(64, len(text) * 2), _MAX_NORMALIZATION_PASSES)
        seen = {text}
        current = text
        for _ in range(budget):
            nxt = current
            for rule in active:
                nxt = rule.apply(nxt)
            if nxt == current:
                return current, True
            if len(nxt) > limit or nxt in seen:
                return text, False
            seen.add(nxt)
            current = nxt
        return text, False
