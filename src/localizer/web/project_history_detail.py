from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Sequence

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.application.review_log import ReviewDecisionEvent

from .review import ReviewService
from .review_recovery import (
    _RECOVERABLE_ACTIONS,
    _coordinate_row,
    _latest_mutation_by_target,
    _project_coordinate_status,
    _reverted_decision_ids,
    _run_units,
    safe_revert,
)

_COORDINATE_STATUSES = {"all", "current", "superseded", "reverted", "recorded"}
_RECOVERY_FILTERS = {"all", "revertible", "blocked"}


def _group_events(
    service: ReviewService,
    *,
    run_id: str,
    action: str,
    audit_id: str,
) -> tuple[list[ReviewDecisionEvent], list[ReviewDecisionEvent]]:
    all_events = service._log().read_all()
    events = [
        event
        for event in all_events
        if event.run_id == run_id
        and event.action == action
        and str(event.details.get("audit_id") or event.decision_id) == audit_id
    ]
    if not events:
        raise ValueError("project history operation not found")
    return all_events, events


def project_history_coordinates(
    service: ReviewService,
    *,
    run_id: str,
    action: str,
    audit_id: str,
    query: str = "",
    status: str = "all",
    recovery: str = "all",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Paged coordinate inspection for one project-level Review operation.

    This module is now read-only.  It resolves the same evidence as the canonical
    :func:`safe_revert`, but never restores TM itself.
    """
    if status not in _COORDINATE_STATUSES:
        raise ValueError(f"unsupported coordinate status: {status}")
    if recovery not in _RECOVERY_FILTERS:
        raise ValueError(f"unsupported recovery filter: {recovery}")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    needle = str(query or "").strip().casefold()

    all_events, events = _group_events(
        service, run_id=run_id, action=action, audit_id=audit_id
    )
    latest_mutation = _latest_mutation_by_target(all_events)
    reverted_decisions = _reverted_decision_ids(all_events)
    identities = list(dict.fromkeys(target for event in events for target in event.targets))
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(identities)
    run_units = _run_units(service, run_id)

    rows = []
    status_counts: Counter[str] = Counter()
    proof_counts: Counter[str] = Counter()
    operation_revertible_total = 0
    for event in events:
        for identity in event.targets:
            coordinate_status = _project_coordinate_status(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                reverted_decisions=reverted_decisions,
            )
            row = _coordinate_row(
                event=event,
                identity=identity,
                status=coordinate_status,
                current=current_rows.get(identity),
                latest_mutation=latest_mutation,
                run_units=run_units,
            )
            status_counts[coordinate_status] += 1
            proof_counts[row["recovery_proof"]] += 1
            if row["revertible"]:
                operation_revertible_total += 1

            if status != "all" and coordinate_status != status:
                continue
            if recovery == "revertible" and not row["revertible"]:
                continue
            if recovery == "blocked" and row["revertible"]:
                continue
            if needle:
                haystack = "\n".join(
                    str(row.get(field) or "")
                    for field in (
                        "relative_path",
                        "logical_key",
                        "source_text",
                        "stable_identity",
                        "before_translation",
                        "after_translation",
                        "current_translation",
                        "conflict_reason",
                    )
                ).casefold()
                if needle not in haystack:
                    continue
            rows.append(row)

    total = len(rows)
    page = rows[offset : offset + limit]
    return {
        "available": True,
        "run_id": run_id,
        "action": action,
        "audit_id": audit_id,
        "query": query,
        "status": status,
        "recovery": recovery,
        "total": total,
        "offset": offset,
        "limit": limit,
        "coordinates": page,
        "status_counts": dict(status_counts),
        "proof_counts": dict(proof_counts),
        "revertible_total": sum(1 for row in rows if row["revertible"]),
        "operation_revertible_total": operation_revertible_total,
        "log_revision": service._log().revision(),
        "run_index_available": run_units is not None,
    }


def safe_revert_with_history_fallback(
    service: ReviewService,
    run_id: str,
    decision_ids: Sequence[str],
    *,
    reason: str,
    expected_log_revision: str | None = None,
) -> Dict[str, Any]:
    """Compatibility alias; recovery now has exactly one implementation."""
    return safe_revert(
        service,
        run_id,
        decision_ids,
        reason=reason,
        expected_log_revision=expected_log_revision,
    )
