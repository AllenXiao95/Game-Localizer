"""审查服务：把 sidecar、决策日志、ledger 与 TM 串成面板能用的接口。

边界（framework-design §16.4）：这是 **QA 缺陷的定点修复**，不是审核平台。
没有审核队列、任务分发、多人审批、权限模型、审核状态机，也不与 ParaTranz
同步 stage。它只处理 QA 报告已经识别出来的四类问题。

三条硬性不变量：
1. 落库前服务端**自己**重跑判据，不信客户端传来的结果；新增 error 无
   `accepted_debt` 一律拒绝。
2. `written < requested` 一律按错误返回，绝不显示「已落表」。
3. 永不输出 `QualityGateResult.passed`。
"""
from __future__ import annotations

import fnmatch
import getpass
import os
import socket
import uuid
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.glossary import GlossaryRepository, GlossaryTerm
from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
    TMGuardError,
)
from localizer.application.local_build import LocalBuildPipeline
from localizer.application.review_index import INDEX_FILENAME, ReviewIndex
from localizer.application.review_ledger import ReviewLedger
from localizer.application.review_log import (
    ReviewDecisionEvent,
    ReviewDecisionLog,
)
from localizer.application.review_recheck import ReviewRechecker
from localizer.config.models import ProjectConfig
from localizer.rules.loader import load_validation_rule
from localizer.rules.placeholder import PlaceholderRule

MAX_COMMIT_ITEMS = 100
MAX_DECISION_ITEMS = 200


# 队列排序用的处置优先级。**已处理的沉到底部** —— 2000 个决策的场景下，
# 每定完一条它还留在原地会让人反复扫过同一批已经做完的东西。
# 未决 → 待议 → 跳过 → 已定稿/已撤销。
_STATE_RANK = {
    "pending": 0,
    "draft": 1,
    "deferred": 2,
    "skipped": 3,
    "reverted": 4,
    "committed": 5,
}


def diff_ops(before: str, after: str) -> List[Dict[str, Any]]:
    """字符级差异，服务端算。

    前端只渲染 `<ins>`/`<del>`。差异算法放服务端有两个原因：CSP 只允许内联脚本
    （不能引外部 diff 库），而手写一个 JS diff 又是一份没人测的实现 —— 而
    `difflib` 是标准库，且这条链路上已经有「判据只有一份」的先例。
    """
    if before == after:
        return [{"op": "equal", "text": after}]
    ops: List[Dict[str, Any]] = []
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append({"op": "equal", "text": after[j1:j2]})
        elif tag == "delete":
            ops.append({"op": "delete", "text": before[i1:i2]})
        elif tag == "insert":
            ops.append({"op": "insert", "text": after[j1:j2]})
        else:
            ops.append({"op": "delete", "text": before[i1:i2]})
            ops.append({"op": "insert", "text": after[j1:j2]})
    return ops


class ReviewConflict(RuntimeError):
    """有运行在跑、或者索引/日志已经变了 —— 现在不能写。"""


class ReviewUnavailable(RuntimeError):
    """这次运行没有审查索引。"""


@dataclass(frozen=True)
class CommitOutcome:
    audit_id: str
    requested: int
    written: int
    guarded: Tuple[Tuple[str, str], ...]
    rejected: Tuple[Tuple[str, str], ...]
    log_revision: str
    committed_target_ids: Tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.written == self.requested and not self.guarded and not self.rejected

    def as_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "requested": self.requested,
            "written": self.written,
            # written < requested 一律按错误渲染，绝不显示「已落表」。
            "complete": self.complete,
            "guarded": [{"stable_identity": s, "reason": r} for s, r in self.guarded],
            "rejected": [{"stable_identity": s, "reason": r} for s, r in self.rejected],
            "log_revision": self.log_revision,
            "committed_target_ids": list(self.committed_target_ids),
        }


class ReviewService:
    def __init__(
        self,
        config: ProjectConfig,
        *,
        output_root: Path,
        workspace_root: Path,
        is_busy=None,
    ) -> None:
        self.config = config
        self.output = Path(output_root)
        self.workspace = Path(workspace_root)
        # 有 queued/running 的任务时一律拒绝写入：SQLite 单写者，
        # 而且跑到一半的运行会读到半截 TM。
        self._is_busy = is_busy or (lambda: False)
        self._glossary = GlossaryRepository(config.glossary.file)
        self._terms: Optional[Tuple[GlossaryTerm, ...]] = None
        self._pipeline: Optional[LocalBuildPipeline] = None

    # ------------------------------------------------------------------ paths

    def _index_path(self, run_id: str) -> Optional[Path]:
        for mode in ("release", "preview"):
            candidate = self.output / mode / run_id / "reports" / INDEX_FILENAME
            if candidate.is_file():
                return candidate
        return None

    def _run_review_dir(self, run_id: str) -> Path:
        return self.workspace / "runs" / run_id / "review"

    def _ledger_path(self, run_id: str) -> Path:
        return self._run_review_dir(run_id) / "ledger.json"

    def _log(self) -> ReviewDecisionLog:
        configured = self.config.review.decisions_file
        if configured is None:
            configured = Path(self.config.glossary.file).parent / "review" / "decisions.jsonl"
        return ReviewDecisionLog(Path(configured))

    # ---------------------------------------------------------------- loading

    def terms(self) -> Tuple[GlossaryTerm, ...]:
        if self._terms is None:
            self._terms = tuple(self._glossary.load())
        return self._terms

    def pipeline(self) -> LocalBuildPipeline:
        if self._pipeline is None:
            self._pipeline = LocalBuildPipeline(
                validation_rule=load_validation_rule(
                    self.config.rules.file, source_locale=self.config.languages.source
                ),
                glossary_terms=self.terms(),
            )
        return self._pipeline

    def index(self, run_id: str) -> ReviewIndex:
        path = self._index_path(run_id)
        if path is None:
            raise ReviewUnavailable(
                f"运行 {run_id} 没有审查索引（qa-review-index.json）。"
                f"它由构建期产出 —— 重跑一次 preview 即可。"
            )
        return ReviewIndex.load(path)

    def rechecker(self, run_id: str) -> ReviewRechecker:
        return ReviewRechecker(self.index(run_id), self.pipeline())

    def ledger(self, run_id: str) -> ReviewLedger:
        return ReviewLedger.load(self._ledger_path(run_id))

    def human_translations(self, identities: Sequence[str]) -> Dict[str, str]:
        """这些坐标当前的**人工定稿**译文。

        sidecar 里的 translation 是那次运行产出的原值，落表之后它不会变 ——
        再点开一条已修复的条目却看到旧译文，人会以为自己的修改没保存。
        """
        keys = [key for key in identities if key]
        if not keys:
            return {}
        with SQLiteTranslationMemory(self.config.tm.database) as tm:
            rows = tm.rows_for(keys)
        return {
            key: row["translation"]
            for key, row in rows.items()
            if row.get("origin") == "human"
        }

    # ---------------------------------------------------------------- queries

    def session(self, run_id: str) -> Dict[str, Any]:
        try:
            index = self.index(run_id)
        except ReviewUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        ledger = self.ledger(run_id)
        groups = index.same_source_groups
        glossary_clusters = self._current_glossary_clusters(index)
        with_majority = sum(1 for g in groups if g.get("majority"))
        with_plurality = sum(1 for g in groups if self._plurality_of(g))
        collapsible = sum(1 for g in groups if g.get("normalized_collapse"))
        return {
            "available": True,
            "run_id": run_id,
            "log_revision": self._log().revision(),
            "mappings_empty": index.mappings_empty,
            "cursor": ledger.cursor,
            "counters": {
                **ledger.counters(),
                "glossary_clusters": len(glossary_clusters),
                "glossary_violations": sum(
                    c["violation_count"] for c in glossary_clusters
                ),
                "same_source_groups": len(groups),
                # 两种口径都在运行时计算：majority 是历史自动收敛的 >=80%，
                # plurality 是审查页显式一键操作的唯一最高频（无比例门槛）。
                "groups_with_majority": with_majority,
                "groups_with_plurality": with_plurality,
                "groups_normalized_collapse": collapsible,
                "groups_needing_case_by_case": len(groups) - with_plurality,
            },
            # same_source_inconsistency 是 warning，既不进基线也从不阻断 release。
            # 不写清楚，操作者会先啃最大的那堆，干完发现照样发不出去。
            "notes": {
                "same_source": "一致性收益，不解阻断 —— same_source_inconsistency 是 "
                               "warning，既不进存量债基线也从不阻断 release。"
                               "解阻断的是术语队列。",
                "authority": "面板不输出 release 结论。权威闸门只来自换新 run_id 的整轮运行。",
            },
        }

    def glossary_clusters(self, run_id: str) -> Dict[str, Any]:
        index = self.index(run_id)
        return {"available": True, "clusters": self._current_glossary_clusters(index)}

    def _term_for_cluster(
        self, cluster: Mapping[str, Any]
    ) -> Optional[GlossaryTerm]:
        matches = [
            term
            for term in self.terms()
            if term.source == cluster.get("source_term")
            and term.target == cluster.get("required_target")
            and term.scope == cluster.get("scope")
        ]
        if len(matches) > 1:
            raise ValueError(
                "glossary cluster is ambiguous: multiple terms share source/target/scope"
            )
        return matches[0] if matches else None

    def _current_glossary_clusters(self, index: ReviewIndex) -> List[Dict[str, Any]]:
        """用当前 glossary 覆盖不可变 sidecar 中的 scope 投影。

        QA sidecar 属于旧 run，不能重写；但操作者刚加完 exclude_scope 后，列表必须立即
        反映当前规则，否则看起来像“按钮没生效”。这里仅做非权威投影，正式结论仍靠重建。
        """
        rows: List[Dict[str, Any]] = []
        for raw in index.glossary_clusters:
            cluster = dict(raw)
            term = self._term_for_cluster(cluster)
            if term is not None:
                cluster.update(
                    {
                        "match_mode": term.match_mode,
                        "variants": list(term.variants),
                        "scope": term.scope,
                        "exclude_scope": list(term.exclude_scope),
                        "status": term.status,
                        "provenance": term.provenance,
                        "protected": term.human_reviewed,
                    }
                )
                identities = [
                    identity
                    for identity in cluster.get("violation_identities", ())
                    if identity in index.units
                    and term.applies_to(index.units[identity]["relative_path"])
                ]
                cluster["violation_identities"] = identities
                files = sorted(
                    {index.units[identity]["relative_path"] for identity in identities}
                )
                cluster["violation_count"] = len(identities)
                cluster["file_count"] = len(files)
                cluster["files"] = files
            rows.append(cluster)
        rows.sort(key=lambda item: (-item["violation_count"], item["source_term"]))
        return rows

    def groups(
        self,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        has_majority: Optional[bool] = None,
        query: str = "",
    ) -> Dict[str, Any]:
        index = self.index(run_id)
        ledger = self.ledger(run_id)
        rows = list(index.same_source_groups)
        if has_majority is not None:
            rows = [g for g in rows if bool(g.get("majority")) is has_majority]
        needle = (query or "").strip().lower()
        if needle:
            rows = [
                g
                for g in rows
                if needle in g["source"].lower()
                or any(needle in m["translation"].lower() for m in g["members"])
            ]
        committed = self.human_translations(
            [
                member["stable_identity"]
                for group in rows
                for member in group["members"]
            ]
        )
        decorated = []
        for group in rows:
            members = [member["stable_identity"] for member in group["members"]]
            unified = {committed[key] for key in members if key in committed}
            state = ledger.state_of(group["group_id"])
            # 全部成员都落了同一条人工译文才算这一组解决了。只解决一半的
            # 「统一」是最坏的结果：剩下的下一次运行会被重译出第 N 种译法。
            resolved = len(unified) == 1 and len(
                [k for k in members if k in committed]
            ) == len(members)
            decorated.append(
                {
                    **group,
                    "state": "committed" if resolved else state,
                    "resolved": resolved,
                    "committed_translation": next(iter(unified)) if unified else None,
                    "committed_members": len([k for k in members if k in committed]),
                    "variants": self._variants_of(group),
                    # 审查页“一键同步”使用无比例门槛的唯一最高频译法；它不同于
                    # ReviewIndex 中为历史自动收敛保留的 >=80% majority。
                    "plurality": self._plurality_of(group),
                }
            )
        # 已处理的沉到底部；其余按「有唯一最高频译法（可批量）、变体少」排。
        decorated.sort(
            key=lambda g: (
                _STATE_RANK.get(g["state"], 0),
                not g.get("plurality"),
                g["variant_count"],
                g["source"],
            )
        )
        window = decorated[offset : offset + max(1, min(limit, 500))]
        return {
            "available": True,
            "total": len(decorated),
            "offset": offset,
            "groups": window,
        }

    @staticmethod
    def _variants_of(group: Mapping[str, Any]) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        for member in group["members"]:
            text = member["translation"]
            counts[text] = counts.get(text, 0) + 1
        return [
            {"translation": text, "count": count}
            for text, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    @staticmethod
    def _plurality_of(group: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """返回非空译文中唯一的最高频译法；并列第一没有多数胜者。"""
        counts: Dict[str, int] = {}
        for member in group["members"]:
            text = str(member.get("translation") or "")
            if text:
                counts[text] = counts.get(text, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
            return None
        text, count = ranked[0]
        total = sum(counts.values())
        return {
            "translation": text,
            "count": count,
            "total": total,
            "ratio": round(count / total, 4),
        }

    def units(
        self, run_id: str, *, code: str, limit: int = 200, offset: int = 0
    ) -> Dict[str, Any]:
        index = self.index(run_id)
        ledger = self.ledger(run_id)
        matching = [
            (identity, payload)
            for identity, payload in sorted(index.units.items())
            if code in (payload.get("codes") or ())
        ]
        committed = self.human_translations([identity for identity, _ in matching])
        rows = []
        for identity, payload in matching:
            state = ledger.state_of(identity)
            rows.append(
                {
                    "stable_identity": identity,
                    **payload,
                    "state": state,
                    # 已定稿的当前译文。UI 靠它回显，而不是回显运行时的原值。
                    "committed_translation": committed.get(identity),
                    "resolved": identity in committed,
                }
            )
        # 已处理的沉到底部：定完一条还留在原地，会让人反复扫过做完的东西。
        rows.sort(
            key=lambda row: (
                _STATE_RANK.get(row["state"], 0),
                row["relative_path"],
                row["logical_key"],
            )
        )
        window = rows[offset : offset + max(1, min(limit, 500))]
        return {
            "available": True,
            "total": len(rows),
            "offset": offset,
            "units": window,
        }

    def unit(self, run_id: str, stable_identity: str) -> Optional[Dict[str, Any]]:
        index = self.index(run_id)
        payload = index.units.get(stable_identity)
        if payload is None:
            return None
        rule = PlaceholderRule.for_adapter(payload["adapter_id"])
        spans = []
        for term in self.terms():
            if term.status != "reviewed" or not term.applies_to(payload["relative_path"]):
                continue
            for start, end, form in term.find_source_spans(payload["source_text"]):
                spans.append(
                    {
                        "start": start,
                        "end": end,
                        "matched": form,
                        "source_term": term.source,
                        "required_target": term.target,
                        "satisfied": term.target in (payload.get("translation") or ""),
                    }
                )
        placeholders = [
            {"start": m.start(), "end": m.end(), "text": m.group(0)}
            for m in rule._pattern.finditer(payload["source_text"])
        ]
        group = None
        if payload.get("source_group"):
            group = index.group_for(payload["source_group"])
        committed = self.human_translations([stable_identity]).get(stable_identity)
        original = payload.get("translation") or ""
        return {
            "available": True,
            "stable_identity": stable_identity,
            **payload,
            # 回显：已定稿的看到自己写的那条，不是运行时的原值。
            "committed_translation": committed,
            "resolved": committed is not None,
            "original_translation": original,
            # 差异由服务端算（CSP 不允许外部库，手写 JS diff 又是一份没人测的实现）。
            "diff": diff_ops(original, committed) if committed is not None else [],
            # 高亮区间由服务端算，前端只渲染 —— 前端另写一套正则必然与判据漂移。
            "glossary_spans": sorted(spans, key=lambda s: s["start"]),
            "placeholder_spans": placeholders,
            "group": group,
            "ledger_state": self.ledger(run_id).state_of(stable_identity),
        }

    def decisions(self, run_id: str, *, limit: int = 100) -> Dict[str, Any]:
        events = self._log().read_all()
        rows = [
            {
                "decision_id": event.decision_id,
                "decided_at": event.decided_at,
                "action": event.action,
                "targets": list(event.targets),
                "translation": event.translation,
                "reason": event.reason,
                "actor": dict(event.actor),
                "details": dict(event.details),
            }
            for event in events
            if event.run_id == run_id
        ]
        return {"available": True, "total": len(rows), "decisions": rows[-limit:]}

    # ----------------------------------------------------------------- checks

    def recheck(
        self, run_id: str, edits: Mapping[str, str], *, scope: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.rechecker(run_id).check(edits, scope=scope).as_dict()

    # ----------------------------------------------------------------- writes

    def _actor(self) -> Dict[str, str]:
        # 这是操作会话与 OS 账户，**不是密码学身份**。面板无认证、只绑回环。
        try:
            os_user = getpass.getuser()
        except Exception:  # pragma: no cover
            os_user = ""
        return {
            "name": self.config.review.reviewer or os_user,
            "os_user": os_user,
            "host": socket.gethostname(),
            "pid": str(os.getpid()),
        }

    def _assert_writable(self) -> None:
        if self._is_busy():
            raise ReviewConflict(
                "有运行正在进行。TM 是单写者，且跑到一半的运行会读到半截数据 —— "
                "等它结束再提交。"
            )

    def commit(
        self,
        run_id: str,
        edits: Mapping[str, str],
        *,
        reason: str,
        expected_log_revision: Optional[str] = None,
        accepted_debt: Optional[Mapping[str, Any]] = None,
        allow_remote_override: bool = False,
        action: str = "commit",
        _trusted_bulk: bool = False,
    ) -> CommitOutcome:
        self._assert_writable()
        if not edits:
            raise ValueError("commit requires at least one edit")
        if len(edits) > MAX_COMMIT_ITEMS and not _trusted_bulk:
            raise ValueError(
                f"commit accepts at most {MAX_COMMIT_ITEMS} items per request; "
                f"split into chunks and carry the returned log_revision forward"
            )
        if not reason.strip():
            raise ValueError("commit requires a non-empty reason")

        index = self.index(run_id)
        # **服务端自己重跑判据**，不信客户端传来的结果。
        result = self.rechecker(run_id).check(edits)
        if result.unknown_identities:
            raise ValueError(
                f"these identities are not in this run's review index: "
                f"{', '.join(result.unknown_identities)}"
            )
        introduced = result.introduced_errors
        if introduced:
            allowed = set((accepted_debt or {}).get("codes") or ())
            unexplained = [
                f"{identity}:{code}" for identity, code in introduced if code not in allowed
            ]
            if unexplained or not str((accepted_debt or {}).get("reason", "")).strip():
                raise ValueError(
                    "这次编辑引入了新的 error："
                    + ", ".join(unexplained or [f"{i}:{c}" for i, c in introduced])
                    + "。确认要带着它们落表的话，请提供 accepted_debt{codes, reason}。"
                )

        entries = []
        with SQLiteTranslationMemory(self.config.tm.database) as tm:
            before = tm.rows_for(list(edits))
            for identity, text in edits.items():
                payload = index.units[identity]
                entries.append(
                    TMEntry(
                        stable_identity=identity,
                        project_id=self.config.project.id,
                        adapter_id=payload["adapter_id"],
                        relative_path=payload["relative_path"],
                        logical_key=payload["logical_key"],
                        source_text=payload["source_text"],
                        source_fingerprint=payload["source_fingerprint"],
                        translation=text,
                        **HUMAN_REVIEW_FIELDS,
                    )
                )
            try:
                outcome = tm.apply_human_review(
                    entries, allow_remote_override=allow_remote_override
                )
            except TMGuardError as exc:
                raise ReviewConflict(str(exc)) from exc

        log = self._log()
        audit_id = uuid.uuid4().hex
        events = []
        for identity in outcome.written:
            events.append(
                ReviewDecisionEvent(
                    action="accept_debt" if accepted_debt else action,
                    run_id=run_id,
                    targets=(identity,),
                    translation=edits[identity],
                    reason=reason,
                    before={identity: before.get(identity)},
                    details={
                        "audit_id": audit_id,
                        "accepted_debt": dict(accepted_debt or {}),
                    },
                    actor=self._actor(),
                )
            )
        revision = log.append(events, expected_revision=expected_log_revision)

        ledger = self.ledger(run_id)
        for event in events:
            for target in event.targets:
                ledger.mark(
                    target, "committed",
                    decision_id=event.decision_id,
                    translation=event.translation,
                )
        self._ledger_path(run_id).parent.mkdir(parents=True, exist_ok=True)
        ledger.save()

        return CommitOutcome(
            audit_id=audit_id,
            requested=len(edits),
            written=len(outcome.written),
            guarded=outcome.guarded,
            rejected=(),
            log_revision=revision,
            committed_target_ids=outcome.written,
        )

    def unify(
        self,
        run_id: str,
        group_id: str,
        translation: str,
        *,
        reason: str,
        expected_log_revision: Optional[str] = None,
        allow_remote_override: bool = False,
    ) -> CommitOutcome:
        """把一个同源组的**全部成员**统一到同一条译文。

        必须为每个成员各写一行：靠「写一条然后指望同源传播」在真机上不成立 ——
        `_resolve` 的第一分支是 `lookup()`，它对 legacy_clean 影子行照样返回，
        `lookup_reviewed_source` 根本轮不到。
        成员含空译文的那些（QA 记录里看不见），漏掉就只统一了一半。
        """
        index = self.index(run_id)
        group = index.group_for(group_id)
        if group is None:
            raise ValueError(f"unknown same-source group: {group_id}")
        edits = {member["stable_identity"]: translation for member in group["members"]}
        return self.commit(
            run_id,
            edits,
            reason=reason,
            expected_log_revision=expected_log_revision,
            allow_remote_override=allow_remote_override,
            action="unify",
        )

    def unify_majorities(
        self,
        run_id: str,
        *,
        reason: str,
        expected_log_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把全部未解决同源多译组按唯一最高频非空译文同步到组内坐标。

        这是审查阶段的显式人工批量动作，不沿用历史自动收敛的 >=80% 门槛。
        只要某个非空译文的出现次数唯一最高就胜出；并列第一没有可定义的多数，
        因此跳过并在结果中报告。空译文不参与投票，但会被胜出译文补齐。

        普通客户端 commit 的 100 条上限是请求面防护；这里的目标集合完全来自服务端
        review index，因此可以走受信批处理。先一次性重验全部候选，任何会引入新 error
        的组整组跳过；其余坐标通过同一个 TM 事务写入，避免 2000 组反复读取日志和 ledger。
        受远端保护的坐标仍不写，所属组会明确显示为不完整。
        """
        self._assert_writable()
        if not reason.strip():
            raise ValueError("majority sync requires a non-empty reason")
        log = self._log()
        revision = log.revision()
        if expected_log_revision is not None and expected_log_revision != revision:
            raise LogRevisionMismatch(
                "review log changed since you read it "
                f"(expected {expected_log_revision}, now {revision}); reload and retry"
            )

        # 先做全部展示组的稳定快照，再开始写。否则每统一一组都会改变排序，
        # offset 翻页会漏组。
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = self.groups(run_id, limit=500, offset=offset)
            batch = list(page["groups"])
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page["total"]):
                break
        unresolved = [row for row in rows if not row.get("resolved")]
        eligible = [row for row in unresolved if row.get("plurality")]
        skipped = [row for row in unresolved if not row.get("plurality")]

        all_edits = {
            member["stable_identity"]: str(group["plurality"]["translation"])
            for group in eligible
            for member in group["members"]
        }
        precheck = self.rechecker(run_id).check(all_edits)
        introduced_by_identity: Dict[str, List[str]] = {}
        for identity, code in precheck.introduced_errors:
            introduced_by_identity.setdefault(identity, []).append(code)
        validation_skipped = [
            group
            for group in eligible
            if any(
                member["stable_identity"] in introduced_by_identity
                for member in group["members"]
            )
        ]
        validation_skipped_ids = {group["group_id"] for group in validation_skipped}
        writable_groups = [
            group for group in eligible if group["group_id"] not in validation_skipped_ids
        ]
        writable_edits = {
            member["stable_identity"]: str(group["plurality"]["translation"])
            for group in writable_groups
            for member in group["members"]
        }

        outcome: Optional[CommitOutcome] = None
        if writable_edits:
            outcome = self.commit(
                run_id,
                writable_edits,
                reason=reason,
                expected_log_revision=revision,
                action="unify",
                _trusted_bulk=True,
            )
            revision = outcome.log_revision

        written_ids = set(outcome.committed_target_ids if outcome else ())
        guarded = dict(outcome.guarded if outcome else ())
        audit_id = outcome.audit_id if outcome else ""
        results = []
        for group in eligible:
            plurality = group["plurality"]
            identities = [member["stable_identity"] for member in group["members"]]
            group_written = [identity for identity in identities if identity in written_ids]
            group_guarded = [
                {"stable_identity": identity, "reason": guarded[identity]}
                for identity in identities
                if identity in guarded
            ]
            validation_errors = [
                {"stable_identity": identity, "codes": introduced_by_identity[identity]}
                for identity in identities
                if identity in introduced_by_identity
            ]
            complete = len(group_written) == len(identities) and not validation_errors
            results.append(
                {
                    "group_id": group["group_id"],
                    "source": group["source"],
                    "translation": plurality["translation"],
                    "ratio": plurality.get("ratio"),
                    "audit_id": audit_id if group_written else "",
                    "complete": complete,
                    "requested": len(identities),
                    "written": len(group_written),
                    "guarded": group_guarded,
                    "rejected": [],
                    "validation_errors": validation_errors,
                    "committed_target_ids": group_written,
                }
            )

        completed = sum(1 for item in results if item["complete"])
        return {
            "strategy": "unique_non_empty_plurality",
            "minimum_ratio": None,
            "groups_total": len(rows),
            "groups_unresolved": len(unresolved),
            "groups_eligible": len(eligible),
            "groups_skipped_tied": len(skipped),
            "groups_skipped_validation": len(validation_skipped),
            "groups_completed": completed,
            "groups_incomplete": len(results) - completed,
            "items_requested": sum(int(item["requested"]) for item in results),
            "items_written": sum(int(item["written"]) for item in results),
            "log_revision": revision,
            "skipped": [
                {"group_id": row["group_id"], "source": row["source"]}
                for row in skipped
            ],
            "results": results,
        }

    def exclude_glossary_scope(
        self,
        run_id: str,
        cluster_id: str,
        path_glob: str,
        *,
        reason: str,
        expected_log_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把当前术语从指定资源路径排除，并留下双重审计。

        术语文件是版本控制资产，human+reviewed 又受 G01 绝对保护，因此不能走普通
        ``upsert_term``。这里复用 destructive maintenance：自动备份、diff、维护日志，
        同时在面板 append-only 决策日志登记操作者、理由和前后像。
        """
        self._assert_writable()
        if not reason.strip():
            raise ValueError("glossary exclusion requires a non-empty reason")
        pattern = str(path_glob or "").strip().replace("\\", "/")
        if not pattern:
            raise ValueError("glossary exclusion requires a path or path glob")
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts or ":" in pattern:
            raise ValueError("exclude_scope must be a project-relative POSIX path glob")
        if pattern in {"*", "**", "*.mo", "**/*", "**/*.mo"}:
            raise ValueError(
                "exclude_scope is too broad for the review panel; use glossary maintenance "
                "to disable or rescope a term"
            )

        index = self.index(run_id)
        cluster = index.cluster_for(cluster_id)
        if cluster is None:
            raise ValueError(f"unknown glossary cluster: {cluster_id}")
        matching_files = [
            path
            for path in cluster.get("files", ())
            if fnmatch.fnmatchcase(path, pattern)
        ]
        if not matching_files:
            raise ValueError(
                "exclude_scope does not match any affected file in this review cluster"
            )
        term = self._term_for_cluster(cluster)
        if term is None:
            raise ValueError("glossary term behind this cluster no longer exists")

        log = self._log()
        revision = log.revision()
        if expected_log_revision is not None and expected_log_revision != revision:
            raise LogRevisionMismatch(
                "review log changed since you read it "
                f"(expected {expected_log_revision}, now {revision}); reload and retry"
            )
        if pattern in term.exclude_scope:
            return {
                "complete": True,
                "changed": False,
                "source_term": term.source,
                "required_target": term.target,
                "exclude_scope": list(term.exclude_scope),
                "matched_files": matching_files,
                "log_revision": revision,
            }

        updated = replace(
            term,
            exclude_scope=tuple((*term.exclude_scope, pattern)),
        )
        terms = list(self.terms())
        terms[terms.index(term)] = updated
        self._glossary.destructive_replace_all(
            terms,
            destructive=True,
            reason=reason,
        )
        self._terms = tuple(terms)
        self._pipeline = None
        event = ReviewDecisionEvent(
            action="glossary",
            run_id=run_id,
            targets=(cluster_id,),
            reason=reason,
            before={cluster_id: asdict(term)},
            details={
                "operation": "add_exclude_scope",
                "path_glob": pattern,
                "matched_files": matching_files,
                "after": asdict(updated),
                "maintenance_directory": str(
                    self._glossary.maintenance_directory
                ),
            },
            actor=self._actor(),
        )
        revision = log.append([event], expected_revision=revision)
        return {
            "complete": True,
            "changed": True,
            "source_term": updated.source,
            "required_target": updated.target,
            "exclude_scope": list(updated.exclude_scope),
            "matched_files": matching_files,
            "decision_id": event.decision_id,
            "log_revision": revision,
        }

    def revert(
        self,
        run_id: str,
        decision_ids: Sequence[str],
        *,
        expected_log_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._assert_writable()
        wanted = set(decision_ids)
        if not wanted:
            raise ValueError("revert requires at least one decision_id")
        log = self._log()
        events = [event for event in log.read_all() if event.decision_id in wanted]
        missing = wanted - {event.decision_id for event in events}
        if missing:
            raise ValueError(f"unknown decision ids: {', '.join(sorted(missing))}")

        snapshots = []
        targets: List[str] = []
        for event in events:
            for identity in event.targets:
                snapshots.append(
                    {"stable_identity": identity, "row": event.before.get(identity)}
                )
                targets.append(identity)
        with SQLiteTranslationMemory(self.config.tm.database) as tm:
            try:
                restored = tm.restore_rows(snapshots)
            except TMGuardError as exc:
                raise ReviewConflict(str(exc)) from exc

        revision = log.append(
            [
                ReviewDecisionEvent(
                    action="revert",
                    run_id=run_id,
                    targets=tuple(targets),
                    reason="撤销",
                    details={"reverted_decision_ids": sorted(wanted)},
                    actor=self._actor(),
                )
            ],
            expected_revision=expected_log_revision,
        )
        ledger = self.ledger(run_id)
        for identity in targets:
            ledger.mark(identity, "reverted")
        self._ledger_path(run_id).parent.mkdir(parents=True, exist_ok=True)
        ledger.save()
        return {"restored": restored, "targets": targets, "log_revision": revision}

    def mark(
        self,
        run_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        expected_log_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """草稿 / 跳过 / 待议。不写 TM，只进日志与 ledger。"""
        if len(items) > MAX_DECISION_ITEMS:
            raise ValueError(
                f"at most {MAX_DECISION_ITEMS} items per request"
            )
        actor = self._actor()
        events = []
        for item in items:
            action = str(item.get("action", ""))
            if action not in {"draft", "skip", "defer"}:
                raise ValueError(f"mark only accepts draft/skip/defer, got {action!r}")
            events.append(
                ReviewDecisionEvent(
                    action=action,
                    run_id=run_id,
                    targets=(str(item["target_id"]),),
                    translation=item.get("translation"),
                    reason=str(item.get("reason", "")),
                    actor=actor,
                )
            )
        revision = self._log().append(events, expected_revision=expected_log_revision)
        ledger = self.ledger(run_id)
        state_of = {"draft": "draft", "skip": "skipped", "defer": "deferred"}
        for event in events:
            for target in event.targets:
                ledger.mark(
                    target, state_of[event.action],
                    decision_id=event.decision_id,
                    translation=event.translation,
                )
        if items:
            ledger.cursor = str(items[-1]["target_id"])
        self._ledger_path(run_id).parent.mkdir(parents=True, exist_ok=True)
        ledger.save()
        return {"log_revision": revision, "counters": ledger.counters()}
