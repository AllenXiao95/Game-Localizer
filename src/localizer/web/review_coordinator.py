"""Review mutation coordination at the local Dashboard boundary.

The legacy :class:`ReviewService` owns validation/business semantics.  This module
adds only two cross-cutting guarantees that became necessary once Recovery grew:

1. every Dashboard Review mutation shares the same process-local maintenance RLock
   as task submission / TM maintenance, so ``check -> TM -> decision log`` cannot be
   interleaved by another Dashboard write;
2. newly appended TM-mutation decisions carry a complete ``after`` row snapshot.

It deliberately does not add a second audit store or duplicate commit/unify rules.
Old ReviewService callers remain compatible; production Dashboard services use the
coordinated subclass.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMGuardError
from localizer.application.review_log import (
    LogRevisionMismatch,
    ReviewDecisionEvent,
    ReviewDecisionLog,
)

from .review import ReviewConflict, ReviewService

_TM_MUTATION_ACTIONS = {"commit", "unify", "accept_debt", "retire", "revert"}


class SnapshottingReviewDecisionLog(ReviewDecisionLog):
    """Append the authoritative event together with the TM state it produced.

    The TM mutation has already completed when ReviewService calls ``append``.  The
    shared mutation lock guarantees another Dashboard Review write cannot slip into
    the gap, so the rows read here are the exact post-state for this decision.
    """

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

        enriched = []
        for event in events:
            if event not in candidates:
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
    """ReviewService with one explicit mutation critical section.

    ``RLock`` is intentional: ``unify_majorities`` enters the outer guard and then
    calls ``self.commit()``, which re-enters the same lock.  The Dashboard passes the
    *same* lock to TaskService, closing the former race where a task could be queued
    after ``_assert_writable`` but before the Review TM write.
    """

    def __init__(
        self,
        *args,
        mutation_lock=None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._mutation_lock = mutation_lock or threading.RLock()

    @property
    def mutation_lock(self):
        return self._mutation_lock

    def _log(self) -> ReviewDecisionLog:
        base = super()._log()
        return SnapshottingReviewDecisionLog(base.path, self.config.tm.database)

    def commit(self, run_id: str, edits: Mapping[str, str], **kwargs):
        """Serialize commit and compensate the narrow optimistic-race failure.

        ``ReviewService.commit`` writes TM before appending the decision.  Under this
        shared lock, a local Dashboard writer cannot change the revision in between.
        If an external writer still wins and append reports LogRevisionMismatch, the
        recorded before-images are restored before surfacing the conflict so TM does
        not silently diverge from the append-only authority.
        """
        with self._mutation_lock:
            with SQLiteTranslationMemory(self.config.tm.database) as tm:
                before = tm.rows_for(list(edits))
            try:
                return super().commit(run_id, edits, **kwargs)
            except LogRevisionMismatch:
                snapshots = [
                    {"stable_identity": identity, "row": before.get(identity)}
                    for identity in edits
                ]
                try:
                    with SQLiteTranslationMemory(self.config.tm.database) as tm:
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
