from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Mapping, Optional, Sequence

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory
from localizer.application.review_log import ReviewDecisionEvent

from .review import ReviewService, ReviewUnavailable
from .review_recovery import (
    _RECOVERABLE_ACTIONS,
    _coordinate_value,
    _latest_mutation_by_target,
    _project_coordinate_status,
    _revertibility,
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


def _before_anchor_payload(
    before: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build fallback freshness evidence when an old ReviewIndex is gone.

    A Review write changes translation/authority, not the coordinate's source identity.
    Therefore an old decision may still be recovered without its run sidecar only when
    the complete before-image proves that immutable source/coordinate metadata is still
    identical to the current TM row. Missing before rows or any drift fail closed.
    """
    if before is None or current is None:
        return None
    fields = (
        "project_id",
        "adapter_id",
        "relative_path",
        "logical_key",
        "source_text",
        "source_fingerprint",
    )
    for field in fields:
        if before.get(field) != current.get(field):
            return None
    return {field: current.get(field) for field in fields}


def recovery_payload_for_event(
    service: ReviewService,
    event: ReviewDecisionEvent,
    identity: str,
    current: Optional[Mapping[str, Any]],
) -> tuple[Optional[Mapping[str, Any]], str]:
    try:
        return service.index(event.run_id).units.get(identity), "review_index"
    except ReviewUnavailable:
        payload = _before_anchor_payload(event.before.get(identity), current)
        if payload is not None:
            return payload, "before_image"
        return None, "missing_evidence"


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
    reverted_decisions = {
        str(item)
        for event in all_events
        if event.action == "revert"
        for item in (event.details.get("reverted_decision_ids") or [])
        if str(item)
    }
    identities = list(dict.fromkeys(target for event in events for target in event.targets))
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(identities)

    rows = []
    status_counts: Counter[str] = Counter()
    proof_counts: Counter[str] = Counter()
    for event in events:
        for identity in event.targets:
            before = event.before.get(identity)
            current = current_rows.get(identity)
            coordinate_status = _project_coordinate_status(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                reverted_decisions=reverted_decisions,
                current_row=current,
            )
            revertible = False
            conflict = ""
            proof = "not_applicable"
            if event.action in _RECOVERABLE_ACTIONS and coordinate_status == "current":
                payload, proof = recovery_payload_for_event(service, event, identity, current)
                if payload is None:
                    conflict = (
                        "历史 ReviewIndex 已不可用，且 before-image 不能证明 source/coordinate 元数据未漂移"
                    )
                else:
                    revertible, conflict = _revertibility(
                        event=event,
                        identity=identity,
                        latest_mutation=latest_mutation,
                        current_row=current,
                        payload=payload,
                    )
            elif coordinate_status == "reverted":
                conflict = "该 Review 决策已经被后续 revert 撤销"
            elif coordinate_status == "superseded":
                conflict = "该坐标在此决策后已有新的人工/TM 状态"
            elif event.action not in _RECOVERABLE_ACTIONS:
                conflict = f"{event.action} 不是可恢复的 TM 写入决策"

            row = {
                "decision_id": event.decision_id,
                "stable_identity": identity,
                "relative_path": _coordinate_value("relative_path", None, before, current),
                "logical_key": _coordinate_value("logical_key", None, before, current),
                "source_text": _coordinate_value("source_text", None, before, current),
                "source_fingerprint": _coordinate_value(
                    "source_fingerprint", None, before, current
                ),
                "before_translation": None if before is None else before.get("translation"),
                "after_translation": event.translation,
                "current_translation": None if current is None else current.get("translation"),
                "current_origin": None if current is None else current.get("origin"),
                "current_review_state": None if current is None else current.get("review_state"),
                "status": coordinate_status,
                "revertible": revertible,
                "recovery_proof": proof,
                "conflict_reason": conflict,
            }
            status_counts[coordinate_status] += 1
            proof_counts[proof] += 1
            if status != "all" and coordinate_status != status:
                continue
            if recovery == "revertible" and not revertible:
                continue
            if recovery == "blocked" and revertible:
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
        "log_revision": service._log().revision(),
    }


def validate_historical_revert_selection(
    service: ReviewService,
    run_id: str,
    decision_ids: Sequence[str],
) -> Dict[str, Mapping[str, Any]]:
    """Build strong payload evidence for selected old decisions."""
    wanted = set(str(item) for item in decision_ids if str(item))
    events = [event for event in service._log().read_all() if event.decision_id in wanted]
    if len(events) != len(wanted):
        raise ValueError("unknown decision id in historical recovery selection")
    if any(event.run_id != run_id for event in events):
        raise ValueError("historical recovery selection spans multiple runs")
    identities = [event.targets[0] for event in events if len(event.targets) == 1]
    if len(identities) != len(events):
        raise ValueError("historical recovery requires one coordinate per decision")
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(identities)
    payloads: Dict[str, Mapping[str, Any]] = {}
    for event, identity in zip(events, identities):
        payload, _proof = recovery_payload_for_event(
            service, event, identity, current_rows.get(identity)
        )
        if payload is None:
            raise ValueError(
                f"{identity}: historical recovery evidence unavailable; refusing unsafe fallback"
            )
        payloads[identity] = payload
    return payloads
