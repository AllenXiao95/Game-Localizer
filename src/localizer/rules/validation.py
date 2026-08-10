from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .filtering import NormalizationRuleSet
from .language_profile import BUILTIN_PROFILES, LanguageProfile
from .placeholder import TOKEN_PATTERN, PlaceholderMap, PlaceholderRule


# \u517c\u5bb9\u4fdd\u7559\uff1a\u4ecd\u6709\u4ee3\u7801\u5f15\u7528\u8fd9\u4e2a\u5e38\u91cf\u3002\u771f\u6b63\u7684\u5224\u636e\u5728 LanguageProfile\u3002
CYRILLIC_RE = re.compile(r"[\u0400-\u052f]")


@dataclass(frozen=True)
class QAIssue:
    code: str
    severity: str
    message: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class RuleScope:
    pattern: str
    adapter_id: Optional[str] = None
    path_glob: Optional[str] = None

    def applies(self, text: str, adapter_id: str, relative_path: str) -> bool:
        if self.adapter_id and self.adapter_id != adapter_id:
            return False
        if self.path_glob and not fnmatch.fnmatchcase(relative_path, self.path_glob):
            return False
        return re.fullmatch(self.pattern, text) is not None


@dataclass(frozen=True)
class ValidationSummary:
    text: str
    issues: Tuple[QAIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


class ValidationRule:
    def __init__(
        self,
        *,
        profile: Optional[LanguageProfile] = None,
        residue_exact_allowlist: Optional[Iterable[str]] = None,
        residue_mappings: Optional[Mapping[str, str]] = None,
        residue_scopes: Optional[Sequence[RuleScope]] = None,
        # 旧参数名。rules.yaml 的 cyrillic 段与既有调用方仍在用，保留为别名。
        cyrillic_exact_allowlist: Iterable[str] = (),
        cyrillic_mappings: Optional[Mapping[str, str]] = None,
        cyrillic_scopes: Sequence[RuleScope] = (),
        # §7.7 的第四类规则（R12）。默认空集，既有调用方行为一字不变。
        normalization: Optional["NormalizationRuleSet"] = None,
    ) -> None:
        self.normalization = normalization or NormalizationRuleSet()
        self.placeholder_rule = PlaceholderRule()
        # 不指定画像时沿用俄语 —— 这是**兼容默认值**，不是设计意图。
        # 正常路径上 loader 会按项目的 languages.source 显式给出画像。
        self.profile = profile if profile is not None else BUILTIN_PROFILES["ru"]
        self.residue_exact_allowlist = tuple(
            residue_exact_allowlist
            if residue_exact_allowlist is not None
            else cyrillic_exact_allowlist
        )
        self.residue_mappings = dict(
            residue_mappings if residue_mappings is not None else (cyrillic_mappings or {})
        )
        self.residue_scopes = tuple(
            residue_scopes if residue_scopes is not None else cyrillic_scopes
        )

    # 旧属性名保留只读别名，避免既有代码与测试大面积改动。
    @property
    def cyrillic_exact_allowlist(self) -> Tuple[str, ...]:
        return self.residue_exact_allowlist

    @property
    def cyrillic_mappings(self) -> Mapping[str, str]:
        return self.residue_mappings

    @property
    def cyrillic_scopes(self) -> Tuple[RuleScope, ...]:
        return self.residue_scopes

    def validate_masked_translation(
        self,
        translation: str,
        placeholder_map: PlaceholderMap,
        *,
        preserve_order: bool = False,
    ) -> Tuple[QAIssue, ...]:
        expected = list(placeholder_map.tokens)
        actual = list(self.placeholder_rule.find_tokens(translation))
        issues = []
        expected_counts = Counter(expected)
        actual_counts = Counter(actual)
        if expected_counts != actual_counts:
            missing = list((expected_counts - actual_counts).elements())
            extra = list((actual_counts - expected_counts).elements())
            issues.append(
                QAIssue(
                    code="placeholder_mismatch",
                    severity="error",
                    message="placeholder multiset differs from source",
                    details={"missing": missing, "extra": extra},
                )
            )
        elif preserve_order and expected != actual:
            issues.append(
                QAIssue(
                    code="placeholder_order",
                    severity="error",
                    message="placeholder order differs from source",
                    details={"expected": expected, "actual": actual},
                )
            )
        return tuple(issues)

    def validate_text(
        self,
        text: str,
        *,
        adapter_id: str,
        relative_path: str,
        allow_source_text: bool = False,
    ) -> ValidationSummary:
        resolved = self.apply_residue_mappings(text)
        issues = []
        # NormalizationRule 在**所有判据之前**跑：它的定位是确定性格式修复，
        # 修完之后的文本才是要被检查、也是要被写进产物的那一份。
        resolved, converged = self.normalization.apply(
            resolved, adapter_id=adapter_id, relative_path=relative_path
        )
        if not converged:
            issues.append(
                QAIssue(
                    "normalization_unstable",
                    "error",
                    "normalization rules do not reach a fixed point",
                    {"text": resolved},
                )
            )
        if not resolved.strip():
            issues.append(QAIssue("empty_translation", "error", "translation is empty", {}))
        if not allow_source_text:
            remaining = self._unallowed_residue(resolved, adapter_id, relative_path)
            if remaining:
                issues.append(
                    QAIssue(
                        "source_language_residue",
                        "error",
                        f"translation contains {self.profile.display} text "
                        f"not allowed by rules.yaml",
                        {"fragments": remaining, "profile": self.profile.code},
                    )
                )
        return ValidationSummary(resolved, tuple(issues))

    @property
    def rewrites_text(self) -> bool:
        """本规则集是否会改写译文。

        改写过就不能「从产物反算 QA」—— 任何基于渲染结果的复核都会**二次应用**
        这些规则，结论与真实构建不一致。审查索引的 `mappings_empty` 就是这个
        判断的载体，加了 NormalizationRule 之后它必须一起算进来。
        """
        return bool(self.residue_mappings) or bool(self.normalization)

    def apply_residue_mappings(self, text: str) -> str:
        result = text
        for source in sorted(self.residue_mappings, key=len, reverse=True):
            result = result.replace(source, self.residue_mappings[source])
        return result

    # 旧方法名保留别名。
    def apply_cyrillic_mappings(self, text: str) -> str:
        return self.apply_residue_mappings(text)

    def _unallowed_residue(
        self, text: str, adapter_id: str, relative_path: str
    ) -> Tuple[str, ...]:
        # 先把占位符整个抠掉再找残留。
        #
        # 这是拉丁系源语言的硬需求：译文里的 $COUNTRY$、[Root.GetName]、%(count)d
        # 全是纯 ASCII 拉丁，不处理就会把每一个占位符都报成「源语言残留」，
        # 误报直接淹没报告。俄语画像从来不需要这一步 —— 西里尔占位符不存在。
        #
        # 注意不能只 mask 就完事：掩码产出的 [PH_xxxxxxxx_0] 本身也是拉丁字符，
        # 换成 token 只是把一种误报替换成另一种。必须连 token 一起移除。
        masked = PlaceholderRule.for_adapter(adapter_id).mask(text).masked_text
        probe = TOKEN_PATTERN.sub(" ", masked)
        allowed_terms = {item.strip() for item in self.residue_exact_allowlist}
        fragments = []
        for fragment in self.profile.find_runs(probe):
            cleaned = fragment.strip(".,:;!?()[]{}\"'“”")
            if not cleaned or cleaned in allowed_terms:
                continue
            if any(
                scope.applies(cleaned, adapter_id, relative_path)
                for scope in self.residue_scopes
            ):
                continue
            fragments.append(cleaned)
        return tuple(fragments)

    def _unallowed_cyrillic(
        self, text: str, adapter_id: str, relative_path: str
    ) -> Tuple[str, ...]:
        # 先切片段再逐个整词比对白名单。
        #
        # 原实现是 candidate.replace(allowed, "") 全文子串剥离，名为 exact 实为
        # 无边界删除：allowlist 里放了 KB-1，KB-1234 和 xKB-1x 也一并放行，
        # §7.7.1「未命中即失败」在这类串上被击穿。更糟的是 YAML 少写一个 "- " 让
        # allowlist 退化成字符串时，逐字符白名单会把整词字符恰好齐全的未翻译原文
        # 剥空，通过 RELEASE 零容忍闸门。
        #
        # 需要词内片段放行的场景，用 cyrillic_scopes 的正则显式表达。
        allowed_terms = {item.strip() for item in self.cyrillic_exact_allowlist}
        fragments = []
        for match in re.finditer(r"[^\s]*[\u0400-\u052f][^\s]*", text):
            fragment = match.group(0).strip(".,:;!?()[]{}\"'“”")
            if not fragment or fragment in allowed_terms:
                continue
            if any(scope.applies(fragment, adapter_id, relative_path) for scope in self.cyrillic_scopes):
                continue
            fragments.append(fragment)
        return tuple(fragments)
