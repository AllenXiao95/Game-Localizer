from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Mapping, Optional, Sequence

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMGuardError
from localizer.application.review_log import LogRevisionMismatch, ReviewDecisionEvent

from .review import ReviewConflict, ReviewService, ReviewUnavailable
from .review_recovery import (
    MAX_RECOVERY_ITEMS,
    _RECOVERABLE_ACTIONS,
    _coordinate_value,
    _latest_mutation_by_target,
    _project_coordinate_status,
    _revertibility,
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


def _run_units(service: ReviewService, run_id: str) -> Optional[Mapping[str, Mapping[str, Any]]]:
    try:
        return service.index(run_id).units
    except ReviewUnavailable:
        return None


def recovery_payload_for_event(
    event: ReviewDecisionEvent,
    identity: str,
    current: Optional[Mapping[str, Any]],
    run_units: Optional[Mapping[str, Mapping[str, Any]]],
) -> tuple[Optional[Mapping[str, Any]], str]:
    if run_units is not None:
        payload = run_units.get(identity)
        if payload is not None:
            return payload, "review_index"
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
    run_units = _run_units(service, run_id)

    rows = []
    status_counts: Counter[str] = Counter()
    proof_counts: Counter[str] = Counter()
    raw_revertible_count = 0
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
                payload, proof = recovery_payload_for_event(
                    event, identity, current, run_units
                )
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
            if revertible:
                raw_revertible_count += 1
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
        "operation_revertible_total": raw_revertible_count,
        "log_revision": service._log().revision(),
        "run_index_available": run_units is not None,
    }


def safe_revert_with_history_fallback(
    service: ReviewService,
    run_id: str,
    decision_ids: Sequence[str],
    *,
    reason: str,
    expected_log_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """Use normal safe_revert, then fall back to before-image evidence for old runs.

    The fallback is deliberately narrower than the normal path: every selected event
    must have a complete before-image whose immutable source/coordinate metadata still
    matches current TM. Any missing evidence rejects the entire batch before mutation.
    """
    try:
        return safe_revert(
            service,
            run_id,
            decision_ids,
            reason=reason,
            expected_log_revision=expected_log_revision,
        )
    except ReviewUnavailable:
        pass

    service._assert_writable()
    wanted = list(dict.fromkeys(str(item) for item in decision_ids if str(item)))
    if not wanted:
        raise ValueError("revert requires at least one decision_id")
    if len(wanted) > MAX_RECOVERY_ITEMS:
        raise ValueError(
            f"selective recovery accepts at most {MAX_RECOVERY_ITEMS} decisions per request"
        )
    if not reason.strip():
        raise ValueError("selective recovery requires a non-empty reason")

    log = service._log()
    revision = log.revision()
    if expected_log_revision is not None and expected_log_revision != revision:
        raise LogRevisionMismatch(
            "review log changed since you read it "
            f"(expected {expected_log_revision}, now {revision}); reload and retry"
        )

    all_events = log.read_all()
    by_id = {event.decision_id: event for event in all_events}
    missing = [decision_id for decision_id in wanted if decision_id not in by_id]
    if missing:
        raise ValueError(f"unknown decision ids: {', '.join(sorted(missing))}")
    events = [by_id[decision_id] for decision_id in wanted]
    foreign = [event.decision_id for event in events if event.run_id != run_id]
    if foreign:
        raise ValueError(
            "recovery decisions must belong to the selected run: " + ", ".join(foreign)
        )
    unsupported = [
        event.decision_id for event in events if event.action not in _RECOVERABLE_ACTIONS
    ]
    if unsupported:
        raise ValueError(
            "selected decisions are not recoverable TM writes: " + ", ".join(unsupported)
        )

    targets = []
    event_by_target: Dict[str, ReviewDecisionEvent] = {}
    for event in events:
        if len(event.targets) != 1:
            raise ValueError(
                f"decision {event.decision_id} does not have a single coordinate target"
            )
        identity = event.targets[0]
        if identity in event_by_target:
            raise ValueError(
                f"multiple selected decisions target {identity}; recover one state transition at a time"
            )
        event_by_target[identity] = event
        targets.append(identity)

    latest_mutation = _latest_mutation_by_target(all_events)
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(targets)
        conflicts = []
        snapshots = []
        for identity in targets:
            event = event_by_target[identity]
            payload = _before_anchor_payload(
                event.before.get(identity), current_rows.get(identity)
            )
            if payload is None:
                conflicts.append(
                    (
                        identity,
                        "历史 ReviewIndex 已不存在，且 before-image 无法证明当前 source/coordinate 未漂移",
                    )
                )
            else:
                revertible, conflict = _revertibility(
                    event=event,
                    identity=identity,
                    latest_mutation=latest_mutation,
                    current_row=current_rows.get(identity),
                    payload=payload,
                )
                if not revertible:
                    conflicts.append((identity, conflict))
            snapshots.append(
                {"stable_identity": identity, "row": event.before.get(identity)}
            )
        if conflicts:
            preview = "; ".join(
                f"{identity}: {message}" for identity, message in conflicts[:5]
            )
            more = "" if len(conflicts) <= 5 else f"；另有 {len(conflicts) - 5} 条"
            raise ReviewConflict(
                "选择中存在证据不足或已过期的撤销决策，整批未修改：" + preview + more
            )
        try:
            restored = tm.restore_rows(snapshots)
        except TMGuardError as exc:
            raise ReviewConflict(str(exc)) from exc

    event = ReviewDecisionEvent(
        action="revert",
        run_id=run_id,
        targets=tuple(targets),
        reason=reason,
        details={
            "reverted_decision_ids": wanted,
            "recovery_mode": "selective_coordinate_historical_before_image",
        },
        actor=service._actor(),
    )
    revision = log.append([event], expected_revision=revision)
    ledger = service.ledger(run_id)
    for identity in targets:
        ledger.mark(identity, "reverted", decision_id=event.decision_id)
    service._ledger_path(run_id).parent.mkdir(parents=True, exist_ok=True)
    ledger.save()
    return {
        "complete": True,
        "restored": restored,
        "targets": targets,
        "reverted_decision_ids": wanted,
        "decision_id": event.decision_id,
        "log_revision": revision,
        "recovery_proof": "before_image",
    }
