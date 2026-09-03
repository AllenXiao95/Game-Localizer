"""Review history inspection and one canonical selective-recovery implementation.

The append-only ReviewDecisionLog is the operator-mutation authority; SQLite TM is
its current projection.  New events carry complete ``before`` + ``after`` images.
Older events remain recoverable through a narrow compatibility evidence chain:

    after-image -> immutable ReviewIndex -> matching before-image anchor

All actual restores flow through :func:`safe_revert`.  Project-history query code
never owns a second mutation path.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMGuardError,
)
from localizer.application.review_log import (
    ACTIONS,
    LogRevisionMismatch,
    ReviewDecisionEvent,
)

from .review import ReviewConflict, ReviewService, ReviewUnavailable

_TM_MUTATION_ACTIONS = {"commit", "unify", "accept_debt", "retire", "revert"}
_RECOVERABLE_ACTIONS = {"commit", "unify", "accept_debt"}
_RECOVERY_ACTION_FILTERS = _RECOVERABLE_ACTIONS | {"all"}
_PROJECT_HISTORY_STATUSES = {
    "all",
    "current",
    "superseded",
    "reverted",
    "mixed",
    "recorded",
}
MAX_RECOVERY_ITEMS = 500
# Small operations stay inline for backwards compatibility and run-level UX.  Large
# audit summaries never serialize thousands of coordinates; the detail endpoint owns
# paging/search for those.
INLINE_PROJECT_COORDINATES = 200

# created/updated timestamps are storage bookkeeping.  Everything else is semantic
# or provenance state and must still equal the recorded after-image for a safe revert.
_AFTER_IGNORE_FIELDS = {"created_at", "updated_at"}


def _mutation_guard(service: ReviewService):
    lock = getattr(service, "mutation_lock", None)
    return lock if lock is not None else nullcontext()


def _latest_mutation_by_target(
    events: Sequence[ReviewDecisionEvent],
) -> Dict[str, str]:
    latest: Dict[str, str] = {}
    for event in events:
        if event.action not in _TM_MUTATION_ACTIONS:
            continue
        for target in event.targets:
            latest[target] = event.decision_id
    return latest


def _coordinate_value(
    name: str,
    payload: Optional[Mapping[str, Any]],
    before: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]],
    default: str = "",
) -> str:
    for source in (payload or {}, before or {}, current or {}):
        raw = source.get(name)
        if raw is not None and raw != "":
            return str(raw)
    return default


def _rows_equal_after(
    current: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
) -> bool:
    if current is None or after is None:
        return current is None and after is None
    keys = (set(current) | set(after)) - _AFTER_IGNORE_FIELDS
    return all(current.get(key) == after.get(key) for key in keys)


def _current_matches_written_state(
    row: Optional[Mapping[str, Any]],
    event: ReviewDecisionEvent,
    identity: str,
    payload: Optional[Mapping[str, Any]],
) -> bool:
    """Whether TM still equals the state written by ``event``.

    New decisions are trivial: compare directly with their authoritative after-image.
    Legacy decisions reconstruct the post-state from translation + fixed human Review
    authority fields + immutable source-coordinate evidence.
    """
    if identity in event.after:
        return _rows_equal_after(row, event.after.get(identity))
    if row is None or event.translation is None:
        return False
    if row.get("translation") != event.translation:
        return False
    for field, expected in HUMAN_REVIEW_FIELDS.items():
        actual = row.get(field)
        if isinstance(expected, bool):
            actual = bool(actual)
        if actual != expected:
            return False
    if payload is not None:
        fingerprint = payload.get("source_fingerprint")
        if fingerprint and row.get("source_fingerprint") != fingerprint:
            return False
        for field in (
            "project_id",
            "adapter_id",
            "relative_path",
            "logical_key",
            "source_text",
        ):
            value = payload.get(field)
            if value not in (None, "") and row.get(field) != value:
                return False
    return True


def _revertibility(
    *,
    event: ReviewDecisionEvent,
    identity: str,
    latest_mutation: Mapping[str, str],
    current_row: Optional[Mapping[str, Any]],
    payload: Optional[Mapping[str, Any]],
) -> tuple[bool, str]:
    if event.action not in _RECOVERABLE_ACTIONS:
        return False, f"{event.action} 不是可恢复的 TM 写入决策"
    if latest_mutation.get(identity) != event.decision_id:
        return False, "该坐标在此决策后已有新的人工/TM 决策"
    if not _current_matches_written_state(current_row, event, identity, payload):
        return False, "当前 TM 已不再等于该决策写入后的状态"
    return True, ""


def _before_anchor_payload(
    before: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Legacy fallback proof when an event has neither after-image nor ReviewIndex."""
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


def _run_units(
    service: ReviewService,
    run_id: str,
) -> Optional[Mapping[str, Mapping[str, Any]]]:
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
    """Resolve deterministic freshness evidence for one historical decision."""
    if identity in event.after:
        after = event.after.get(identity)
        if after is not None:
            return after, "after_image"
    if run_units is not None:
        payload = run_units.get(identity)
        if payload is not None:
            return payload, "review_index"
    payload = _before_anchor_payload(event.before.get(identity), current)
    if payload is not None:
        return payload, "before_image"
    return None, "missing_evidence"


def _reverted_decision_ids(events: Sequence[ReviewDecisionEvent]) -> set[str]:
    return {
        str(item)
        for event in events
        if event.action == "revert"
        for item in (event.details.get("reverted_decision_ids") or [])
        if str(item)
    }


def _project_coordinate_status(
    *,
    event: ReviewDecisionEvent,
    identity: str,
    latest_mutation: Mapping[str, str],
    reverted_decisions: set[str],
    current_row: Optional[Mapping[str, Any]] = None,
) -> str:
    """Project-history status derived from the authoritative Review event stream.

    ``current_row`` is intentionally not required.  Summary pages classify by Review
    authority only; the coordinate-detail endpoint performs the stronger TM freshness
    proof before enabling a destructive checkbox.
    """
    if event.action not in _RECOVERABLE_ACTIONS:
        return "recorded"
    if event.decision_id in reverted_decisions:
        return "reverted"
    if latest_mutation.get(identity) != event.decision_id:
        return "superseded"
    return "current"


def _operation_status(action: str, statuses: Sequence[str]) -> str:
    if action not in _RECOVERABLE_ACTIONS:
        return "recorded"
    unique = set(statuses)
    if not unique:
        return "recorded"
    if len(unique) == 1:
        return next(iter(unique))
    return "mixed"


def _event_search_text(event: ReviewDecisionEvent) -> str:
    values: List[str] = [
        event.run_id,
        event.action,
        event.decision_id,
        event.translation or "",
        event.reason,
        str(event.details),
    ]
    values.extend(event.targets)
    for mapping in (event.before, event.after):
        for row in mapping.values():
            if isinstance(row, Mapping):
                values.extend(
                    str(row.get(field) or "")
                    for field in (
                        "relative_path",
                        "logical_key",
                        "source_text",
                        "translation",
                    )
                )
    return "\n".join(values).casefold()


def _coordinate_row(
    *,
    event: ReviewDecisionEvent,
    identity: str,
    status: str,
    current: Optional[Mapping[str, Any]],
    latest_mutation: Mapping[str, str],
    run_units: Optional[Mapping[str, Mapping[str, Any]]],
) -> Dict[str, Any]:
    before = event.before.get(identity)
    payload, proof = recovery_payload_for_event(event, identity, current, run_units)
    revertible = False
    conflict = ""
    if event.action in _RECOVERABLE_ACTIONS and status == "current":
        if payload is None:
            conflict = (
                "没有 after-image；历史 ReviewIndex 也不可用，且 before-image "
                "不能证明 source/coordinate 元数据未漂移"
            )
        else:
            revertible, conflict = _revertibility(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                current_row=current,
                payload=payload,
            )
    elif status == "reverted":
        conflict = "该 Review 决策已经被后续 revert 撤销"
    elif status == "superseded":
        conflict = "该坐标在此决策后已有新的人工/TM 状态"
    elif event.action not in _RECOVERABLE_ACTIONS:
        conflict = f"{event.action} 不是可恢复的 TM 写入决策"

    return {
        "decision_id": event.decision_id,
        "stable_identity": identity,
        "relative_path": _coordinate_value("relative_path", payload, before, current),
        "logical_key": _coordinate_value("logical_key", payload, before, current),
        "source_text": _coordinate_value("source_text", payload, before, current),
        "source_fingerprint": _coordinate_value(
            "source_fingerprint", payload, before, current
        ),
        "before_translation": None if before is None else before.get("translation"),
        "after_translation": (
            (event.after.get(identity) or {}).get("translation")
            if identity in event.after
            else event.translation
        ),
        "current_translation": None if current is None else current.get("translation"),
        "current_origin": None if current is None else current.get("origin"),
        "current_review_state": None if current is None else current.get("review_state"),
        "status": status,
        "revertible": revertible,
        "recovery_proof": proof,
        "conflict_reason": conflict,
    }


def recovery_operations(
    service: ReviewService,
    run_id: str,
    *,
    action: str = "unify",
    limit: int = 100,
) -> Dict[str, Any]:
    """Run-scoped operator mutations grouped by audit id."""
    if action not in _RECOVERY_ACTION_FILTERS:
        raise ValueError(
            f"unsupported recovery action filter {action!r}; "
            f"expected one of {sorted(_RECOVERY_ACTION_FILTERS)}"
        )
    index = service.index(run_id)
    log = service._log()
    all_events = log.read_all()
    latest_mutation = _latest_mutation_by_target(all_events)
    reverted = _reverted_decision_ids(all_events)
    candidates = [
        event
        for event in all_events
        if event.run_id == run_id
        and event.action in _RECOVERABLE_ACTIONS
        and (action == "all" or event.action == action)
    ]
    identities = [target for event in candidates for target in event.targets]
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(identities)

    grouped: "OrderedDict[str, List[ReviewDecisionEvent]]" = OrderedDict()
    for event in candidates:
        audit_id = str(event.details.get("audit_id") or event.decision_id)
        grouped.setdefault(audit_id, []).append(event)

    operations: List[Dict[str, Any]] = []
    for audit_id, events in grouped.items():
        coordinates = []
        for event in events:
            for identity in event.targets:
                status = _project_coordinate_status(
                    event=event,
                    identity=identity,
                    latest_mutation=latest_mutation,
                    reverted_decisions=reverted,
                )
                coordinates.append(
                    _coordinate_row(
                        event=event,
                        identity=identity,
                        status=status,
                        current=current_rows.get(identity),
                        latest_mutation=latest_mutation,
                        run_units=index.units,
                    )
                )
        safe_count = sum(1 for item in coordinates if item["revertible"])
        first = events[0]
        translations = {event.translation for event in events}
        operations.append(
            {
                "audit_id": audit_id,
                "action": first.action,
                "decided_at": events[-1].decided_at,
                "reason": first.reason,
                "actor": dict(first.actor),
                "translation": next(iter(translations)) if len(translations) == 1 else None,
                "coordinate_count": len(coordinates),
                "revertible_count": safe_count,
                "conflict_count": len(coordinates) - safe_count,
                "coordinates": coordinates,
            }
        )

    operations.reverse()
    bounded = operations[: max(1, min(int(limit), 200))]
    return {
        "available": True,
        "run_id": run_id,
        "action": action,
        "total": len(operations),
        "operations": bounded,
        "log_revision": log.revision(),
    }


def project_change_history(
    service: ReviewService,
    *,
    action: str = "all",
    status: str = "all",
    run_id: str = "",
    query: str = "",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Project-level Review history with truly bounded large-audit responses.

    Large operations return summary only.  Coordinate detail/search/recovery belongs to
    ``GET /api/review/history/coordinates``.  Small operations remain inline to avoid
    breaking existing callers and tests while the UI migrates to the detail endpoint.
    """
    if action != "all" and action not in ACTIONS:
        raise ValueError(
            f"unsupported project history action {action!r}; "
            f"expected 'all' or one of {sorted(ACTIONS)}"
        )
    if status not in _PROJECT_HISTORY_STATUSES:
        raise ValueError(
            f"unsupported project history status {status!r}; "
            f"expected one of {sorted(_PROJECT_HISTORY_STATUSES)}"
        )
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    wanted_run = str(run_id or "").strip()
    needle = str(query or "").strip().casefold()

    log = service._log()
    all_events = log.read_all()
    latest_mutation = _latest_mutation_by_target(all_events)
    reverted = _reverted_decision_ids(all_events)
    candidates = [
        event
        for event in all_events
        if (action == "all" or event.action == action)
        and (not wanted_run or event.run_id == wanted_run)
    ]

    grouped: "OrderedDict[tuple[str, str, str], List[ReviewDecisionEvent]]" = OrderedDict()
    for event in candidates:
        audit_id = str(event.details.get("audit_id") or event.decision_id)
        grouped.setdefault((event.run_id, event.action, audit_id), []).append(event)

    operations: List[Dict[str, Any]] = []
    for (event_run_id, event_action, audit_id), events in grouped.items():
        if needle and not any(needle in _event_search_text(event) for event in events):
            continue
        coordinate_statuses = [
            _project_coordinate_status(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                reverted_decisions=reverted,
            )
            for event in events
            for identity in event.targets
        ]
        op_status = _operation_status(event_action, coordinate_statuses)
        if status != "all" and op_status != status:
            continue

        first = events[0]
        translations = {event.translation for event in events}
        coordinate_count = len(coordinate_statuses)
        operation: Dict[str, Any] = {
            "audit_id": audit_id,
            "run_id": event_run_id,
            "action": event_action,
            "decided_at": events[-1].decided_at,
            "reason": first.reason,
            "actor": dict(first.actor),
            "translation": next(iter(translations)) if len(translations) == 1 else None,
            "details": dict(first.details),
            "decision_ids": [event.decision_id for event in events],
            "coordinate_count": coordinate_count,
            "current_count": sum(1 for value in coordinate_statuses if value == "current"),
            "superseded_count": sum(
                1 for value in coordinate_statuses if value == "superseded"
            ),
            "reverted_count": sum(1 for value in coordinate_statuses if value == "reverted"),
            "status": op_status,
            "coordinates_inline": coordinate_count <= INLINE_PROJECT_COORDINATES,
            "coordinates": [],
            "revertible_count": None,
            "conflict_count": None,
        }

        if operation["coordinates_inline"] and coordinate_count:
            identities = list(
                dict.fromkeys(target for event in events for target in event.targets)
            )
            with SQLiteTranslationMemory(service.config.tm.database) as tm:
                current_rows = tm.rows_for(identities)
            run_units = _run_units(service, event_run_id)
            rows = []
            for event in events:
                for identity in event.targets:
                    coord_status = _project_coordinate_status(
                        event=event,
                        identity=identity,
                        latest_mutation=latest_mutation,
                        reverted_decisions=reverted,
                    )
                    rows.append(
                        _coordinate_row(
                            event=event,
                            identity=identity,
                            status=coord_status,
                            current=current_rows.get(identity),
                            latest_mutation=latest_mutation,
                            run_units=run_units,
                        )
                    )
            operation["coordinates"] = rows
            operation["revertible_count"] = sum(
                1 for item in rows if item["revertible"]
            )
            operation["conflict_count"] = sum(
                1
                for item in rows
                if item["status"] == "current" and not item["revertible"]
            )
        operations.append(operation)

    operations.reverse()
    total = len(operations)
    bounded = operations[offset : offset + limit]
    run_ids = list(
        dict.fromkeys(
            event.run_id for event in reversed(all_events) if str(event.run_id).strip()
        )
    )
    return {
        "available": True,
        "scope": "project_review_history",
        "action": action,
        "status": status,
        "run_id": wanted_run,
        "query": query,
        "total": total,
        "offset": offset,
        "limit": limit,
        "operations": bounded,
        "run_ids": run_ids,
        "log_revision": log.revision(),
        "inline_coordinate_limit": INLINE_PROJECT_COORDINATES,
        "note": (
            "仅覆盖 append-only ReviewDecisionLog 中的人工审查/操作决策；"
            "大 audit 这里只返回摘要，coordinate 通过分页 detail endpoint 查询；"
            "不代表 Provider、Planner 或 TM 生命周期的完整事件流。"
        ),
    }


def safe_revert(
    service: ReviewService,
    run_id: str,
    decision_ids: Sequence[str],
    *,
    reason: str,
    expected_log_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore selected before-images with one evidence resolver and one write path."""
    with _mutation_guard(service):
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
                "recovery decisions must belong to the selected run: "
                + ", ".join(foreign)
            )
        unsupported = [
            event.decision_id
            for event in events
            if event.action not in _RECOVERABLE_ACTIONS
        ]
        if unsupported:
            raise ValueError(
                "selected decisions are not recoverable TM writes: "
                + ", ".join(unsupported)
            )

        targets: List[str] = []
        event_by_target: Dict[str, ReviewDecisionEvent] = {}
        for event in events:
            if len(event.targets) != 1:
                raise ValueError(
                    f"decision {event.decision_id} does not have a single coordinate target"
                )
            identity = event.targets[0]
            if identity in event_by_target:
                raise ValueError(
                    f"multiple selected decisions target {identity}; "
                    "recover one state transition at a time"
                )
            event_by_target[identity] = event
            targets.append(identity)

        latest_mutation = _latest_mutation_by_target(all_events)
        run_units = _run_units(service, run_id)
        proofs: List[str] = []
        with SQLiteTranslationMemory(service.config.tm.database) as tm:
            current_rows = tm.rows_for(targets)
            conflicts = []
            snapshots = []
            for identity in targets:
                event = event_by_target[identity]
                payload, proof = recovery_payload_for_event(
                    event, identity, current_rows.get(identity), run_units
                )
                proofs.append(proof)
                if payload is None:
                    conflicts.append(
                        (
                            identity,
                            "没有可证明 freshness 的 after/ReviewIndex/before-image 证据",
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
                    "选择中存在证据不足或已过期的撤销决策，整批未修改："
                    + preview
                    + more
                )
            try:
                restored = tm.restore_rows(snapshots)
                restored_rows = tm.rows_for(targets)
            except TMGuardError as exc:
                raise ReviewConflict(str(exc)) from exc

        recovery_mode = (
            "selective_coordinate"
            if all(proof in {"after_image", "review_index"} for proof in proofs)
            else "selective_coordinate_historical_before_image"
        )
        revert_event = ReviewDecisionEvent(
            action="revert",
            run_id=run_id,
            targets=tuple(targets),
            reason=reason,
            before={identity: current_rows.get(identity) for identity in targets},
            after={identity: restored_rows.get(identity) for identity in targets},
            details={
                "reverted_decision_ids": wanted,
                "recovery_mode": recovery_mode,
                "recovery_proofs": dict(zip(targets, proofs)),
            },
            actor=service._actor(),
        )
        try:
            revision = log.append([revert_event], expected_revision=revision)
        except Exception:
            # The authoritative log did not accept the revert.  Re-apply the exact
            # pre-revert rows so TM does not claim a state absent from the log.
            try:
                with SQLiteTranslationMemory(service.config.tm.database) as tm:
                    tm.restore_rows(
                        [
                            {"stable_identity": identity, "row": current_rows.get(identity)}
                            for identity in targets
                        ]
                    )
            except Exception as compensation_exc:
                raise ReviewConflict(
                    "revert 日志写入失败，且 TM 补偿也失败；停止继续写入并人工核对："
                    + str(compensation_exc)
                ) from compensation_exc
            raise

        ledger = service.ledger(run_id)
        for identity in targets:
            ledger.mark(identity, "reverted", decision_id=revert_event.decision_id)
        service._ledger_path(run_id).parent.mkdir(parents=True, exist_ok=True)
        ledger.save()
        unique_proofs = set(proofs)
        return {
            "complete": True,
            "restored": restored,
            "targets": targets,
            "reverted_decision_ids": wanted,
            "decision_id": revert_event.decision_id,
            "recovery_proof": (
                next(iter(unique_proofs)) if len(unique_proofs) == 1 else "mixed"
            ),
            "log_revision": revision,
        }
