"""Review history inspection and safe selective recovery.

This module intentionally stays in the Review/application edge.  The append-only
Review decision log is the authority for operator mutations; SQLite TM is only the
current projection.  Recovery therefore never invents a second history store and
never rewrites immutable run/QA sidecars.
"""
from __future__ import annotations

import json
from collections import OrderedDict
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

# These actions mutate the TM projection for a coordinate.  Draft/skip/defer and
# glossary decisions do not.  A later mutation makes an older decision stale even
# when it happens to write the same translation text again: the newer human
# decision is still the authority that must not be overwritten by an old revert.
_TM_MUTATION_ACTIONS = {"commit", "unify", "accept_debt", "retire", "revert"}
_RECOVERABLE_ACTIONS = {"commit", "unify", "accept_debt"}
_RECOVERY_ACTION_FILTERS = _RECOVERABLE_ACTIONS | {"all"}
_PROJECT_HISTORY_STATUSES = {"all", "current", "superseded", "reverted", "mixed", "recorded"}
MAX_RECOVERY_ITEMS = 500


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


def _current_matches_written_state(
    row: Optional[Mapping[str, Any]],
    event: ReviewDecisionEvent,
    payload: Optional[Mapping[str, Any]],
) -> bool:
    """Whether TM still represents the state written by ``event``.

    The decision log stores the complete *before* image and the committed
    translation, while Review commits have a deliberately fixed authority shape
    (`HUMAN_REVIEW_FIELDS`).  The run's immutable ReviewIndex supplies the source
    fingerprint written by that decision.  Together those are enough for a
    deterministic freshness check without adding an after-image/provenance store.
    """
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
        for field in ("adapter_id", "relative_path", "logical_key"):
            value = payload.get(field)
            if value and row.get(field) != value:
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
    if not _current_matches_written_state(current_row, event, payload):
        return False, "当前 TM 已不再等于该决策写入后的状态"
    return True, ""


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


def recovery_operations(
    service: ReviewService,
    run_id: str,
    *,
    action: str = "unify",
    limit: int = 100,
) -> Dict[str, Any]:
    """Return operator-readable Review mutations grouped by audit id.

    ``unify_majorities`` intentionally writes one decision event per coordinate but
    shares one ``audit_id``.  Grouping by that id reconstructs the operator action
    while keeping coordinate-level selection possible.
    """
    if action not in _RECOVERY_ACTION_FILTERS:
        raise ValueError(
            f"unsupported recovery action filter {action!r}; "
            f"expected one of {sorted(_RECOVERY_ACTION_FILTERS)}"
        )
    index = service.index(run_id)
    log = service._log()  # ReviewDecisionLog is the existing audit authority.
    all_events = log.read_all()
    latest_mutation = _latest_mutation_by_target(all_events)
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
                payload = index.units.get(identity)
                before = event.before.get(identity)
                current = current_rows.get(identity)
                revertible, conflict = _revertibility(
                    event=event,
                    identity=identity,
                    latest_mutation=latest_mutation,
                    current_row=current,
                    payload=payload,
                )
                coordinates.append(
                    {
                        "decision_id": event.decision_id,
                        "stable_identity": identity,
                        "relative_path": _coordinate_value(
                            "relative_path", payload, before, current
                        ),
                        "logical_key": _coordinate_value(
                            "logical_key", payload, before, current
                        ),
                        "source_text": _coordinate_value(
                            "source_text", payload, before, current
                        ),
                        "source_fingerprint": _coordinate_value(
                            "source_fingerprint", payload, before, current
                        ),
                        "before_translation": (
                            None if before is None else before.get("translation")
                        ),
                        "after_translation": event.translation,
                        "current_translation": (
                            None if current is None else current.get("translation")
                        ),
                        "current_origin": (
                            None if current is None else current.get("origin")
                        ),
                        "current_review_state": (
                            None if current is None else current.get("review_state")
                        ),
                        "revertible": revertible,
                        "conflict_reason": conflict,
                    }
                )
        safe_count = sum(1 for item in coordinates if item["revertible"])
        first = events[0]
        operations.append(
            {
                "audit_id": audit_id,
                "action": first.action,
                "decided_at": first.decided_at,
                "reason": first.reason,
                "actor": dict(first.actor),
                "translation": first.translation,
                "coordinate_count": len(coordinates),
                "revertible_count": safe_count,
                "conflict_count": len(coordinates) - safe_count,
                "coordinates": coordinates,
            }
        )

    # Most recent operator action first.  ``limit`` applies to operations, never to
    # coordinates inside one operation; silently hiding half a bulk audit would make
    # selective recovery unsafe.
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


def _project_coordinate_status(
    *,
    event: ReviewDecisionEvent,
    identity: str,
    latest_mutation: Mapping[str, str],
    reverted_decisions: set[str],
    current_row: Optional[Mapping[str, Any]],
) -> str:
    if event.action not in _RECOVERABLE_ACTIONS:
        return "recorded"
    if event.decision_id in reverted_decisions:
        return "reverted"
    if latest_mutation.get(identity) != event.decision_id:
        return "superseded"
    if not _current_matches_written_state(current_row, event, None):
        return "superseded"
    return "current"


def _operation_status(action: str, coordinates: Sequence[Mapping[str, Any]]) -> str:
    if action not in _RECOVERABLE_ACTIONS:
        return "recorded"
    statuses = {str(item.get("status") or "superseded") for item in coordinates}
    if not statuses:
        return "recorded"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed"


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
    """Return project-level Review change history across all runs.

    This is deliberately *Review history*, not a generic TM event-sourcing layer.
    Provider writes, planner lifecycle events and ``tm_source_history`` remain outside
    this view.  The existing append-only ReviewDecisionLog stays the sole authority.

    Grouping uses ``(run_id, action, audit_id)`` so one operator action can expand to
    its coordinate-level before/after/current state without accidentally combining
    unrelated runs.  Recovery still goes through :func:`safe_revert`; this query adds
    no second write path.
    """
    if action != "all" and action not in ACTIONS:
        raise ValueError(
            f"unsupported project history action {action!r}; expected 'all' or one of {sorted(ACTIONS)}"
        )
    if status not in _PROJECT_HISTORY_STATUSES:
        raise ValueError(
            f"unsupported project history status {status!r}; expected one of {sorted(_PROJECT_HISTORY_STATUSES)}"
        )
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    wanted_run = str(run_id or "").strip()
    needle = str(query or "").strip().casefold()

    log = service._log()
    all_events = log.read_all()
    latest_mutation = _latest_mutation_by_target(all_events)
    reverted_decisions: set[str] = set()
    for event in all_events:
        if event.action == "revert":
            reverted_decisions.update(
                str(item)
                for item in (event.details.get("reverted_decision_ids") or [])
                if str(item)
            )

    candidates = [
        event
        for event in all_events
        if (action == "all" or event.action == action)
        and (not wanted_run or event.run_id == wanted_run)
    ]
    identities = list(
        dict.fromkeys(target for event in candidates for target in event.targets)
    )
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(identities)

    grouped: "OrderedDict[tuple[str, str, str], List[ReviewDecisionEvent]]" = OrderedDict()
    for event in candidates:
        audit_id = str(event.details.get("audit_id") or event.decision_id)
        grouped.setdefault((event.run_id, event.action, audit_id), []).append(event)

    operations: List[Dict[str, Any]] = []
    for (event_run_id, event_action, audit_id), events in grouped.items():
        coordinates: List[Dict[str, Any]] = []
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
                conflict = ""
                if coordinate_status == "reverted":
                    conflict = "该 Review 决策已经被后续 revert 撤销"
                elif coordinate_status == "superseded":
                    conflict = "该坐标在此决策后已有新的人工/TM 状态"
                coordinates.append(
                    {
                        "decision_id": event.decision_id,
                        "stable_identity": identity,
                        "relative_path": _coordinate_value(
                            "relative_path", None, before, current
                        ),
                        "logical_key": _coordinate_value(
                            "logical_key", None, before, current
                        ),
                        "source_text": _coordinate_value(
                            "source_text", None, before, current
                        ),
                        "source_fingerprint": _coordinate_value(
                            "source_fingerprint", None, before, current
                        ),
                        "before_translation": (
                            None if before is None else before.get("translation")
                        ),
                        "after_translation": event.translation,
                        "current_translation": (
                            None if current is None else current.get("translation")
                        ),
                        "current_origin": (
                            None if current is None else current.get("origin")
                        ),
                        "current_review_state": (
                            None if current is None else current.get("review_state")
                        ),
                        "status": coordinate_status,
                        # Filled with the stronger run-index check after paging.  A
                        # project history query must not open every historical run.
                        "revertible": False,
                        "conflict_reason": conflict,
                    }
                )

        first = events[0]
        translations = {event.translation for event in events}
        operation = {
            "audit_id": audit_id,
            "run_id": event_run_id,
            "action": event_action,
            "decided_at": events[-1].decided_at,
            "reason": first.reason,
            "actor": dict(first.actor),
            "translation": next(iter(translations)) if len(translations) == 1 else None,
            "details": dict(first.details),
            "decision_ids": [event.decision_id for event in events],
            "coordinate_count": len(coordinates),
            "current_count": sum(1 for item in coordinates if item["status"] == "current"),
            "superseded_count": sum(
                1 for item in coordinates if item["status"] == "superseded"
            ),
            "reverted_count": sum(
                1 for item in coordinates if item["status"] == "reverted"
            ),
            "revertible_count": 0,
            "conflict_count": 0,
            "coordinates": coordinates,
        }
        operation["status"] = _operation_status(event_action, coordinates)
        if status != "all" and operation["status"] != status:
            continue
        if needle:
            haystack = json.dumps(operation, ensure_ascii=False, sort_keys=True).casefold()
            if needle not in haystack:
                continue
        operations.append(operation)

    # Append order is authoritative; reverse only after grouping so latest operator
    # actions appear first without re-sorting equal timestamps by random UUIDs.
    operations.reverse()
    total = len(operations)
    bounded = operations[offset : offset + limit]

    # Strong recovery eligibility is intentionally calculated only for the visible
    # page.  This keeps a project-level audit over years of runs cheap while making
    # every checkbox on screen trustworthy.
    indexes: Dict[str, Optional[Mapping[str, Mapping[str, Any]]]] = {}
    for operation in bounded:
        if operation["action"] not in _RECOVERABLE_ACTIONS:
            continue
        op_run_id = str(operation["run_id"])
        if op_run_id not in indexes:
            try:
                indexes[op_run_id] = service.index(op_run_id).units
            except ReviewUnavailable:
                indexes[op_run_id] = None
        units = indexes[op_run_id]
        for coordinate in operation["coordinates"]:
            if coordinate["status"] != "current":
                continue
            if units is None:
                coordinate["conflict_reason"] = (
                    "对应 run 的 ReviewIndex 已不可用，项目历史仍可查看，但不能从 GUI 安全撤销"
                )
                continue
            identity = str(coordinate["stable_identity"])
            event = next(
                (
                    candidate
                    for candidate in all_events
                    if candidate.decision_id == coordinate["decision_id"]
                ),
                None,
            )
            if event is None:
                coordinate["conflict_reason"] = "找不到原始 Review 决策"
                continue
            revertible, conflict = _revertibility(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                current_row=current_rows.get(identity),
                payload=units.get(identity),
            )
            coordinate["revertible"] = revertible
            coordinate["conflict_reason"] = conflict

        operation["revertible_count"] = sum(
            1 for item in operation["coordinates"] if item["revertible"]
        )
        operation["conflict_count"] = sum(
            1
            for item in operation["coordinates"]
            if item["status"] == "current" and not item["revertible"]
        )

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
        "note": (
            "仅覆盖 append-only ReviewDecisionLog 中的人工审查/操作决策；"
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
    """Restore selected before-images only while their decisions are still current.

    The whole batch is validated before ``restore_rows`` starts.  One stale member
    rejects the request, so a mixed selection cannot partially roll back good data.
    """
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
        event.decision_id for event in events if event.action not in _RECOVERABLE_ACTIONS
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
                f"multiple selected decisions target {identity}; recover one state transition at a time"
            )
        event_by_target[identity] = event
        targets.append(identity)

    index = service.index(run_id)
    latest_mutation = _latest_mutation_by_target(all_events)
    with SQLiteTranslationMemory(service.config.tm.database) as tm:
        current_rows = tm.rows_for(targets)
        conflicts = []
        snapshots = []
        for identity in targets:
            event = event_by_target[identity]
            revertible, conflict = _revertibility(
                event=event,
                identity=identity,
                latest_mutation=latest_mutation,
                current_row=current_rows.get(identity),
                payload=index.units.get(identity),
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
                "选择中存在已过期的撤销决策，整批未修改：" + preview + more
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
            "recovery_mode": "selective_coordinate",
        },
        actor=service._actor(),
    )
    # Re-use the revision that was checked before touching TM.  Normal local usage is
    # single-operator; if the log did change, append still fails rather than silently
    # claiming a different history.  A generic cross-store transaction is out of scope.
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
    }
