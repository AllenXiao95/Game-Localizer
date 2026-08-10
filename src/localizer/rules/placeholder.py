from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict, Mapping, Sequence, Tuple


# 与格式无关的通用占位符：printf、{name}/{0}、标签、换行。
# 顺序有意义——合并时 Adapter 预设排在前面，避免 generic 的 `%…` 分支把
# `$VALUE|*1$` 这类语法切碎。
GENERIC_PLACEHOLDERS: Tuple[str, ...] = (
    r"%\([^)]+\)(?:[-+#0]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa])?",
    r"%(?!\()[\-+#0]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]",
    r"%%",
    r"\{[A-Za-z_][A-Za-z0-9_.\[\]]*\}",
    r"\{\d+\}",
    r"</?[A-Za-z][^<>]*?>",
    r"\r\n|\r|\n",
)

# 向后兼容：仍有代码直接引用模块级常量。
PLACEHOLDER_PATTERN = re.compile("(?:" + "|".join(GENERIC_PLACEHOLDERS) + ")")

TOKEN_PATTERN = re.compile(r"\[PH_[0-9a-f]{8}_\d+\]")

# 严格 token 只认半角小写十六进制。但模型会把占位符「中文化」：换全角方括号、
# 大写、加空格、甚至同时保留一份半角的。这类变体逃过 find_tokens 的多重集比较
# （半角计数仍然 1:1 相等），restore 也不认识它们，于是字面量直达 .mo ——
# 玩家在游戏 UI 上会看到「【PH_d9e14c98_0】」。
#
# 这条宽松式只用于 restore **之后**的残留扫描，不参与掩码与回填，
# 因此宁可宽一点。白名单式的 [VARIANT]、[BUFF_0] 不会命中：必须有 PH 前缀
# 加 8 位十六进制。
TOKEN_RESIDUE_PATTERN = re.compile(
    r"[\[【〔（(]\s*[Pp][Hh][ _\-]?[0-9a-fA-F]{8}[ _\-]?\d+\s*[\]】〕）)]"
)

# 「看起来像占位符但没被任何模式识别」的探测式。
#
# 未识别的占位符是**静默假阴性**，比不支持更危险：mask 对它零匹配，
# round_trip 恒为 True（掩码为空、还原自然相等），多重集比对里根本不出现这一项。
# 结果是译文把 $NAME$ 改成 $名字$、把 §Y 吞掉，QA 全绿。
# 新格式接入时，这条探测式会在 QA 报告里显式列出「我没认识的东西」。
UNKNOWN_PLACEHOLDER_PATTERN = re.compile(
    r"\$[^\s$]{1,40}\$"          # Paradox $VAR$ / $VALUE|*1$
    r"|£[^\s£]{1,40}£"           # Paradox 图标 £energy£
    r"|§."                        # Paradox 颜色码 §Y … §!
    r"|\[[^\]\s]{1,40}\]"        # 作用域函数 [Root.GetName]
    r"|%[0-9]+\$[a-zA-Z]"        # 位置参数 %1$s
    r"|\{\{[^}]{1,40}\}\}"       # 双花括号模板
)

# adapter_id -> 该格式专有的占位符模式
_SYNTAX_REGISTRY: Dict[str, Tuple[str, ...]] = {}


def register_placeholder_syntax(adapter_id: str, patterns: Sequence[str]) -> None:
    """登记某种资源格式专有的占位符语法。

    Adapter 模块在导入时调用它，内核不需要知道有哪些格式。
    """
    for pattern in patterns:
        re.compile(pattern)  # 早失败：模式写错要在登记时就炸，不是运行到一半
    _SYNTAX_REGISTRY[adapter_id] = tuple(patterns)


def registered_syntax(adapter_id: str) -> Tuple[str, ...]:
    return _SYNTAX_REGISTRY.get(adapter_id, ())


# 掩码之后只剩这些字符的条目没有实义文本可翻译。
NON_TRANSLATABLE_CHARS = " \t\r\n.,:;-—/|()[]{}%#*+"


def has_meaningful_text(masked_text: str) -> bool:
    """给**已掩码**的文本用。原始源文请用 PlaceholderRule.is_translatable()。"""
    return bool(TOKEN_PATTERN.sub("", masked_text).strip(NON_TRANSLATABLE_CHARS))


@dataclass(frozen=True)
class PlaceholderMap:
    masked_text: str
    token_to_value: Mapping[str, str]

    @property
    def tokens(self) -> Tuple[str, ...]:
        return tuple(self.token_to_value)


class PlaceholderRule:
    def __init__(self, extra_patterns: Sequence[str] = ()) -> None:
        self.extra_patterns = tuple(extra_patterns)
        self._pattern = re.compile(
            "(?:" + "|".join((*self.extra_patterns, *GENERIC_PLACEHOLDERS)) + ")"
        )

    @classmethod
    def for_adapter(
        cls, adapter_id: str, *, project_extra: Sequence[str] = ()
    ) -> "PlaceholderRule":
        """按 adapter_id 取该格式的占位符预设，叠加项目级追加模式。

        源文与译文必须用**同一套**预设，否则多重集比对必然失配。
        """
        return cls((*project_extra, *registered_syntax(adapter_id)))

    def mask(self, text: str, *, namespace: str = "") -> PlaceholderMap:
        identity = sha256((namespace + "\x1f" + text).encode("utf-8")).hexdigest()[:8]
        values: Dict[str, str] = {}
        pieces = []
        cursor = 0
        for index, match in enumerate(self._pattern.finditer(text)):
            pieces.append(text[cursor : match.start()])
            token = f"[PH_{identity}_{index}]"
            pieces.append(token)
            values[token] = match.group(0)
            cursor = match.end()
        pieces.append(text[cursor:])
        return PlaceholderMap("".join(pieces), values)

    def restore(self, masked_text: str, placeholder_map: PlaceholderMap) -> str:
        restored = masked_text
        for token, value in placeholder_map.token_to_value.items():
            restored = restored.replace(token, value)
        return restored

    def extract(self, text: str) -> Tuple[str, ...]:
        return tuple(match.group(0) for match in self._pattern.finditer(text))

    def find_tokens(self, text: str) -> Tuple[str, ...]:
        return tuple(TOKEN_PATTERN.findall(text))

    def find_token_residue(self, text: str) -> Tuple[str, ...]:
        """restore 之后仍残留的占位符 token（含全角/大写/带空格等变体）。

        必须在 restore **之后**调用：U10/U11 的 rationale 就是「校验必须发生在
        restore 之后」这条时序约束。
        """
        return tuple(match.group(0) for match in TOKEN_RESIDUE_PATTERN.finditer(text))

    def find_unmasked_candidates(self, masked_text: str) -> Tuple[str, ...]:
        """掩码之后仍然「像占位符」的片段 —— 说明有语法没被任何模式覆盖。

        必须在 mask **之后**调用。命中只报 warning 不报 error：探测式必然
        宽于真实语法，用它阻断发布会误伤。它的价值是让未识别语法**可见**。
        """
        return tuple(
            match.group(0)
            for match in UNKNOWN_PLACEHOLDER_PATTERN.finditer(masked_text)
            if not TOKEN_PATTERN.fullmatch(match.group(0))
        )

    def is_translatable(self, source_text: str) -> bool:
        """源文掩掉占位符之后还剩实义文本吗？

        纯占位符条目（Paradox 的 `"$VALUE$"`、纯数字、纯符号）本来就不该被翻译，
        模型原样返回是**正确行为**，译文与源文必然相同。一律判 `untranslated`
        会大批误报并直接阻断 release，而这类条目任何键值型格式都有。

        接受**原始**源文，内部自行掩码 —— 调用方不必先掩。这条曾经是
        batch_orchestrator 的私有函数，local_build 没有对应守卫，于是同一批条目
        在翻译阶段被豁免、在构建阶段被重判成 `untranslated/machine` 落进零容忍的
        new_error，且无任何出口。判据必须只有一份。
        """
        return has_meaningful_text(self.mask(source_text).masked_text)

    def round_trip(self, text: str, *, namespace: str = "") -> bool:
        masked = self.mask(text, namespace=namespace)
        return self.restore(masked.masked_text, masked) == text
