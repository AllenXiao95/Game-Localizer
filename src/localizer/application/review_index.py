"""审查索引（sidecar）：把 QARecord 缺的源文、译文、分组成员补上。

为什么是 sidecar 而不是给 `QARecord` 加字段：`debt_key` 是
`sid::code::sha256(canonical_json(details))[:12]`，动 `details` 的形状会让
**已登记的存量债基线 100% 失配**；而加顶层字段虽然不影响 debt_key，却会把
每条记录都撑大（真机 2936 条），且 `qa-report.json` 是 `qa-accept-debt`、
棘轮与外部脚本的契约面。索引与报告平行落盘，两边互不影响。

它在构建期由 `LocalBuildPipeline.build()` 顺手写出 —— 那时 `prepared` 里
源文、译文、分组全都在手，**零额外扫描成本**。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.application.quality_gate import QARecord
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO

SCHEMA_VERSION = 1
INDEX_FILENAME = "qa-review-index.json"

# 归一化时剥掉的尾部标点：中英文句号、逗号、冒号、分号、感叹号、问号、空白。
_TRAILING = " \t\r\n。．.，,、：:；;！!？?"


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_variant(text: str) -> str:
    """同源多译的「其实是同一个译法」归一化。

    只做保守的三件事：NFKC（全角/半角统一）、去首尾空白、去尾部标点。
    **不做**去除全部标点空白 —— 那会把「A、B」和「AB」判成同一个，
    而它们在 UI 上是两个不同的决策。
    """
    return unicodedata.normalize("NFKC", text).strip().strip(_TRAILING)


@dataclass(frozen=True)
class ReviewIndex:
    payload: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path) -> "ReviewIndex":
        raw = AtomicIO.read_text(Path(path))
        import json

        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise ValueError(f"review index root must be an object: {path}")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"review index {path}: unsupported schema_version {version!r}; "
                f"supported: {SCHEMA_VERSION}"
            )
        return cls(payload)

    @property
    def units(self) -> Mapping[str, Mapping[str, Any]]:
        return self.payload.get("units") or {}

    @property
    def same_source_groups(self) -> Sequence[Mapping[str, Any]]:
        return self.payload.get("same_source_groups") or ()

    @property
    def glossary_clusters(self) -> Sequence[Mapping[str, Any]]:
        return self.payload.get("glossary_clusters") or ()

    @property
    def source_buckets(self) -> Mapping[str, Mapping[str, Any]]:
        return self.payload.get("source_buckets") or {}

    @property
    def mappings_empty(self) -> bool:
        return bool(self.payload.get("mappings_empty", True))

    def group_for(self, group_id: str) -> Optional[Mapping[str, Any]]:
        for group in self.same_source_groups:
            if group.get("group_id") == group_id:
                return group
        return None

    def cluster_for(self, cluster_id: str) -> Optional[Mapping[str, Any]]:
        for cluster in self.glossary_clusters:
            if cluster.get("cluster_id") == cluster_id:
                return cluster
        return None


class ReviewIndexWriter:
    """构建期写出 sidecar。数据全部来自 `build()` 手里已有的东西。"""

    @staticmethod
    def build_payload(
        prepared: Sequence[Tuple[Any, Tuple[TranslationUnit, ...]]],
        records: Sequence[QARecord],
        *,
        unit_provenance: Optional[Mapping[str, str]] = None,
        raw_translations: Optional[Mapping[str, str]] = None,
        glossary_terms: Sequence[GlossaryTerm] = (),
        context: Optional[Mapping[str, Any]] = None,
        group_by_source=None,
        root_causes: Optional[Mapping[str, Mapping[str, Any]]] = None,
        suppressed_codes: Optional[Mapping[str, Sequence[str]]] = None,
        filtered_identities: Sequence[str] = (),
    ) -> Dict[str, Any]:
        provenance = dict(unit_provenance or {})
        raw = dict(raw_translations or {})
        causes = dict(root_causes or {})
        suppressed = dict(suppressed_codes or {})
        # R12：被 FilterRule 跳过的词条不在翻译范围内。它们既不该出现在同源
        # 多译分组里（那是无中生有的"另一种译法"），也不该进 source_buckets ——
        # 桶是面板判断"这次编辑新造了一条分歧"的唯一判据，掺进跳过项会让
        # 面板报出一条构建期永远不会出现的告警。
        filtered = frozenset(filtered_identities)
        all_units: List[TranslationUnit] = [
            unit
            for _resource, units in prepared
            for unit in units
            if unit.stable_identity not in filtered
        ]
        by_identity = {unit.stable_identity: unit for unit in all_units}

        codes: Dict[str, List[str]] = {}
        for record in records:
            if record.stable_identity:
                codes.setdefault(record.stable_identity, []).append(record.code)

        # 分组用 include_empty=True：空译文成员不在 QA 记录里，漏掉它们
        # 「一键统一」就只会统一一半，剩下的下一次运行被模型重译出第 N 种译法。
        groups = (group_by_source or _default_group_by_source)(
            prepared, include_empty=True, exclude=filtered
        )
        divergent = {
            source: variants
            for source, variants in groups.items()
            if len([text for text in variants if text]) > 1
        }

        qa_sample = {
            record.details.get("source"): record.stable_identity
            for record in records
            if record.code == "same_source_inconsistency" and record.details
        }

        same_source_groups = []
        interesting: set = set()
        for source, variants in sorted(divergent.items()):
            members = []
            for text, units in variants.items():
                for unit in units:
                    members.append(
                        {
                            "stable_identity": unit.stable_identity,
                            "relative_path": unit.relative_path,
                            "translation": text,
                        }
                    )
                    interesting.add(unit.stable_identity)
            members.sort(key=lambda item: (item["relative_path"], item["stable_identity"]))
            non_empty = {text: units for text, units in variants.items() if text}
            counts = {text: len(units) for text, units in non_empty.items()}
            total = sum(counts.values())
            majority = None
            if counts:
                best_text, best_count = max(counts.items(), key=lambda kv: kv[1])
                if total and best_count / total >= 0.8 and len(counts) > 1:
                    majority = {
                        "translation": best_text,
                        "count": best_count,
                        "ratio": round(best_count / total, 4),
                    }
            normalized = {normalize_variant(text) for text in non_empty}
            same_source_groups.append(
                {
                    "group_id": _digest(source),
                    "source": source,
                    "members": members,
                    "member_count": len(members),
                    "variant_count": len(non_empty),
                    "has_empty_members": any(not text for text in variants),
                    "majority": majority,
                    # 归一化之后坍缩成一个译法 —— 这类可以安全批量统一。
                    "normalized_collapse": (
                        next(iter(normalized)) if len(normalized) == 1 else None
                    ),
                    "qa_record_identity": qa_sample.get(source),
                }
            )

        clusters = _glossary_clusters(records, glossary_terms)
        for cluster in clusters:
            interesting.update(cluster["violation_identities"])
        interesting.update(codes)
        # 本次翻译失败的坐标即使**没有**任何 QA 记录（例如源文件里原本就带着
        # 一条历史译文，空译判据不命中）也必须出现在索引里，否则根因无处可挂。
        interesting.update(causes)

        units_payload = {}
        for identity in sorted(interesting):
            unit = by_identity.get(identity)
            if unit is None:
                continue
            units_payload[identity] = {
                "relative_path": unit.relative_path,
                "logical_key": unit.logical_key,
                "adapter_id": unit.adapter_id,
                "source_text": unit.source_text,
                "source_fingerprint": unit.source_fingerprint,
                "translation": unit.translation or "",
                # 未经 rules.yaml mappings 改写的入库文本。mappings 为空时二者相同。
                "raw_translation": raw.get(identity, unit.translation or ""),
                "provenance": provenance.get(identity, "unknown"),
                "codes": sorted(set(codes.get(identity, ()))),
                "source_group": _digest(unit.source_text)
                if unit.source_text in divergent
                else None,
            }
            # R03：空译文短路之后，`codes` 里只剩 `empty_translation`。
            # 「为什么是空的」与「短路掉了哪些判据」在这里补上 —— 放边车
            # 而不是 QARecord.details，因为 details 参与 debt_key。
            # 两个字段都可选，schema 保持 v1（老索引缺字段，消费方须容忍）。
            cause = causes.get(identity)
            if cause:
                units_payload[identity]["root_cause"] = dict(cause)
            skipped = suppressed.get(identity)
            if skipped:
                units_payload[identity]["suppressed_codes"] = list(skipped)

        # 桶覆盖**全量**单元（不只是进 units 的那些）：它是「你这次手工编辑
        # 刚刚新造出一条同源分歧」的唯一判据，只存哈希与计数，不存文本。
        buckets: Dict[str, Dict[str, Any]] = {}
        for unit in all_units:
            bucket = buckets.setdefault(_digest(unit.source_text), {"n": 0, "t": {}})
            bucket["n"] += 1
            key = _digest(unit.translation or "")
            bucket["t"][key] = bucket["t"].get(key, 0) + 1

        # 语言对是项目级常量，但重建 TranslationUnit 时必需（它拒绝空 locale）。
        first = all_units[0] if all_units else None
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_locale": first.source_locale if first else "",
            "target_locale": first.target_locale if first else "",
            "units": units_payload,
            "same_source_groups": same_source_groups,
            "glossary_clusters": clusters,
            "source_buckets": buckets,
            "unit_total": len(all_units),
        }
        payload.update(dict(context or {}))
        return payload

    @classmethod
    def write(
        cls,
        reports_root: Path,
        prepared,
        records: Sequence[QARecord],
        **kwargs,
    ) -> Path:
        payload = cls.build_payload(prepared, records, **kwargs)
        destination = Path(reports_root).resolve() / INDEX_FILENAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        return AtomicIO.write_json(destination, payload)


def _default_group_by_source(prepared, *, include_empty: bool, exclude=frozenset()):
    from localizer.application.local_build import LocalBuildPipeline

    return LocalBuildPipeline.group_by_source(
        prepared, include_empty=include_empty, exclude=exclude
    )


def _glossary_clusters(
    records: Sequence[QARecord], glossary_terms: Sequence[GlossaryTerm]
) -> List[Dict[str, Any]]:
    """把术语违规按 (源词, 要求译名) 聚类。

    真机 851 条 `glossary_violation` 只对应 **43 个** (源词, 要求译名) 对 ——
    逐条平铺会让操作者做 851 次重复判断，而实际只有 43 个决策。
    这是整个审查界面里唯一一个 20 倍量级的杠杆。
    """
    by_source = {term.source: term for term in glossary_terms}
    clusters: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in records:
        if record.code != "glossary_violation" or not record.details:
            continue
        source_term = str(record.details.get("source_term", ""))
        required = str(record.details.get("required_target", ""))
        key = (source_term, required)
        cluster = clusters.get(key)
        if cluster is None:
            term = by_source.get(source_term)
            cluster = {
                "cluster_id": _digest(f"{source_term}\x1f{required}"),
                "source_term": source_term,
                "required_target": required,
                "match_mode": term.match_mode if term else "word",
                "variants": list(term.variants) if term else [],
                "scope": term.scope if term else None,
                "exclude_scope": list(term.exclude_scope) if term else [],
                "status": term.status if term else "unknown",
                "provenance": term.provenance if term else "unknown",
                # human + reviewed 的术语受 G01 绝对保护，改它要走 destructive 路径。
                "protected": bool(term.human_reviewed) if term else False,
                "violation_identities": [],
                "files": set(),
            }
            clusters[key] = cluster
        if record.stable_identity:
            cluster["violation_identities"].append(record.stable_identity)
        if record.relative_path:
            cluster["files"].add(record.relative_path)

    result = []
    for cluster in clusters.values():
        files = cluster.pop("files")
        cluster["violation_identities"] = sorted(set(cluster["violation_identities"]))
        cluster["violation_count"] = len(cluster["violation_identities"])
        cluster["file_count"] = len(files)
        cluster["files"] = sorted(files)
        result.append(cluster)
    result.sort(key=lambda item: (-item["violation_count"], item["source_term"]))
    return result
