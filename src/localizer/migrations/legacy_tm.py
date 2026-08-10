from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.domain.translation_unit import TranslationUnit
from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.rules.placeholder import PlaceholderRule
from localizer.rules.validation import ValidationRule
from localizer.infrastructure.atomic_io import AtomicIO


@dataclass(frozen=True)
class LegacyMigrationReport:
    source_hash: str
    total: int
    imported: int
    skipped_unchanged: bool
    classifications: Mapping[str, int]
    reasons: Mapping[str, int]
    # 由 provenance 边车还原出保护属性、因而不进影子表的行数（R17-②）。
    restored_protected: int = 0
    # 还原时被 TM 守卫挡下的坐标。静默吞掉等于让人以为人工标记回来了。
    rejected: Sequence[str] = ()


@dataclass
class _Candidate:
    unit: TranslationUnit
    classification: str
    reason: str


class LegacyTMSynchronizer:
    def __init__(
        self,
        tm: SQLiteTranslationMemory,
        *,
        project_id: str,
        source_locale: str,
        target_locale: str,
        validation_rule: Optional[ValidationRule] = None,
        glossary_terms: Sequence[GlossaryTerm] = (),
    ) -> None:
        self.tm = tm
        self.project_id = project_id
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.validation_rule = validation_rule or ValidationRule()
        self.placeholder_rule = PlaceholderRule()
        # 术语表必须参与入库分类。不查的话，违反已定稿术语的历史译文会被判
        # legacy_clean、被坐标回填直接命中，**模型永远没有重译机会** ——
        # QA 只能在构建期发现，而那时它已经是唯一候选，只能整包阻断。
        self.glossary_terms = tuple(glossary_terms)

    def sync(
        self,
        source_path: Path,
        *,
        force: bool = False,
        report_path: Optional[Path] = None,
        activate_write_guard: bool = False,
    ) -> LegacyMigrationReport:
        path = Path(source_path).resolve(strict=True)
        original_bytes = path.read_bytes()
        source_hash = sha256(original_bytes).hexdigest()
        if not force and self.tm.last_sync_hash(path) == source_hash:
            report = LegacyMigrationReport(source_hash, 0, 0, True, {}, {})
            if activate_write_guard:
                self._write_guard(path, source_hash)
            self._write_report(report_path, report)
            return report
        raw = self._parse(original_bytes, path)
        candidates = self._classify(raw)
        source_groups: Dict[str, set] = defaultdict(set)
        for candidate in candidates:
            if candidate.classification == "legacy_clean":
                source_groups[candidate.unit.source_text].add(candidate.unit.translation)
        for candidate in candidates:
            if (
                candidate.classification == "legacy_clean"
                and len(source_groups[candidate.unit.source_text]) > 1
            ):
                candidate.classification = "legacy_suspect"
                candidate.reason = "source_translation_conflict"

        # 回滚导出留下的 provenance 边车：把旧格式装不下的 origin /
        # human_authored / is_formal 等保护属性还原回来。没有边车时行为
        # 与从前逐字一致。
        provenance = self._load_provenance(path, source_hash)
        entries = [self._to_tm_entry(candidate, provenance) for candidate in candidates]
        # 影子表只装 origin='legacy' 的行 —— 那条约束是对的，不能为了塞回
        # 人工内容而松掉。被边车还原出保护属性的行走正常 upsert，因此仍然
        # 受 `_UPSERT_SQL` 的两条 WHERE 保护。
        shadow = [entry for entry in entries if entry.origin == "legacy"]
        restored = [entry for entry in entries if entry.origin != "legacy"]
        self.tm.replace_legacy_shadow(self.project_id, shadow)
        rejected: Tuple[str, ...] = ()
        if restored:
            rejected = tuple(self.tm.upsert_many(restored))
        self.tm.record_sync(path, source_hash, len(entries))
        classifications = Counter(candidate.classification for candidate in candidates)
        reasons = Counter(candidate.reason for candidate in candidates)
        if path.read_bytes() != original_bytes:
            raise RuntimeError("legacy migration modified its read-only input")
        if activate_write_guard:
            self._write_guard(path, source_hash)
        report = LegacyMigrationReport(
            source_hash,
            len(candidates),
            len(entries),
            False,
            dict(classifications),
            dict(reasons),
            len(restored),
            rejected,
        )
        self._write_report(report_path, report)
        return report

    @staticmethod
    def _write_report(
        report_path: Optional[Path], report: LegacyMigrationReport
    ) -> None:
        if report_path is not None:
            AtomicIO.write_json(Path(report_path), report.__dict__)

    def _write_guard(self, path: Path, source_hash: str) -> None:
        AtomicIO.write_json(
            Path(str(path) + ".shadow-sync.lock"),
            {
                "schema_version": 1,
                "legacy_tm": str(path),
                "shadow_database": str(self.tm.path),
                "source_hash": source_hash,
                "reason": "M3 shadow synchronization is active; direct --save-tm is disabled",
            },
        )

    @staticmethod
    def _parse(content: bytes, path: Path) -> Mapping[str, object]:
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid legacy TM {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"legacy TM {path} root must be a mapping")
        return raw

    def _classify(self, raw: Mapping[str, object]) -> List[_Candidate]:
        candidates: List[_Candidate] = []
        for logical_file, values in raw.items():
            if not isinstance(values, Mapping):
                continue
            for logical_key, value in values.items():
                if not isinstance(value, Mapping):
                    continue
                source = value.get("ru")
                translation = value.get("zh")
                if not isinstance(source, str) or not isinstance(translation, str):
                    continue
                unit = TranslationUnit(
                    project_id=self.project_id,
                    adapter_id="gettext",
                    relative_path=str(logical_file).replace("\\", "/"),
                    logical_file=str(logical_file).replace("\\", "/"),
                    logical_key=str(logical_key),
                    source_text=source,
                    translation=translation,
                    source_locale=self.source_locale,
                    target_locale=self.target_locale,
                    metadata={"legacy_coordinate": True},
                )
                classification, reason = self._classify_unit(unit)
                candidates.append(_Candidate(unit, classification, reason))
        return candidates

    def _classify_unit(self, unit: TranslationUnit) -> Tuple[str, str]:
        translation = unit.translation or ""
        if not translation.strip():
            return "legacy_quarantined", "empty_translation"
        if translation.strip() == unit.source_text.strip():
            return "legacy_quarantined", "untranslated"
        placeholder_source = Counter(self.placeholder_rule.extract(unit.source_text))
        placeholder_target = Counter(self.placeholder_rule.extract(translation))
        if placeholder_source != placeholder_target:
            return "legacy_quarantined", "placeholder_mismatch"
        validation = self.validation_rule.validate_text(
            translation,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
        )
        if validation.failed:
            return "legacy_quarantined", validation.issues[0].code
        for term in self.glossary_terms:
            # 判据与 LocalBuildPipeline 共用 GlossaryTerm.is_violated_by。
            if term.is_violated_by(
                unit.source_text, translation, relative_path=unit.relative_path
            ):
                # 必须是 quarantined 而不是 suspect：lookup_legacy_coordinate 的
                # WHERE 里 `classification IN ('legacy_clean','legacy_suspect')`,
                # suspect 照样会被命中回填，只有 quarantined 被
                # `review_state != 'quarantined'` 挡住。挡住才等于「交给模型重译」。
                return "legacy_quarantined", "glossary_violation"
        return "legacy_clean", "clean"

    def _load_provenance(self, path: Path, source_hash: str) -> Mapping[tuple, Mapping]:
        """读取与旧 JSON 同名的 `.provenance.json`。

        **哈希必须对得上**：边车绑在导出那一刻的内容上。旧 JSON 被手改过之后
        再套用边车，等于把「人工已定稿」的标记贴到一份谁也没审过的译文上，
        比丢掉标记更坏。对不上就当没有边车。
        """
        sidecar = path.with_name(path.name + LegacyTMExporter.PROVENANCE_SUFFIX)
        if not sidecar.is_file():
            return {}
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid provenance sidecar {sidecar}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"provenance sidecar {sidecar} root must be a mapping")
        if payload.get("schema_version") != LegacyTMExporter.PROVENANCE_SCHEMA_VERSION:
            raise ValueError(
                f"provenance sidecar {sidecar}: unsupported schema_version "
                f"{payload.get('schema_version')!r}"
            )
        if payload.get("export_sha256") != source_hash:
            # 静默忽略是不行的：操作者以为人工标记会回来。
            raise ValueError(
                f"provenance sidecar {sidecar} was written for a different export "
                f"(sidecar {str(payload.get('export_sha256'))[:12]}, "
                f"file {source_hash[:12]}); 旧 JSON 在导出之后被改过，"
                f"套用它会把人工定稿标记贴到未经审阅的译文上。"
            )
        restored = {}
        for relative_path, values in (payload.get("entries") or {}).items():
            if not isinstance(values, Mapping):
                continue
            for logical_key, fields in values.items():
                if isinstance(fields, Mapping):
                    restored[(str(relative_path), str(logical_key))] = fields
        return restored

    @staticmethod
    def _to_tm_entry(
        candidate: _Candidate, provenance: Mapping[tuple, Mapping] = {}
    ) -> TMEntry:
        unit = candidate.unit
        review_state = {
            "legacy_clean": "unreviewed",
            "legacy_suspect": "suspect",
            "legacy_quarantined": "quarantined",
            "legacy_unknown": "quarantined",
        }[candidate.classification]
        restored = provenance.get((unit.relative_path, unit.logical_key))
        if restored:
            return TMEntry(
                stable_identity=unit.stable_identity,
                project_id=unit.project_id,
                adapter_id=unit.adapter_id,
                relative_path=unit.relative_path,
                logical_key=unit.logical_key,
                source_text=unit.source_text,
                source_fingerprint=unit.source_fingerprint,
                translation=unit.translation or "",
                match_scope="coordinate_exact",
                origin=str(restored.get("origin", "legacy")),
                review_state=str(restored.get("review_state", review_state)),
                classification=str(
                    restored.get("classification", candidate.classification)
                ),
                stage=restored.get("stage"),
                quality_state=str(restored.get("quality_state", "passed")),
                is_formal=bool(restored.get("is_formal", False)),
                human_authored=bool(restored.get("human_authored", False)),
            )
        return TMEntry(
            stable_identity=unit.stable_identity,
            project_id=unit.project_id,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
            logical_key=unit.logical_key,
            source_text=unit.source_text,
            source_fingerprint=unit.source_fingerprint,
            translation=unit.translation or "",
            origin="legacy",
            review_state=review_state,
            match_scope="coordinate_exact",
            classification=candidate.classification,
            quality_state="passed" if candidate.classification == "legacy_clean" else "failed",
            is_formal=False,
        )


@dataclass(frozen=True)
class LegacyExportReport:
    """回滚导出的结果。计数必须自洽：`exported + withheld + collisions == total`。"""

    destination: str
    export_hash: str
    total_rows: int
    exported: int
    withheld: Mapping[str, int]
    collisions: Sequence[Mapping[str, str]]
    # 带保护属性（人工定稿、正式、ParaTranz 锁定/隐藏/存疑）的行数。
    # 旧格式装不下这些维度，全靠 provenance 边车带回来。
    protected_exported: int = 0
    provenance_sidecar: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "destination": self.destination,
            "export_hash": self.export_hash,
            "total_rows": self.total_rows,
            "exported": self.exported,
            "withheld": dict(self.withheld),
            "collisions": [dict(item) for item in self.collisions],
            "protected_exported": self.protected_exported,
            "provenance_sidecar": self.provenance_sidecar,
        }


class LegacyTMExporter:
    """把 SQLite TM 导回旧 `history_tm.json` 形状（R17-②）。

    设计 §12.10 第 10 条要求切换权威源之后「保留导出旧 JSON 的回滚能力」，
    但迁移一直是单向的。R13 把权威源切换做成了带前置条件的**单向棘轮**之后，
    这条回滚路径就从「补齐 M4 验收项」变成了「那个棘轮唯一的逃生口」。

    三条刻意的取舍：

    1. **默认只导出可用译文。** 旧格式没有 classification 维度，quarantined 行
       导回去就是无标记的正常译文，等于把已知坏账重新喂给旧流程。要真正的
       逐行往返用 `include_quarantined=True`，并且报告里一定列出差异。
    2. **绝不原地覆盖。** 旧文件本身就是回滚目标，用导出结果盖掉它正是要防的
       那个事故。目标已存在时拒绝，除非显式 `overwrite=True`。
    3. **坐标碰撞报告而不是静默丢弃。** 旧格式的键是
       `(relative_path, logical_key)`，没有 adapter 维度；跨 adapter 的同坐标
       在 SQLite 里是两行、在旧格式里只能是一个。静默取其一会让回滚后的库
       悄悄少一批译文。
    """

    # 旧格式里这两个键是写死的字面量（`multi_i18n_processor_v6.py` 直接读
    # `["ru"]` / `["zh"]`），不随项目语言变化。导出必须照抄，否则旧入口读不了。
    SOURCE_KEY = "ru"
    TARGET_KEY = "zh"

    # 不进回滚导出的分类。它们在旧格式里无法表达，导回去就变成无标记的正常译文。
    WITHHELD_CLASSIFICATIONS = ("legacy_quarantined", "legacy_unknown")

    # provenance 边车的后缀。旧入口不认识它、也不需要认识；它只服务于
    # 「导出的文件再被 LegacyTMSynchronizer 正向迁移回来」这一步。
    PROVENANCE_SUFFIX = ".provenance.json"
    PROVENANCE_SCHEMA_VERSION = 1

    # 需要靠边车带回来的字段。少一个 human_authored，`_UPSERT_SQL` 里
    # `NOT (excluded.origin = 'machine' AND tm_entries.human_authored = 1)`
    # 这条全仓唯一的物理执行点就在往返之后失效 —— 实测：人工定稿导出再导入，
    # origin 变 legacy、human_authored 变 0，随后一次普通机器写入就把它覆盖了。
    PROVENANCE_FIELDS = (
        "origin",
        "review_state",
        "classification",
        "stage",
        "quality_state",
        "is_formal",
        "human_authored",
    )

    def __init__(self, tm: SQLiteTranslationMemory, *, project_id: str) -> None:
        self.tm = tm
        self.project_id = project_id

    def export(
        self,
        destination: Path,
        *,
        include_quarantined: bool = False,
        overwrite: bool = False,
        report_path: Optional[Path] = None,
    ) -> LegacyExportReport:
        target = Path(destination)
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"refusing to overwrite {target}: 回滚导出的目标就是旧 TM 本身，"
                f"盖掉它正是这条路径要防的事故。换一个路径，或显式传 overwrite。"
            )
        rows = self.tm.entries_for_project(self.project_id)
        withheld: Counter = Counter()
        collisions: List[Dict[str, str]] = []

        # 先按坐标分组决出胜者，再统一落盘。分两步是因为「谁赢」不能是
        # 「谁先被遍历到」—— 见 `_authority_rank`。
        grouped: Dict[Tuple[str, str], List[TMEntry]] = {}
        for entry in rows:
            if not (entry.translation or "").strip():
                withheld["empty_translation"] += 1
                continue
            if (
                not include_quarantined
                and entry.classification in self.WITHHELD_CLASSIFICATIONS
            ):
                withheld[entry.classification] += 1
                continue
            grouped.setdefault(
                (entry.relative_path, entry.logical_key), []
            ).append(entry)

        payload: Dict[str, Dict[str, Dict[str, str]]] = {}
        provenance: Dict[str, Dict[str, Dict[str, object]]] = {}
        seen: Dict[Tuple[str, str], TMEntry] = {}
        for coordinate, candidates in grouped.items():
            winner = max(candidates, key=self._authority_rank)
            for loser in candidates:
                if loser is winner:
                    continue
                collisions.append(
                    {
                        "relative_path": loser.relative_path,
                        "logical_key": loser.logical_key,
                        "kept_adapter": winner.adapter_id,
                        "kept_identity": winner.stable_identity,
                        "kept_reason": self._authority_reason(winner),
                        "dropped_adapter": loser.adapter_id,
                        "dropped_identity": loser.stable_identity,
                        "dropped_reason": self._authority_reason(loser),
                    }
                )
            seen[coordinate] = winner
            payload.setdefault(winner.relative_path, {})[winner.logical_key] = {
                self.SOURCE_KEY: winner.source_text,
                self.TARGET_KEY: winner.translation,
            }
            if self._is_protected(winner):
                provenance.setdefault(winner.relative_path, {})[
                    winner.logical_key
                ] = {field: getattr(winner, field) for field in self.PROVENANCE_FIELDS}

        # 排序在这里再做一次：`entries_for_project` 保证了行序，但 dict 的
        # 插入序只在同一次调用内可靠。显式排序让导出对库的物理布局免疫。
        ordered = {
            path: dict(sorted(values.items()))
            for path, values in sorted(payload.items())
        }
        serialized = json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"
        export_hash = sha256(serialized.encode("utf-8")).hexdigest()
        AtomicIO.write_text(target, serialized)

        # provenance 边车。只有存在受保护行时才写 —— 一个空边车会让人以为
        # 「这次导出没有人工内容」，而实际是「这次导出没有写边车」。
        sidecar_path = None
        protected = sum(len(values) for values in provenance.values())
        if protected:
            sidecar_path = target.with_name(target.name + self.PROVENANCE_SUFFIX)
            AtomicIO.write_json(
                sidecar_path,
                {
                    "schema_version": self.PROVENANCE_SCHEMA_VERSION,
                    "project_id": self.project_id,
                    # 绑在导出内容的哈希上：边车与旧 JSON 对不上时导入侧必须
                    # 拒绝应用它，否则会把人工标记贴到一份已经被手改过的译文上。
                    "export_sha256": export_hash,
                    "entries": {
                        path: dict(sorted(values.items()))
                        for path, values in sorted(provenance.items())
                    },
                },
            )
        report = LegacyExportReport(
            destination=str(target.resolve()),
            export_hash=export_hash,
            total_rows=len(rows),
            exported=len(seen),
            withheld=dict(withheld),
            collisions=tuple(collisions),
            protected_exported=protected,
            provenance_sidecar=str(sidecar_path) if sidecar_path else None,
        )
        if report_path is not None:
            AtomicIO.write_json(Path(report_path), report.as_dict())
        return report

    # 同坐标碰撞时的 review_state 权重。数值只用于比较，不写进任何产物。
    _REVIEW_WEIGHT = {
        "locked": 5,
        "reviewed": 4,
        "unreviewed": 3,
        "suspect": 2,
        "hidden": 1,
        "quarantined": 0,
    }

    @classmethod
    def _authority_rank(cls, entry: TMEntry) -> tuple:
        """同坐标只能留一条时，留权威度最高的那条。

        旧格式的键是 `(relative_path, logical_key)`，没有 adapter 维度；
        SQLite 里的两行到了旧 JSON 里只能是一个。原来的胜出规则完全由
        `entries_for_project` 的 `ORDER BY … adapter_id` 决定 —— 也就是
        **adapter_id 的字典序**，不看 origin / is_formal / human_authored 中的
        任何一个。方向还恰好是反的：实测 gettext 的一条 `origin=legacy,
        is_formal=0` 机器遗留译文，会挤掉同坐标 paratranz 的 `stage=9 locked`
        人工锁定译文，而回滚文件里留下的是被淘汰的那条。

        `adapter_id` 保留在最后一位，只作确定性 tiebreak —— 导出必须逐字节可复现。
        """
        return (
            1 if entry.is_formal else 0,
            1 if entry.human_authored else 0,
            cls._REVIEW_WEIGHT.get(entry.review_state, 0),
            entry.stage if entry.stage is not None else -1,
            # 字典序**倒过来**：max() 取最大，而 tiebreak 要的是字典序最小的
            # adapter_id 胜出（与原行为一致，避免无谓的产物变化）。
            tuple(-ord(ch) for ch in entry.adapter_id),
        )

    @classmethod
    def _authority_reason(cls, entry: TMEntry) -> str:
        """碰撞报告里写清楚「凭什么」，否则读的人只能猜。"""
        marks = []
        if entry.is_formal:
            marks.append("formal")
        if entry.human_authored:
            marks.append("human")
        marks.append(f"review={entry.review_state}")
        if entry.stage is not None:
            marks.append(f"stage={entry.stage}")
        marks.append(f"origin={entry.origin}")
        return " ".join(marks)

    @staticmethod
    def _is_protected(entry: TMEntry) -> bool:
        """这一行携带旧格式装不下的保护属性吗？

        判据按 origin/human_authored/review_state 组合来定，**不看
        classification** —— 面板人工定稿的 classification 是 `native`，
        ParaTranz 锁定行是 `paratranz`，两者都不在 `WITHHELD_CLASSIFICATIONS`
        里，此前一路无标记地导了出去。
        """
        return bool(
            entry.human_authored
            or entry.is_formal
            or entry.origin in ("human", "paratranz")
            or entry.review_state in ("reviewed", "locked", "hidden", "suspect")
        )
