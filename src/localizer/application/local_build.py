from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.application.artifact import ArtifactBuilder, ReleaseBundle
from localizer.application.quality_gate import (
    QARecord,
    QAReportWriter,
    QualityGate,
    QualityGateResult,
)
from localizer.application.review_index import ReviewIndexWriter
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.infrastructure.workspace import validate_run_id
from localizer.ports.resource import ResourceAdapter, resolve_destination
from localizer.rules.validation import ValidationRule
from localizer.rules.placeholder import PlaceholderRule


class BuildMode(str, Enum):
    PREVIEW = "preview"
    RELEASE = "release"


@dataclass(frozen=True)
class ResourceBuild:
    adapter: ResourceAdapter
    source: Path
    units: Tuple[TranslationUnit, ...]


@dataclass(frozen=True)
class UnitInspection:
    """单个词条的判据结果。"""

    # validate_text 规范化之后的译文（rules.yaml 的 mappings 可能改写它）。
    # 这是实际写进产物的文本，也是同源多译分组要用的文本。
    text: str
    records: Tuple[QARecord, ...]
    # 空译文时被短路掉的下游判据（R03）。不进 qa-report.json，只用于解释
    # 「为什么这条只报了一个 error」。
    suppressed_codes: Tuple[str, ...] = ()


# 依赖「译文非空」才有意义的判据。译文为空时它们必然命中，且命中原因
# 全部是同一个根因 —— 译文压根没产出来。R03：真机 98 个空译派生出
# 83 条 glossary_violation + 31 条 placeholder_mismatch，98 个失败词条
# 被报成 212 个 error，审查台把同一件事排了三遍队。
#
# 注意这里**只做减法**：不改任何既有记录的 details，所以已登记的 853 条
# 存量债 debt_key 一个都不失配（棘轮只查 unaccepted，集合变小天然安全）。
_EMPTY_TRANSLATION_DEPENDENT_CODES = (
    "placeholder_mismatch",
    "untranslated",
    "invalid_control_character",
    "placeholder_variant_residue",
    "source_language_residue",
    "glossary_violation",
)


def resolve_run_output(
    output_root: Path, *, mode: BuildMode, run_id: str
) -> Tuple[Path, Path]:
    """校验并解析一次运行的产物根目录，返回 `(输出根, 本次运行根)`。

    抽成模块级函数是为了让 `ProjectRunner.run()` 能在**任何 Provider 调用之前**
    跑同一份校验（R16-②）。原来它只在 `build()` 里，而 `build()` 发生在整轮
    翻译之后 —— 往一个已发布的 `run_id` 再跑一次 release，会先把整轮的钱烧完，
    然后在写产物的那一刻才失败。两处必须是同一份实现，否则前置检查放行、
    后置检查拦下就是最糟的组合。
    """
    validate_run_id(run_id)
    root = Path(output_root).resolve()
    mode_root = root / mode.value / run_id
    try:
        mode_root.resolve().relative_to(root)
    except ValueError as exc:  # pragma: no cover —— validate_run_id 之后应不可达
        raise ValueError(f"run output escapes {root}: {mode_root}") from exc
    # 同 run_id 重跑会静默改写已发布的 Manifest 与 zip：实测两次 release 的
    # manifest 路径相同、sha 从 6400bb15… 变成 fc993683…，无任何提示。
    # RunWorkspace.create() 的 mkdir(exist_ok=False) 本来就提供这个保护，
    # 但构建链路从不实例化它。preview 允许反复覆盖，正式产物不允许。
    if mode is BuildMode.RELEASE and mode_root.exists():
        raise FileExistsError(
            f"release output already exists for run_id {run_id!r}: {mode_root}. "
            f"Use a new run_id; refusing to overwrite a published artifact."
        )
    return root, mode_root


@dataclass(frozen=True)
class LocalBuildResult:
    mode: BuildMode
    output_root: Path
    qa_json: Path
    qa_csv: Path
    rendered: Tuple[Path, ...]
    bundle: Optional[ReleaseBundle]
    quality_gate: QualityGateResult


class LocalBuildPipeline:
    def __init__(
        self,
        *,
        validation_rule: Optional[ValidationRule] = None,
        quality_gate: Optional[QualityGate] = None,
        artifact_builder: Optional[ArtifactBuilder] = None,
        glossary_terms: Sequence[GlossaryTerm] = (),
    ) -> None:
        self.validation_rule = validation_rule or ValidationRule()
        # 同上：按 adapter_id 取预设，源文与译文用同一套。
        self._placeholder_rules: Dict[str, PlaceholderRule] = {}
        self.quality_gate = quality_gate or QualityGate()
        self.artifact_builder = artifact_builder or ArtifactBuilder()
        self.glossary_terms = tuple(glossary_terms)

    def _placeholder_rule_for(self, adapter_id: str) -> PlaceholderRule:
        rule = self._placeholder_rules.get(adapter_id)
        if rule is None:
            rule = PlaceholderRule.for_adapter(adapter_id)
            self._placeholder_rules[adapter_id] = rule
        return rule

    def inspect_unit(
        self, unit: TranslationUnit, translation: str, provenance: str = "unknown"
    ) -> "UnitInspection":
        """对单个词条跑全部**单条目内**判据，返回 QA 记录与规范化后的译文。

        这是判据的唯一来源：`build()` 与审查视图的即时重校验都调它。抽出来是为了
        让面板能在毫秒级复判一条编辑，而**不需要**第二份判据实现 —— 两份判据一旦
        漂移，面板就会给出「我这里是绿的」而构建期照样阻断，那是最坏的一种假绿灯。

        跨条目的「同源多译」不在这里（见 `group_by_source`），它天然需要全集。

        译文为空时**只报 `empty_translation`**（R03）：下游判据全部依赖非空译文，
        在空串上跑出来的都是同一个根因的派生噪声。根因本身（Provider 报错、
        内容 QA 原因）不进 `qa-report.json`，走审查索引边车 —— 动 `details`
        会让 853 条已登记存量债的 `debt_key` 全部失配。
        """
        summary = self.validation_rule.validate_text(
            translation,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
        )
        records: List[QARecord] = []
        for issue in summary.issues:
            records.append(
                QARecord(
                    issue.code,
                    issue.severity,
                    issue.message,
                    unit.stable_identity,
                    unit.relative_path,
                    issue.details,
                    provenance,
                )
            )
        # 译文为空时下游判据**照常计算、但不上报**（R03）。
        #
        # 只报主错误是为了消除派生噪声；而「到底压掉了哪几条」必须精确，
        # 因为审查页的即时复核拿它当"这条编辑之前就有的问题"的基线：
        # 报少了，把组内既有违规误判成本次新引入，一键统一被硬阻断；
        # 报多了（比如直接用那张静态清单——里面 4 条在空串上根本不会命中），
        # 反而会把真正新引入的问题掩盖成"本来就有"。两个方向都会出事，
        # 所以这里老老实实算一遍，只是不把记录放进 qa-report.json。
        withheld = not summary.text.strip()
        suppressed: List[str] = []

        def _emit(record: QARecord) -> None:
            """要么进报告，要么只记下 code —— 两者互斥。"""
            if withheld:
                suppressed.append(record.code)
            else:
                records.append(record)

        placeholder_rule = self._placeholder_rule_for(unit.adapter_id)
        source_placeholders = Counter(placeholder_rule.extract(unit.source_text))
        target_placeholders = Counter(placeholder_rule.extract(summary.text))
        if source_placeholders != target_placeholders:
            _emit(
                QARecord(
                    "placeholder_mismatch",
                    "error",
                    "placeholder multiset differs from source",
                    unit.stable_identity,
                    unit.relative_path,
                    {
                        "source": list(source_placeholders.elements()),
                        "target": list(target_placeholders.elements()),
                    },
                    provenance,
                )
            )
        # 与翻译阶段用同一套判据。纯占位符条目本来就不该被翻译，
        # 模型原样返回是正确行为；一律判 untranslated 会把它变成
        # 零容忍的 new_error，且无任何出口。
        if (
            placeholder_rule.is_translatable(unit.source_text)
            and summary.text.strip() == unit.source_text.strip()
        ):
            _emit(
                QARecord(
                    "untranslated",
                    "error",
                    "translation is identical to source text",
                    unit.stable_identity,
                    unit.relative_path,
                    {},
                    provenance,
                )
            )
        if "\x00" in summary.text:
            _emit(
                QARecord(
                    "invalid_control_character",
                    "error",
                    "translation contains a NUL character",
                    unit.stable_identity,
                    unit.relative_path,
                    {},
                    provenance,
                )
            )
        # 这里也要查一遍残留 token 变体：TM 命中与 ParaTranz 回流的译文
        # 不经过 BatchOrchestrator，构建阶段是它们唯一的关口。
        residue = placeholder_rule.find_token_residue(summary.text)
        if residue:
            _emit(
                QARecord(
                    "placeholder_variant_residue",
                    "error",
                    "placeholder token survived restore in a variant form",
                    unit.stable_identity,
                    unit.relative_path,
                    {"fragments": list(residue)},
                    provenance,
                )
            )
        for record in self._glossary_issues(unit, summary.text, provenance):
            _emit(record)
        return UnitInspection(summary.text, tuple(records), tuple(dict.fromkeys(suppressed)))

    def build(
        self,
        resources: Sequence[ResourceBuild],
        translations: Mapping[str, str],
        *,
        mode: BuildMode,
        project_id: str,
        run_id: str,
        output_root: Path,
        failed_unit_identities: Sequence[str] = (),
        tm: Optional[SQLiteTranslationMemory] = None,
        formal_tm_identities: Optional[Sequence[str]] = None,
        manifest_metadata: Optional[Mapping[str, object]] = None,
        artifact_options: Optional[Mapping[str, object]] = None,
        unit_provenance: Optional[Mapping[str, str]] = None,
        # R03：本次翻译失败的坐标 → 根因（Provider 报错 / 内容 QA 判据）。
        # 只写审查索引边车，不进 qa-report.json。
        unit_root_causes: Optional[Mapping[str, Mapping[str, object]]] = None,
        # R12：被 FilterRule 移出翻译范围的坐标。它们照常渲染（原样保留源文件
        # 里的内容），但**完全不参与 QA** —— 否则「跳过」只是把噪声从翻译搬到
        # 报告里，跳过就失去了意义。
        filtered_identities: Sequence[str] = (),
    ) -> LocalBuildResult:
        # run_id 直接进路径，必须先校验；否则含 ".." 的值会让制品与 Manifest
        # 落到 paths.output 之外（实测能落到 %LOCALAPPDATA%），工作区目录根本不被创建。
        root, mode_root = resolve_run_output(output_root, mode=mode, run_id=run_id)
        rendered_root = mode_root / "resources"
        reports_root = mode_root / "reports"
        records: List[QARecord] = []
        suppressed: Dict[str, List[str]] = {}
        filtered = frozenset(filtered_identities)
        prepared: List[Tuple[ResourceBuild, Tuple[TranslationUnit, ...]]] = []
        for resource in resources:
            translated = []
            for unit in resource.units:
                if unit.stable_identity in filtered:
                    # **原样**放回，不套 `unit.translation or ""`。
                    # Adapter 用 translation is None（paradox）或源文所在字段
                    # （gettext keyed_source 的 msgstr）表达"这条没有译文"；
                    # 把它压成空串再交给 render，就从"跳过"变成了"清空"。
                    translated.append(unit)
                    continue
                value = translations.get(unit.stable_identity, unit.translation or "")
                # 这条译文是谁产出的。machine = 本次运行自己生成，零容忍；
                # 其余都是搬运过来的存量内容，按存量债基线棘轮处理。
                prov = (unit_provenance or {}).get(unit.stable_identity, "unknown")
                inspection = self.inspect_unit(unit, value, prov)
                records.extend(inspection.records)
                if inspection.suppressed_codes:
                    suppressed[unit.stable_identity] = list(inspection.suppressed_codes)
                translated.append(replace(unit, translation=inspection.text))
            prepared.append((resource, tuple(translated)))
        records.extend(self._consistency_issues(prepared, exclude=filtered))
        gate = self.quality_gate.evaluate(
            records, failed_unit_identities=failed_unit_identities
        )
        qa_json, qa_csv = QAReportWriter.write(reports_root, records, gate)
        # 审查索引与报告**平行**落盘。QARecord、details 与 qa-report.json 的
        # issue 形状一个字节都不改 —— 动 details 会让已登记的存量债基线
        # 100% 失配（debt_key 含 details 摘要）。
        ReviewIndexWriter.write(
            reports_root,
            prepared,
            records,
            unit_provenance=unit_provenance,
            raw_translations=translations,
            glossary_terms=self.glossary_terms,
            root_causes=unit_root_causes,
            suppressed_codes=suppressed,
            filtered_identities=filtered,
            context={
                "project_id": project_id,
                "run_id": run_id,
                "mode": mode.value,
                # rules.yaml 的 residue mappings 与 NormalizationRule 都会改写
                # 译文。非空时任何「基于产物重算」的复核都会**二次应用**它们，
                # 结论与真实 build 不一致 —— 下游据此直接拒绝运行，而不是打个
                # 警告了事。
                "mappings_empty": not getattr(
                    self.validation_rule,
                    "rewrites_text",
                    bool(getattr(self.validation_rule, "residue_mappings", None)),
                ),
            },
        )
        if mode is BuildMode.RELEASE:
            self.quality_gate.require_release(
                records, failed_unit_identities=failed_unit_identities
            )

        rendered = []
        # plan_destination 开放后，两个源文件映射到同一目标是真实可能，
        # 先做一次唯一性断言（AtomicIO 早有这个方法，此前从未被调用）。
        AtomicIO.assert_unique_targets(
            resolve_destination(resource.adapter, resource.source, rendered_root)
            for resource, _units in prepared
        )
        for resource, units in prepared:
            destination = resolve_destination(
                resource.adapter, resource.source, rendered_root
            )
            # 被 FilterRule 跳过的词条不进 render 的单元集：两个真实 Adapter 的
            # `by_key.get(...) is None` 分支本来就是"保留源文件里的值"。
            # 传进去（哪怕 translation 原样）在 keyed_source 布局下仍会命中
            # `entry.msgstr = unit.translation` 把源文写成空串 —— 实测
            # `tech_debug` 的俄文原文被抹掉，而 QA 因为跳过而零 error、闸门放行。
            renderable = tuple(
                item for item in units if item.stable_identity not in filtered
            )
            result = resource.adapter.render(renderable, resource.source, destination)
            if not result.validation.valid:
                # 报错必须指出是哪个源文件、写到了哪里 —— 原来只有一句
                # "line 2: unparsable entry"，在几百个文件的项目里毫无定位价值。
                raise ValueError(
                    f"render validation failed for {resource.source} "
                    f"-> {destination}: " + "; ".join(result.validation.errors)
                )
            rendered.append(result.destination)

        if mode is BuildMode.PREVIEW:
            return LocalBuildResult(
                mode, mode_root, qa_json, qa_csv, tuple(rendered), None, gate
            )

        identities = (
            tuple(formal_tm_identities)
            if formal_tm_identities is not None
            else tuple(translations.keys())
        )
        if tm is not None and identities:
            tm.validate_promotable_run(run_id, identities)
        bundle = self.artifact_builder.build_release(
            project_id=project_id,
            run_id=run_id,
            resource_root=rendered_root,
            resource_paths=rendered,
            destination=mode_root,
            manifest_metadata=manifest_metadata,
            **dict(artifact_options or {}),
        )
        if tm is not None:
            if identities:
                tm.promote_run(run_id, identities)
        return LocalBuildResult(
            mode, mode_root, qa_json, qa_csv, tuple(rendered), bundle, gate
        )

    def _glossary_issues(
        self, unit: TranslationUnit, translation: str, provenance: str = "unknown"
    ) -> Sequence[QARecord]:
        issues = []
        for term in self.glossary_terms:
            # 判据由 GlossaryTerm 自己持有 —— 入库分类器用的是同一个方法。
            # 两处漂移会制造「入库判干净、构建判违规」这种最坏组合：坏账被洗成
            # 可命中，模型没有重译机会，只能整包阻断。
            if term.is_violated_by(
                unit.source_text, translation, relative_path=unit.relative_path
            ):
                issues.append(
                    QARecord(
                        "glossary_violation",
                        "error",
                        f"reviewed glossary target is missing: {term.source} => {term.target}",
                        unit.stable_identity,
                        unit.relative_path,
                        {"source_term": term.source, "required_target": term.target},
                        provenance,
                    )
                )
        return tuple(issues)

    @staticmethod
    def group_by_source(
        prepared: Sequence[Tuple[ResourceBuild, Tuple[TranslationUnit, ...]]],
        *,
        include_empty: bool,
        exclude: frozenset = frozenset(),
    ) -> Dict[str, Dict[str, List[TranslationUnit]]]:
        """按源文分组：`{源文: {译文: [词条…]}}`。

        `include_empty` 决定空译文成员算不算数，两个口径**都需要**：

        - QA 记录用 `False`（历史行为，逐字节不变）；
        - 审查视图的「一键统一」用 `True`。空译文成员不在 QA 记录里，
          漏掉它们就会只统一一半 —— 剩下的那些下一次运行会被模型重译出
          第 N 种译法，警告复活而操作者以为已经处理完了。
        """
        groups: Dict[str, Dict[str, List[TranslationUnit]]] = {}
        for _, units in prepared:
            for unit in units:
                # 被 FilterRule 跳过的词条不参与同源多译判定：它压根不在
                # 翻译范围内，拿它当"另一种译法"是无中生有。
                if unit.stable_identity in exclude:
                    continue
                if not include_empty and not unit.translation:
                    continue
                groups.setdefault(unit.source_text, {}).setdefault(
                    unit.translation or "", []
                ).append(unit)
        return groups

    @classmethod
    def _consistency_issues(
        cls,
        prepared: Sequence[Tuple[ResourceBuild, Tuple[TranslationUnit, ...]]],
        *,
        exclude: frozenset = frozenset(),
    ) -> Sequence[QARecord]:
        groups = cls.group_by_source(prepared, include_empty=False, exclude=exclude)
        issues = []
        for source, translations in groups.items():
            if len(translations) <= 1:
                continue
            sample = next(iter(translations.values()))[0]
            issues.append(
                QARecord(
                    "same_source_inconsistency",
                    "warning",
                    "same source text has multiple translations in this run",
                    sample.stable_identity,
                    sample.relative_path,
                    {"source": source, "translations": sorted(translations)},
                )
            )
        return tuple(issues)
