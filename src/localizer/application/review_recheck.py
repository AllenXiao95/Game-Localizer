"""面板内的即时重校验（T0/T1）。

这是全案最容易造假绿灯的地方，所以结果对象**强制**携带 `authoritative=False`
与 `not_evaluated`：它算的是「这条编辑本身还有没有问题」，**不是**「release 能不能
发」。闸门结论只来自换新 `run_id` 的整轮运行。

判据不自己实现，一律走 `LocalBuildPipeline.inspect_unit` —— 两份判据一旦漂移，
面板就会说「我这里是绿的」而构建期照样阻断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from localizer.application.review_index import ReviewIndex
from localizer.domain.translation_unit import TranslationUnit

# 面板算不了的东西。写进每一份响应体，UI 必须原样显示。
NOT_EVALUATED = (
    "quality_gate",
    "failed_unit_count",
    "legacy_debt_baseline",
)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class UnitVerdict:
    stable_identity: str
    # 送进判据的文本（可能被 rules.yaml 的 mappings 改写过）。
    judged_text: str
    input_text: str
    fixed: Tuple[str, ...]
    remaining: Tuple[str, ...]
    introduced: Tuple[str, ...]
    records: Tuple[Mapping[str, Any], ...]

    @property
    def introduced_errors(self) -> Tuple[str, ...]:
        return tuple(
            record["code"]
            for record in self.records
            if record["severity"] == "error" and record["code"] in self.introduced
        )


@dataclass(frozen=True)
class ConsistencyDelta:
    resolved_groups: Tuple[str, ...] = ()
    introduced_groups: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RecheckResult:
    verdicts: Tuple[UnitVerdict, ...]
    consistency: ConsistencyDelta
    # 永远是 False。面板算的是「这条编辑本身还有没有问题」，不是「能不能发布」。
    authoritative: bool = False
    not_evaluated: Tuple[str, ...] = NOT_EVALUATED
    unknown_identities: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "authoritative": self.authoritative,
            "not_evaluated": list(self.not_evaluated),
            "unknown_identities": list(self.unknown_identities),
            "consistency": {
                "resolved_groups": list(self.consistency.resolved_groups),
                "introduced_groups": list(self.consistency.introduced_groups),
            },
            "verdicts": [
                {
                    "stable_identity": verdict.stable_identity,
                    "judged_text": verdict.judged_text,
                    "input_text": verdict.input_text,
                    # 两者不同说明 rules.yaml 的 mappings 改写了译文，
                    # UI 必须并排显示 —— 否则人看到的和实际入库的不是一个东西。
                    "rewritten_by_rules": verdict.judged_text != verdict.input_text,
                    "fixed": list(verdict.fixed),
                    "remaining": list(verdict.remaining),
                    "introduced": list(verdict.introduced),
                    "records": [dict(record) for record in verdict.records],
                }
                for verdict in self.verdicts
            ],
        }

    @property
    def introduced_errors(self) -> Tuple[Tuple[str, str], ...]:
        """(stable_identity, code)。落库前的硬闸就看它。"""
        return tuple(
            (verdict.stable_identity, code)
            for verdict in self.verdicts
            for code in verdict.introduced_errors
        )


class ReviewRechecker:
    def __init__(self, index: ReviewIndex, pipeline) -> None:
        self.index = index
        self.pipeline = pipeline

    def scope_identities(self, scope: Optional[str]) -> Optional[Tuple[str, ...]]:
        """把 `cluster:<id>` 解析成该术语覆盖的词条。

        改一个术语会改变全部术语违规的判据基准。对全量单元重判实测要 9.5 秒，
        43 个术语决策就是 7 分钟纯卡顿 —— 必须限定到该 cluster 的违规词条
        （851 条量级约 0.14 秒）。
        """
        if not scope:
            return None
        if not scope.startswith("cluster:"):
            raise ValueError(f"unsupported recheck scope: {scope}")
        cluster = self.index.cluster_for(scope[len("cluster:") :])
        if cluster is None:
            raise ValueError(f"unknown glossary cluster: {scope}")
        return tuple(cluster["violation_identities"])

    def check(
        self,
        edits: Mapping[str, str],
        *,
        scope: Optional[str] = None,
    ) -> RecheckResult:
        limit = self.scope_identities(scope)
        verdicts: List[UnitVerdict] = []
        unknown: List[str] = []
        for identity, text in sorted(edits.items()):
            if limit is not None and identity not in limit:
                continue
            unit_payload = self.index.units.get(identity)
            if unit_payload is None:
                unknown.append(identity)
                continue
            verdicts.append(self._check_one(identity, unit_payload, text))
        return RecheckResult(
            tuple(verdicts),
            self.consistency_delta(edits),
            unknown_identities=tuple(sorted(unknown)),
        )

    def _check_one(
        self, identity: str, payload: Mapping[str, Any], text: str
    ) -> UnitVerdict:
        unit = TranslationUnit(
            project_id=self.index.payload.get("project_id", ""),
            adapter_id=payload["adapter_id"],
            relative_path=payload["relative_path"],
            logical_key=payload["logical_key"],
            source_text=payload["source_text"],
            source_locale=self.index.payload.get("source_locale") or "und",
            target_locale=self.index.payload.get("target_locale") or "und",
        )
        # 判据只有一份。这里刻意直接调 pipeline，不复制任何判定逻辑。
        inspection = self.pipeline.inspect_unit(
            unit, text, payload.get("provenance", "unknown")
        )
        # 两端都必须把「空译文时被压掉的判据」算进来（R03）。
        #
        # 空译文只上报 empty_translation，其余判据的结果放在边车的
        # `suppressed_codes` 里。只看 `codes` 会把组内**既有**的术语违规
        # 算成本次新引入 —— 一键统一到组内已有译法时 commit 直接抛异常，
        # 而 unify() 连 accepted_debt 都没有，操作者没有任何出口。
        #
        # `now` 同样要并：编辑之后仍然是空译文时，那些判据依旧成立，
        # 只是照样不上报；不并进来会被误判成"已修复"。
        now = {record.code for record in inspection.records}
        now |= set(inspection.suppressed_codes)
        was = set(payload.get("codes") or ())
        was |= set(payload.get("suppressed_codes") or ())
        return UnitVerdict(
            stable_identity=identity,
            judged_text=inspection.text,
            input_text=text,
            fixed=tuple(sorted(was - now)),
            remaining=tuple(sorted(was & now)),
            introduced=tuple(sorted(now - was)),
            records=tuple(record.to_dict() for record in inspection.records),
        )

    def consistency_delta(self, edits: Mapping[str, str]) -> ConsistencyDelta:
        """同源多译的增量判断，用 `source_buckets` 做到精确。

        桶覆盖**全量**单元，所以这里能回答「你这次编辑刚刚新造出一条同源分歧」——
        那是最容易被忽略的回归：你把 A 统一了，顺手改的 B 却制造了新的分歧。
        """
        buckets = self.index.source_buckets
        if not buckets:
            return ConsistencyDelta()
        # 复制受影响的桶，套用编辑之后重新数。
        touched: Dict[str, Dict[str, int]] = {}
        before: Dict[str, Dict[str, int]] = {}
        for identity, text in edits.items():
            payload = self.index.units.get(identity)
            if payload is None:
                continue
            key = _digest(payload["source_text"])
            bucket = buckets.get(key)
            if bucket is None:
                continue
            counts = touched.get(key)
            if counts is None:
                counts = dict(bucket.get("t") or {})
                touched[key] = counts
                before[key] = dict(counts)
            old = _digest(payload.get("translation") or "")
            new = _digest(text)
            if counts.get(old):
                counts[old] -= 1
                if counts[old] == 0:
                    del counts[old]
            counts[new] = counts.get(new, 0) + 1

        resolved, introduced = [], []
        for key, counts in touched.items():
            def variants(mapping):
                # 空译文不算一个「译法」—— 与 QA 记录的口径一致。
                return {k for k, v in mapping.items() if v and k != _digest("")}

            was_divergent = len(variants(before[key])) > 1
            is_divergent = len(variants(counts)) > 1
            if was_divergent and not is_divergent:
                resolved.append(key)
            elif not was_divergent and is_divergent:
                introduced.append(key)
        return ConsistencyDelta(tuple(sorted(resolved)), tuple(sorted(introduced)))
