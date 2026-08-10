from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.application.local_build import LocalBuildPipeline, ResourceBuild
from localizer.domain.translation_unit import TranslationUnit
from localizer.rules.filtering import FilterRuleSet
from localizer.rules.placeholder import PlaceholderRule
from localizer.rules.validation import ValidationRule


@dataclass(frozen=True)
class TranslationPlanFile:
    relative_path: str
    extracted_units: int
    tm_hits: int
    embedded_translations: int
    pending_units: int
    by_match_scope: Mapping[str, int]
    # FilterRule 移出翻译范围的词条（R12）。单列，不许混进 tm_hits 或 embedded ——
    # 「跳过」和「已经有译文」是完全不同的两件事，混在一起就再也看不出少译了多少。
    filtered_units: int = 0

    def as_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "extracted_units": self.extracted_units,
            "tm_hits": self.tm_hits,
            "embedded_translations": self.embedded_translations,
            "pending_units": self.pending_units,
            "by_match_scope": dict(self.by_match_scope),
            "filtered_units": self.filtered_units,
        }


@dataclass(frozen=True)
class TranslationPlan:
    fingerprint: str
    resources: Tuple[ResourceBuild, ...]
    translations: Mapping[str, str]
    # stable_identity -> 译文来源（match scope 或 "embedded"）。
    # QualityGate 据此区分「本次新增的缺陷」与「搬运过来的存量债」。
    provenance: Mapping[str, str]
    pending: Tuple[TranslationUnit, ...]
    files: Tuple[TranslationPlanFile, ...]
    tm_hits: int
    by_match_scope: Mapping[str, int]
    # stable_identity -> 命中的 FilterRule id。这些坐标既不送模型，也不参与
    # 构建期的 QA 判据 —— 否则「跳过」只是把噪声从翻译搬到报告里。
    filtered: Mapping[str, str] = field(default_factory=dict)

    @property
    def extracted_units(self) -> int:
        return sum(item.extracted_units for item in self.files)

    @property
    def pending_files(self) -> int:
        return sum(1 for item in self.files if item.pending_units)

    def as_dict(self) -> dict:
        return {
            "plan_fingerprint": self.fingerprint,
            "files_total": len(self.files),
            "files_pending": self.pending_files,
            "extracted_units": self.extracted_units,
            "tm_hits": self.tm_hits,
            "embedded_translations": sum(
                item.embedded_translations for item in self.files
            ),
            "pending_units": len(self.pending),
            "filtered_units": len(self.filtered),
            "by_match_scope": dict(self.by_match_scope),
            "files": [item.as_dict() for item in self.files],
        }


@dataclass(frozen=True)
class _Resolution:
    translation: str
    scope: str


class TranslationPlanner:
    """Read-only translation selection shared by preflight and execution."""

    def __init__(
        self,
        tm: SQLiteTranslationMemory,
        *,
        validation_rule: ValidationRule,
        global_exact_match: str,
        filter_rules: Optional[FilterRuleSet] = None,
    ) -> None:
        self.tm = tm
        self.validation_rule = validation_rule
        self.global_exact_match = global_exact_match
        self.filter_rules = filter_rules or FilterRuleSet()
        # 判据只有一份：规划期与构建期共用同一个 pipeline 的 `inspect_unit`。
        # 术语表刻意不传 —— 术语违规由 `LegacyTMSynchronizer` 在入库分类时判，
        # 在这里再判一次会改变命中策略，超出「口径一致」这件事的范围。
        self.pipeline = LocalBuildPipeline(validation_rule=validation_rule)
        self._placeholder_rules: Dict[str, PlaceholderRule] = {}
        self._legacy_convergence: Optional[Dict[str, _Resolution]] = None

    def build(
        self,
        resources: Sequence[ResourceBuild],
        *,
        revision_materials: Sequence[bytes] = (),
    ) -> TranslationPlan:
        translations: Dict[str, str] = {}
        provenance: Dict[str, str] = {}
        filtered: Dict[str, str] = {}
        pending = []
        files = []
        total_scopes: Counter[str] = Counter()

        for resource in resources:
            file_scopes: Counter[str] = Counter()
            embedded = 0
            file_pending = 0
            file_filtered = 0
            for unit in resource.units:
                # FilterRule 先判：被跳过的词条连 TM 查询都不做。它既不是
                # 命中也不是待译，渲染时原样保留源文件里的内容。
                rule = self.filter_rules.match(unit)
                if rule is not None:
                    filtered[unit.stable_identity] = rule.id
                    file_filtered += 1
                    continue
                resolution = self._resolve(unit)
                if resolution is not None:
                    translations[unit.stable_identity] = resolution.translation
                    provenance[unit.stable_identity] = resolution.scope
                    file_scopes[resolution.scope] += 1
                    total_scopes[resolution.scope] += 1
                elif unit.translation and unit.translation.strip():
                    translations[unit.stable_identity] = unit.translation
                    provenance[unit.stable_identity] = "embedded"
                    embedded += 1
                else:
                    pending.append(unit)
                    file_pending += 1
            relative_path = (
                resource.units[0].relative_path
                if resource.units
                else resource.adapter.scan(resource.source).relative_path
            )
            files.append(
                TranslationPlanFile(
                    relative_path=relative_path,
                    extracted_units=len(resource.units),
                    tm_hits=sum(file_scopes.values()),
                    embedded_translations=embedded,
                    pending_units=file_pending,
                    by_match_scope=dict(file_scopes),
                    filtered_units=file_filtered,
                )
            )

        fingerprint = self._fingerprint(
            resources,
            translations,
            pending,
            revision_materials=revision_materials,
        )
        return TranslationPlan(
            fingerprint=fingerprint,
            resources=tuple(resources),
            translations=translations,
            provenance=provenance,
            pending=tuple(pending),
            files=tuple(files),
            tm_hits=sum(total_scopes.values()),
            by_match_scope=dict(total_scopes),
            filtered=filtered,
        )

    def _resolve(self, unit: TranslationUnit) -> Optional[_Resolution]:
        hit = self.tm.lookup(
            unit.stable_identity,
            source_fingerprint=unit.source_fingerprint,
            allow_shadow=True,
        )
        if hit is not None:
            return _Resolution(hit.translation, self._scope(hit, "coordinate_exact"))

        logical_keys = [unit.logical_key]
        legacy_key = unit.metadata.get("msgid")
        if legacy_key:
            logical_keys.append(str(legacy_key))
        coordinate = self.tm.lookup_legacy_coordinate(
            project_id=unit.project_id,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
            logical_keys=logical_keys,
            source_fingerprint=unit.source_fingerprint,
        )
        if coordinate:
            # Match v5/v6 precedence: relative path before basename.  A bad row at
            # the winning coordinate is not silently replaced by a lower-priority
            # alias; it falls through to the source-level policies instead.
            candidate = coordinate[0]
            cleaned = self._validated_legacy(candidate, unit)
            if cleaned is not None:
                return _Resolution(cleaned, "legacy_coordinate_exact")

        if self.global_exact_match in {
            "reviewed_only",
            "reviewed_or_legacy_converged",
        }:
            reviewed = self.tm.lookup_reviewed_source(
                unit.project_id, unit.source_fingerprint
            )
            if reviewed is not None:
                return _Resolution(reviewed.translation, "reviewed_source_exact")

        if self.global_exact_match != "reviewed_or_legacy_converged":
            return None
        converged = self._legacy_convergence_index(unit.project_id).get(
            self._normalize_source(unit.source_text)
        )
        if converged is not None and self._translation_is_valid(
            unit, converged.translation
        ):
            return converged
        return None

    def _legacy_convergence_index(self, project_id: str) -> Dict[str, _Resolution]:
        if self._legacy_convergence is not None:
            return self._legacy_convergence
        stats: Dict[str, Counter[str]] = defaultdict(Counter)
        for entry in self.tm.legacy_source_candidates(project_id):
            unit = TranslationUnit(
                project_id=entry.project_id,
                adapter_id=entry.adapter_id,
                relative_path=entry.relative_path,
                logical_file=entry.relative_path,
                logical_key=entry.logical_key,
                source_text=entry.source_text,
                translation=entry.translation,
                source_locale="legacy",
                target_locale="legacy",
            )
            cleaned = self._validated_legacy(entry, unit)
            if cleaned is not None:
                stats[self._normalize_source(entry.source_text)][cleaned] += 1
        result: Dict[str, _Resolution] = {}
        for source, counter in stats.items():
            total = sum(counter.values())
            translation, count = counter.most_common(1)[0]
            if total >= 2 and count / total >= 0.8:
                result[source] = _Resolution(translation, "legacy_source_converged")
        self._legacy_convergence = result
        return result

    def _validated_legacy(
        self, entry: TMEntry, unit: TranslationUnit
    ) -> Optional[str]:
        if entry.classification not in {"legacy_clean", "legacy_suspect"}:
            return None
        return entry.translation if self._translation_is_valid(unit, entry.translation) else None

    def _translation_is_valid(self, unit: TranslationUnit, translation: str) -> bool:
        """一条候选译文能不能直接用。**判据只有一份**，就是 `inspect_unit`。

        这里原本自己拼了一遍顺序：先用**未归一化**的原文比占位符，之后才调
        `validate_text`（NormalizationRule 在它里面跑）。而 `inspect_unit` 是
        反过来的 —— 先归一化、再用**归一化后**的文本比占位符。两处口径相反，
        于是一条会被 NormalizationRule 改动占位符形态的 TM 译文，规划期判
        「合法」并原样进 plan.translations，构建期判 `placeholder_mismatch`
        (error)。因为它是 TM 命中而不是 pending，模型不会被调用，
        `rebuild-from-run` 也只重试 pending —— 这条 error **没有任何带内自愈
        路径**，只能改 TM 或改 rules.yaml。

        改成直接调 `inspect_unit` 而不是把顺序抄对，是因为抄对的两份实现下次
        还会漂移。顺带这里也开始拦 `untranslated` / `invalid_control_character`
        / `placeholder_variant_residue` —— 它们本来就会在构建期阻断整包，
        在规划期拦下只是把同一条判据提前到「还能重译」的时刻。
        """
        inspection = self.pipeline.inspect_unit(unit, translation)
        return not any(record.severity == "error" for record in inspection.records)

    @staticmethod
    def _scope(entry: TMEntry, fallback: str) -> str:
        if entry.origin == "legacy":
            return "legacy_coordinate_exact"
        if entry.origin == "paratranz":
            return "paratranz_coordinate_exact"
        return entry.match_scope or fallback

    @staticmethod
    def _normalize_source(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text or "")
        normalized = normalized.replace("\u00a0", " ").replace("ё", "е").replace("Ё", "Е")
        return re.sub(r"\s+", " ", normalized).strip().casefold()

    @staticmethod
    def _fingerprint(
        resources: Sequence[ResourceBuild],
        translations: Mapping[str, str],
        pending: Sequence[TranslationUnit],
        *,
        revision_materials: Sequence[bytes],
    ) -> str:
        digest = sha256()
        for material in revision_materials:
            digest.update(len(material).to_bytes(8, "big"))
            digest.update(material)
        for resource in sorted(resources, key=lambda item: str(item.source)):
            digest.update(str(Path(resource.source).resolve()).encode("utf-8"))
            digest.update(sha256(Path(resource.source).read_bytes()).digest())
        decisions = {
            "translations": sorted(
                (identity, sha256(value.encode("utf-8")).hexdigest())
                for identity, value in translations.items()
            ),
            "pending": sorted(unit.stable_identity for unit in pending),
        }
        digest.update(json.dumps(decisions, sort_keys=True).encode("utf-8"))
        return digest.hexdigest()
