from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .filtering import (
    FilterRule,
    FilterRuleSet,
    NormalizationRule,
    NormalizationRuleSet,
    RuleDefinitionError,
)
from .language_profile import LanguageProfile, available_profiles, build_profile
from .validation import RuleScope, ValidationRule


class RulesLoadError(RuntimeError):
    pass


# `cyrillic:` 是 §7.7.1 时期只支持俄语时的段名。多语言画像落地后正式名是
# `language:`，但旧段名必须继续可用 —— 现网项目的 rules.yaml 都还在用它。
_TOP_LEVEL_SECTIONS = {
    "schema_version",
    "cyrillic",
    "language",
    # §7.7 的另外两类规则（R12）。之前写这两个键会被 loader 直接判未知段报错，
    # 那是刻意的 —— 内核不认识的键静默丢弃比报错更危险。现在它们真的生效了。
    "filter_rules",
    "normalization_rules",
}

_FILTER_KEYS = {"id", "reason", "adapter_id", "path_glob", "key_pattern", "source_pattern"}
_NORMALIZATION_KEYS = {"id", "pattern", "replacement", "adapter_id", "path_glob"}


def _residue_section(raw: Mapping[str, Any], rules_path: Path) -> tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]:
    """返回 (残留规则段, 语言段)。两个段名不能同时出现。"""
    legacy = raw.get("cyrillic")
    modern = raw.get("language")
    if legacy is not None and modern is not None:
        raise RulesLoadError(
            f"rules {rules_path}: use either the legacy `cyrillic:` section or "
            f"`language:`, not both"
        )
    if modern is not None:
        if not isinstance(modern, Mapping):
            raise RulesLoadError("language rules must be a mapping")
        residue = modern.get("residue", {})
        if not isinstance(residue, Mapping):
            raise RulesLoadError("language.residue must be a mapping")
        return residue, modern
    if legacy is None:
        return {}, None
    if not isinstance(legacy, Mapping):
        raise RulesLoadError("cyrillic rules must be a mapping")
    return legacy, None


def _validate_types(section: Mapping[str, Any], label: str) -> tuple[list, dict]:
    # 类型必须显式校验。原实现直接把值交给 ValidationRule，于是 YAML 少写一个 "- "
    # 让 exact_allowlist 退化成标量时，tuple("Танк") 变成逐字符白名单，
    # 未翻译原文可以通过 RELEASE 零容忍闸门；int/None 则抛裸 TypeError。
    allowlist = section.get("exact_allowlist", [])
    if not isinstance(allowlist, list) or not all(
        isinstance(item, str) for item in allowlist
    ):
        raise RulesLoadError(
            f"{label}.exact_allowlist must be a list of strings "
            "(a bare scalar silently degrades into a per-character allowlist)"
        )
    mappings = section.get("mappings", {})
    if not isinstance(mappings, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mappings.items()
    ):
        raise RulesLoadError(f"{label}.mappings must be a mapping of string to string")
    return allowlist, dict(mappings)


def load_validation_rule(
    path: Path, *, source_locale: Optional[str] = None
) -> ValidationRule:
    """加载校验规则。

    `source_locale` 由调用方从项目配置传入，用来在 rules.yaml 没有显式声明
    `language.source_profile` 时选择正确的源语言画像。不传时沿用俄语画像 ——
    这是**兼容默认值**，不是设计意图（见 ValidationRule.__init__ 的注释）。
    """
    rules_path = Path(path).resolve()
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RulesLoadError(f"cannot load rules {rules_path}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise RulesLoadError(f"rules {rules_path} must use schema_version: 1")
    # 未知顶层键报错而不是静默丢弃，避免作者误以为拼错的规则已经生效。
    unknown = set(raw) - _TOP_LEVEL_SECTIONS
    if unknown:
        raise RulesLoadError(
            f"unknown top-level rule sections: {', '.join(sorted(unknown))}"
        )

    section, language = _residue_section(raw, rules_path)
    label = "language.residue" if language is not None else "cyrillic"
    allowlist, mappings = _validate_types(section, label)

    scopes = []
    for item in section.get("scopes", []):
        if not isinstance(item, Mapping) or "pattern" not in item:
            raise RulesLoadError(f"each {label} scope requires pattern")
        scopes.append(
            RuleScope(
                pattern=str(item["pattern"]),
                adapter_id=item.get("adapter_id"),
                path_glob=item.get("path_glob"),
            )
        )

    profile = _resolve_profile(language, source_locale, rules_path)
    return ValidationRule(
        profile=profile,
        residue_exact_allowlist=allowlist,
        residue_mappings=mappings,
        residue_scopes=scopes,
        normalization=load_normalization_rules(rules_path, raw=raw),
    )


def _rule_mappings(raw: Mapping[str, Any], key: str, allowed: set, rules_path: Path):
    items = raw.get(key, [])
    if items is None:
        return []
    if not isinstance(items, list):
        raise RulesLoadError(f"rules {rules_path}: {key} must be a list")
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise RulesLoadError(f"rules {rules_path}: {key}[{index}] must be a mapping")
        unknown = set(item) - allowed
        if unknown:
            # 拼错的字段静默丢弃 = 规则半生效。整个 loader 的既定立场是报错。
            raise RulesLoadError(
                f"rules {rules_path}: {key}[{index}] has unknown fields: "
                f"{', '.join(sorted(unknown))}"
            )
        for name, value in item.items():
            if value is not None and not isinstance(value, str):
                raise RulesLoadError(
                    f"rules {rules_path}: {key}[{index}].{name} must be a string"
                )
        result.append(item)
    return result


def load_filter_rules(
    path: Path, *, raw: Optional[Mapping[str, Any]] = None
) -> FilterRuleSet:
    rules_path = Path(path).resolve()
    payload = raw if raw is not None else _read_raw(rules_path)
    items = _rule_mappings(payload, "filter_rules", _FILTER_KEYS, rules_path)
    try:
        return FilterRuleSet(
            [
                FilterRule(
                    id=str(item.get("id", "")),
                    reason=str(item.get("reason", "")),
                    adapter_id=item.get("adapter_id"),
                    path_glob=item.get("path_glob"),
                    key_pattern=item.get("key_pattern"),
                    source_pattern=item.get("source_pattern"),
                )
                for item in items
            ]
        )
    except RuleDefinitionError as exc:
        raise RulesLoadError(f"rules {rules_path}: {exc}") from exc


def load_normalization_rules(
    path: Path, *, raw: Optional[Mapping[str, Any]] = None
) -> NormalizationRuleSet:
    rules_path = Path(path).resolve()
    payload = raw if raw is not None else _read_raw(rules_path)
    items = _rule_mappings(
        payload, "normalization_rules", _NORMALIZATION_KEYS, rules_path
    )
    for index, item in enumerate(items):
        for required in ("id", "pattern", "replacement"):
            if item.get(required) is None:
                raise RulesLoadError(
                    f"rules {rules_path}: normalization_rules[{index}] "
                    f"requires {required}"
                )
    try:
        return NormalizationRuleSet(
            [
                NormalizationRule(
                    id=str(item["id"]),
                    pattern=str(item["pattern"]),
                    replacement=str(item["replacement"]),
                    adapter_id=item.get("adapter_id"),
                    path_glob=item.get("path_glob"),
                )
                for item in items
            ]
        )
    except RuleDefinitionError as exc:
        raise RulesLoadError(f"rules {rules_path}: {exc}") from exc


def _read_raw(rules_path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RulesLoadError(f"cannot load rules {rules_path}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise RulesLoadError(f"rules {rules_path} must use schema_version: 1")
    unknown = set(raw) - _TOP_LEVEL_SECTIONS
    if unknown:
        raise RulesLoadError(
            f"unknown top-level rule sections: {', '.join(sorted(unknown))}"
        )
    return raw


def _resolve_profile(
    language: Optional[Mapping[str, Any]],
    source_locale: Optional[str],
    rules_path: Path,
) -> Optional[LanguageProfile]:
    declared = (language or {}).get("source_profile")
    pattern = (language or {}).get("residue_pattern")
    min_run = (language or {}).get("residue", {}).get("min_run") if language else None
    if declared is not None and not isinstance(declared, str):
        raise RulesLoadError("language.source_profile must be a string")
    if pattern is not None and not isinstance(pattern, str):
        raise RulesLoadError("language.residue_pattern must be a string")
    if min_run is not None and (not isinstance(min_run, int) or min_run < 1):
        raise RulesLoadError("language.residue.min_run must be an integer >= 1")

    try:
        profile = build_profile(
            source_locale=source_locale or "",
            code=declared,
            residue_pattern=pattern,
            min_run=min_run,
        )
    except (ValueError, TypeError) as exc:
        raise RulesLoadError(f"rules {rules_path}: invalid language profile: {exc}") from exc

    if profile is None and declared:
        raise RulesLoadError(
            f"rules {rules_path}: unknown source_profile {declared!r}; "
            f"built-in profiles: {list(available_profiles())}. "
            f"Provide language.residue_pattern to define a custom one."
        )
    return profile
