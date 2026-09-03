"""Review coordination and current-state projection at the local Dashboard boundary.

The legacy :class:`ReviewService` owns the baseline validation/business semantics.
This module adds five production-boundary guarantees that need current Review/TM
state without broadening TM identity or QA semantics:

1. every Dashboard Review mutation shares the same maintenance RLock as task
   submission / TM maintenance, so ``check -> TM -> decision log`` cannot be
   interleaved by another Dashboard write;
2. newly appended TM-mutation decisions carry a complete ``after`` row snapshot;
3. same-source ``unify`` fails closed before overwriting an existing divergent
   Review-owned human finalization;
4. same-source Review reads expose current TM rows for operator preview without
   making preview state part of mutation semantics;
5. an explicitly acknowledged contextual variant is remembered by exact
   ``group_id + member set`` and suppressed only from unresolved Review work.

It deliberately does not add a second audit store, exception table, context matcher,
or workflow/state-machine layer. Raw QA/ReviewIndex evidence stays immutable; the
append-only Review decision log remains the authority for the acknowledgement.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMGuardError,
)
from localizer.application.review_log import (
    LogRevisionMismatch,
    ReviewDecisionEvent,
    ReviewDecisionLog,
)

from .review import MAX_DECISION_ITEMS, ReviewConflict, ReviewService

_TM_MUTATION_ACTIONS = {"commit", "unify", "accept_debt", "retire", "revert"}


class SnapshottingReviewDecisionLog(ReviewDecisionLog):
    """Append the authoritative event together with the TM state it produced."""

    def __init__(self, path: Path, tm_database: Path) -> None:
        super().__init__(path)
        self.tm_database = Path(tm_database)

    def append(
        self,
        events: Sequence[ReviewDecisionEvent],
        *,
        expected_revision: Optional[str] = None,
    ) -> str:
        candidates = [
            event
            for event in events
            if event.action in _TM_MUTATION_ACTIONS and event.targets and not event.after
        ]
        if not candidates:
            return super().append(events, expected_revision=expected_revision)

        identities = list(
            dict.fromkeys(target for event in candidates for target in event.targets)
        )
        with SQLiteTranslationMemory(self.tm_database, read_only=True) as tm:
            rows = tm.rows_for(identities)

        candidate_ids = {id(event) for event in candidates}
        enriched = []
        for event in events:
            if id(event) not in candidate_ids:
                enriched.append(event)
                continue
            enriched.append(
                replace(
                    event,
                    after={target: rows.get(target) for target in event.targets},
                )
            )
        return super().append(enriched, expected_revision=expected_revision)


class CoordinatedReviewService(ReviewService):
    """ReviewService with one explicit production mutation critical section.

    ``RLock`` is intentional: ``unify_majorities`` enters the outer guard and then
    calls ``self.commit()``, which re-enters the same lock. The Dashboard passes the
    *same* lock to TaskService, closing the former race where a task could be queued
    after ``_assert_writable`` but before the Review TM write.
    """

    def __init__(self, *args, mutation_lock=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mutation_lock = mutation_lock or threading.RLock()

    @property
    def mutation_lock(self):
        return self._mutation_lock

    def _log(self) -> ReviewDecisionLog:
        base = super()._log()
        return SnapshottingReviewDecisionLog(base.path, self.config.tm.database)

    @staticmethod
    def _matches_intended_human_write(row, translation: str) -> bool:
        if row is None or row.get("translation") != translation:
            return False
        for field, expected in HUMAN_REVIEW_FIELDS.items():
            actual = row.get(field)
            if isinstance(expected, bool):
                actual = bool(actual)
            if actual != expected:
                return False
        return True

    @staticmethod
    def _group_signature(group):
        return (
            str(group.get("group_id") or ""),
            frozenset(
                str(member.get("stable_identity") or "")
                for member in group.get("members") or ()
                if member.get("stable_identity")
            ),
        )

    def _intentional_variant_signatures(self):
        """Return durable acknowledgements from the existing append-only log.

        The event's run_id is intentionally ignored here: persistence across run IDs
        is the feature. Exact membership is the safety boundary, so a newly appearing
        coordinate makes the current signature different and the group reappears.
        """
        return {
            (
                str(event.details.get("group_id") or ""),
                frozenset(str(target) for target in event.targets if target),
            )
            for event in self._log().read_all()
            if event.action == "intentional_variant"
            and event.details.get("group_id")
            and event.targets
        }

    def session(self, run_id: str):
        """Keep counters aligned with unresolved Review work, not raw QA evidence."""
        payload = super().session(run_id)
        if not payload.get("available"):
            return payload
        raw_groups = list(self.index(run_id).same_source_groups)
        acknowledged = self._intentional_variant_signatures()
        groups = [
            group
            for group in raw_groups
            if self._group_signature(group) not in acknowledged
        ]
        with_majority = sum(1 for group in groups if group.get("majority"))
        with_plurality = sum(1 for group in groups if self._plurality_of(group))
        collapsible = sum(1 for group in groups if group.get("normalized_collapse"))
        counters = payload["counters"]
        counters.update(
            {
                # Raw evidence remains in ReviewIndex/QA; this is the unresolved UI count.
                "same_source_diagnostic_groups": len(raw_groups),
                "same_source_groups": len(groups),
                "intentional_variant_groups": len(raw_groups) - len(groups),
                "groups_with_majority": with_majority,
                "groups_with_plurality": with_plurality,
                "groups_normalized_collapse": collapsible,
                "groups_needing_case_by_case": len(groups) - with_plurality,
            }
        )
        return payload

    def groups(self, run_id: str, **kwargs):
        """Hide only exact acknowledged groups, then overlay current TM evidence.

        Filtering happens before this method's outward pagination. The base service is
        still the single implementation of group decoration/sorting; we only consume
        its pages, remove exact acknowledged signatures, and page the remainder.
        """
        limit = max(1, min(int(kwargs.get("limit", 100)), 500))
        offset = max(0, int(kwargs.get("offset", 0)))
        has_majority = kwargs.get("has_majority")
        query = str(kwargs.get("query", ""))
        acknowledged = self._intentional_variant_signatures()

        rows = []
        hidden = 0
        base_offset = 0
        while True:
            page = super().groups(
                run_id,
                limit=500,
                offset=base_offset,
                has_majority=has_majority,
                query=query,
            )
            batch = list(page.get("groups") or ())
            for group in batch:
                if self._group_signature(group) in acknowledged:
                    hidden += 1
                else:
                    rows.append(group)
            base_offset += len(batch)
            if not batch or base_offset >= int(page.get("total", 0)):
                break

        window = rows[offset : offset + limit]
        identities = [
            member["stable_identity"]
            for group in window
            for member in group.get("members") or ()
            if member.get("stable_identity")
        ]
        if not identities:
            return {
                "available": True,
                "total": len(rows),
                "offset": offset,
                "intentional_variant_hidden": hidden,
                "groups": window,
            }

        with SQLiteTranslationMemory(self.config.tm.database, read_only=True) as tm:
            current = tm.rows_for(identities)

        decorated = []
        for group in window:
            members = []
            for raw in group.get("members") or ():
                member = dict(raw)
                row = current.get(member.get("stable_identity"))
                member.update(
                    {
                        "current_translation": (
                            str(row.get("translation") or "")
                            if row is not None
                            else str(member.get("translation") or "")
                        ),
                        "current_from_tm": row is not None,
                        "current_origin": str(row.get("origin") or "") if row else "",
                        "current_review_state": (
                            str(row.get("review_state") or "") if row else ""
                        ),
                        "current_is_formal": bool(row.get("is_formal")) if row else False,
                        "current_human_authored": (
                            bool(row.get("human_authored")) if row else False
                        ),
                    }
                )
                members.append(member)
            decorated.append({**group, "members": members})
        return {
            "available": True,
            "total": len(rows),
            "offset": offset,
            "intentional_variant_hidden": hidden,
            "groups": decorated,
        }

    def commit(self, run_id: str, edits: Mapping[str, str], **kwargs):
        """Serialize Review writes and keep same-source convenience actions conservative.

        Under the shared lock, a local Dashboard writer cannot change the Review log
        revision between TM write and log append. If a non-coordinated/external writer
        still causes ``LogRevisionMismatch``, compensation only touches coordinates
        that (a) differ from their captured before-image and (b) still exactly match
        this commit's intended fixed human-write shape. Anything else fails closed.

        ``unify`` has one additional KISS guard: if any target already contains a
        different local ``human + reviewed + formal`` value, reject the whole unify
        before writing anything. There is deliberately no override flag in this slice.
        """
        with self._mutation_lock:
            with SQLiteTranslationMemory(self.config.tm.database) as tm:
                before = tm.rows_for(list(edits))

            if kwargs.get("action") == "unify":
                divergent = [
                    identity
                    for identity, translation in edits.items()
                    if (row := before.get(identity)) is not None
                    and row.get("origin") == "human"
                    and row.get("review_state") == "reviewed"
                    and bool(row.get("is_formal"))
                    and row.get("translation") != translation
                ]
                if divergent:
                    labels = []
                    for identity in divergent[:10]:
                        row = before[identity]
                        path = str(row.get("relative_path") or "")
                        key = str(row.get("logical_key") or identity)
                        labels.append(f"{path}:{key}" if path else key)
                    more = "" if len(divergent) <= 10 else f" 等 {len(divergent)} 条"
                    raise ReviewConflict(
                        "同源统一会覆盖已有且译文不同的人工定稿；已拒绝整个操作。"
                        "请逐条确认或保留该语境译法："
                        + ", ".join(labels)
                        + more
                    )

            try:
                return super().commit(run_id, edits, **kwargs)
            except LogRevisionMismatch:
                with SQLiteTranslationMemory(self.config.tm.database) as tm:
                    current = tm.rows_for(list(edits))
                    snapshots = []
                    unsafe = []
                    for identity, translation in edits.items():
                        old = before.get(identity)
                        now = current.get(identity)
                        if now == old:
                            continue
                        if not self._matches_intended_human_write(now, translation):
                            unsafe.append(identity)
                            continue
                        snapshots.append({"stable_identity": identity, "row": old})
                    if unsafe:
                        raise ReviewConflict(
                            "Review 日志并发冲突后检测到坐标已不是本次写入状态；"
                            "拒绝自动补偿以免覆盖外部更新："
                            + ", ".join(unsafe[:10])
                        )
                    try:
                        if snapshots:
                            tm.restore_rows(snapshots)
                    except TMGuardError as exc:
                        raise ReviewConflict(
                            "Review 日志并发冲突后 TM 补偿失败；停止继续写入并人工核对："
                            + str(exc)
                        ) from exc
                raise

    def unify_majorities(self, run_id: str, **kwargs):
        with self._mutation_lock:
            return super().unify_majorities(run_id, **kwargs)

    def exclude_glossary_scope(self, run_id: str, cluster_id: str, path_glob: str, **kwargs):
        with self._mutation_lock:
            return super().exclude_glossary_scope(run_id, cluster_id, path_glob, **kwargs)

    def mark(self, run_id: str, items: Sequence[Mapping[str, object]], **kwargs):
        """Reuse the existing decisions endpoint for whole-group acknowledgements."""
        with self._mutation_lock:
            actions = {str(item.get("action", "")) for item in items}
            if "intentional_variant" not in actions:
                return super().mark(run_id, items, **kwargs)
            if actions != {"intentional_variant"}:
                raise ValueError(
                    "intentional_variant decisions cannot be mixed with draft/skip/defer"
                )
            if len(items) > MAX_DECISION_ITEMS:
                raise ValueError(f"at most {MAX_DECISION_ITEMS} items per request")

            index = self.index(run_id)
            actor = self._actor()
            events = []
            for item in items:
                group_id = str(item.get("target_id", ""))
                reason = str(item.get("reason", "")).strip()
                if not reason:
                    raise ValueError("intentional_variant requires a non-empty reason")
                group = index.group_for(group_id)
                if group is None:
                    raise ValueError(f"unknown same-source group: {group_id}")
                # The server derives membership from this run's immutable index; the
                # client never gets to choose which coordinates the acknowledgement covers.
                members = tuple(
                    sorted(
                        str(member["stable_identity"])
                        for member in group.get("members") or ()
                    )
                )
                events.append(
                    ReviewDecisionEvent(
                        action="intentional_variant",
                        run_id=run_id,
                        targets=members,
                        reason=reason,
                        details={"group_id": group_id},
                        actor=actor,
                    )
                )
            revision = self._log().append(
                events,
                expected_revision=kwargs.get("expected_log_revision"),
            )
            return {
                "log_revision": revision,
                "counters": self.ledger(run_id).counters(),
                "intentional_variant_groups": len(events),
            }

    def revert(
        self,
        run_id: str,
        decision_ids: Sequence[str],
        *,
        expected_log_revision: Optional[str] = None,
    ):
        """Keep direct production callers on the same stale-safe recovery path."""
        from .review_recovery import safe_revert

        return safe_revert(
            self,
            run_id,
            decision_ids,
            reason="撤销",
            expected_log_revision=expected_log_revision,
        )
