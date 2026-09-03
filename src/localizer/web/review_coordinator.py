"""Review mutation coordination at the local Dashboard boundary.

The legacy :class:`ReviewService` owns the baseline validation/business semantics.
This module adds three production-boundary guarantees that need the current TM
snapshot under one process-local lock:

1. every Dashboard Review mutation shares the same maintenance RLock as task
   submission / TM maintenance, so ``check -> TM -> decision log`` cannot be
   interleaved by another Dashboard write;
2. newly appended TM-mutation decisions carry a complete ``after`` row snapshot;
3. same-source ``unify`` fails closed before overwriting an existing divergent
   Review-owned human finalization.

It deliberately does not add a second audit store, override workflow, or duplicate
commit/unify implementations. Old ReviewService callers remain compatible;
production Dashboard services use the coordinated subclass.
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

from .review import ReviewConflict, ReviewService

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
        with self._mutation_lock:
            return super().mark(run_id, items, **kwargs)

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
