"""源语言画像 —— 把「源语言残留」从硬编码西里尔抽象出来。

`ValidationRule` 原本写死 `CYRILLIC_RE = [\\u0400-\\u052f]`，只对俄译中成立。
换成英译中项目时这条 QA **恒真通过** —— 中文译文里本来就不会出现西里尔字母，
于是「源语言残留」这条检查等于没跑。M7 验收要求「至少一个不同源语言项目完成
扫描、翻译和 QA」，光换源语言标签而不换检测器，拿到的是一个空头保证。

两条设计要点：

1. **min_run**。俄语、谚文、假名的字符集与中文完全不交叉，出现一个就是残留
   （min_run=1）。拉丁字母不行：中文译文里合法地含专有名词（Panzer）、
   UI 缩写（HP、XP）、型号（IS-3）。所以英语 profile 要求连续 ≥2 个拉丁词
   才判残留，单个词放过。

2. **先剥离占位符**。英语 profile 的硬需求：译文里的 `$COUNTRY$`、
   `[Root.GetName]`、`%(count)d` 全是纯 ASCII 拉丁，不先掩码就会把每一个占位符
   都报成「源语言残留」，误报直接淹没报告。俄语 profile 从来不需要这一步 ——
   这正是抽象是否做对的试金石。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class LanguageProfile:
    """一种源语言在译文里「不该出现」的形态。"""

    code: str
    # 单个「疑似源语言片段」的正则。
    residue_pattern: str
    # 连续多少个片段才判为残留。见模块文档。
    min_run: int = 1
    label: str = ""

    def __post_init__(self) -> None:
        re.compile(self.residue_pattern)  # 早失败：模式写错在构造时就炸
        if self.min_run < 1:
            raise ValueError("min_run must be >= 1")

    @property
    def display(self) -> str:
        return self.label or self.code

    def find_runs(self, text: str) -> Tuple[str, ...]:
        """找出达到 min_run 的连续片段。

        「连续」允许中间只隔空白与常见标点 —— `Rearmament Program` 是两个词，
        `Panzer，还有` 里的 Panzer 是孤立一个。
        """
        pattern = re.compile(self.residue_pattern)
        runs: list[str] = []
        current: list[str] = []
        cursor = 0
        for match in pattern.finditer(text):
            if current:
                gap = text[cursor : match.start()]
                # 只隔空白或轻标点算同一段；隔了实义字符（比如中文）就断开。
                if not re.fullmatch(r"[\s.,;:!?'\"()\[\]/\\-]*", gap):
                    if len(current) >= self.min_run:
                        runs.append(" ".join(current))
                    current = []
            current.append(match.group(0))
            cursor = match.end()
        if current and len(current) >= self.min_run:
            runs.append(" ".join(current))
        return tuple(runs)


# 目标语言（中文）的字符与全角标点。构造片段模式时要把它们排除在 token 之外，
# 否则「重型坦克КВ-1装甲」会被当成一整个 token。
_TARGET_SIDE = r"\s　-〿一-鿿＀-￯‘’“”"


def token_pattern(script_class: str) -> str:
    """含目标脚本字符的**整个 token**。

    片段必须是整词而不是脚本字符本身的连续段，理由有两条：

    - 白名单是按整词写的。`КВ-1` 里的连字符与数字不属于西里尔字符类，
      只取脚本段会得到 `КВ`，白名单永远命中不了。
    - 夹带必须能被发现。`xКВ-1x` 若只取 `КВ` 就会命中白名单里的 `КВ-1` 而放行，
      这正是 H3 修过的「子串剥离」漏洞的另一种形态。

    所以 token = 一段连续的「非空白、非中日韩、非全角标点」字符，其中至少含一个
    目标脚本字符。ASCII 括号等留在 token 里，由调用方 strip 掉。
    """
    other = f"[^{_TARGET_SIDE}]"
    return f"{other}*{script_class}{other}*"


_CYRILLIC = r"[Ѐ-ԯ]"
_LATIN = r"[A-Za-zÀ-ɏ]"

# 内建画像。key 是语言主码（BCP-47 的第一段）。
BUILTIN_PROFILES: Dict[str, LanguageProfile] = {
    "ru": LanguageProfile("ru", token_pattern(_CYRILLIC), 1, "Cyrillic"),
    "uk": LanguageProfile("uk", token_pattern(_CYRILLIC), 1, "Cyrillic"),
    "be": LanguageProfile("be", token_pattern(_CYRILLIC), 1, "Cyrillic"),
    # 拉丁字母在中文译文里合法出现的场合太多（专名 Panzer、缩写 HP/XP、
    # 型号 IS-3），单个 token 一律放过，连续两个 token 才判残留。
    "en": LanguageProfile("en", token_pattern(_LATIN), 2, "Latin"),
    "de": LanguageProfile("de", token_pattern(_LATIN), 2, "Latin"),
    "fr": LanguageProfile("fr", token_pattern(_LATIN), 2, "Latin"),
    "es": LanguageProfile("es", token_pattern(_LATIN), 2, "Latin"),
    "ko": LanguageProfile("ko", token_pattern(r"[가-힣ᄀ-ᇿ]"), 1, "Hangul"),
    "ja": LanguageProfile("ja", token_pattern(r"[぀-ヿㇰ-ㇿ]"), 1, "Kana"),
    "ar": LanguageProfile("ar", token_pattern(r"[؀-ۿ]"), 1, "Arabic"),
    "th": LanguageProfile("th", token_pattern(r"[฀-๿]"), 1, "Thai"),
    "el": LanguageProfile("el", token_pattern(r"[Ͱ-Ͽἀ-῿]"), 1, "Greek"),
    "he": LanguageProfile("he", token_pattern(r"[֐-׿]"), 1, "Hebrew"),
}

# 目标语言是中文时不做残留检查的语言（同文种，检测无意义）。
_CJK_LIKE = {"zh", "yue", "wuu"}


def normalize_locale(locale: str) -> str:
    """ru-RU -> ru，zh-Hans -> zh，en_US -> en。"""
    return (locale or "").replace("_", "-").split("-")[0].strip().lower()


def for_locale(locale: str) -> Optional[LanguageProfile]:
    """按 locale 取内建画像。未知语言返回 None（不做残留检查，但要能诊断）。"""
    code = normalize_locale(locale)
    if not code or code in _CJK_LIKE:
        return None
    return BUILTIN_PROFILES.get(code)


def build_profile(
    *,
    source_locale: str = "",
    code: Optional[str] = None,
    residue_pattern: Optional[str] = None,
    min_run: Optional[int] = None,
    label: str = "",
) -> Optional[LanguageProfile]:
    """从配置构造画像：可以只写语言码走内建，也可以完全自定义正则。"""
    base = for_locale(code or source_locale)
    if residue_pattern is None:
        if base is None:
            return None
        if min_run is None and not label:
            return base
        return LanguageProfile(
            base.code,
            base.residue_pattern,
            min_run if min_run is not None else base.min_run,
            label or base.label,
        )
    return LanguageProfile(
        code or normalize_locale(source_locale) or "custom",
        residue_pattern,
        min_run if min_run is not None else 1,
        label or "custom",
    )


def available_profiles() -> Tuple[str, ...]:
    return tuple(sorted(BUILTIN_PROFILES))
