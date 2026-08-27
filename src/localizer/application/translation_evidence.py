from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from localizer.infrastructure.atomic_io import AtomicIO


_METRIC_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "translation_units_total",
    "translation_files_total",
)


def _int_metric(metrics: Mapping[str, object], key: str) -> int:
    try:
        return max(0, int(metrics.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def normalize_execution_record(
    run_id: str,
    metrics: Mapping[str, object],
    *,
    translation_files: Sequence[str] = (),
) -> dict:
    """Normalize one Provider execution run into immutable release evidence."""
    record = {"run_id": str(run_id)}
    for key in _METRIC_FIELDS:
        record[key] = _int_metric(metrics, key)
    files = tuple(sorted({str(path) for path in translation_files if str(path)}))
    if files:
        record["translation_files"] = list(files)
        record["translation_files_total"] = len(files)
    return record


def merge_execution_records(records: Sequence[Mapping[str, object]]) -> Tuple[dict, ...]:
    """Deduplicate evidence by run_id while preserving first-seen lineage order."""
    merged: Dict[str, dict] = {}
    order = []
    for raw in records:
        run_id = str(raw.get("run_id") or "").strip()
        if not run_id:
            continue
        files = raw.get("translation_files")
        files = (
            tuple(str(item) for item in files)
            if isinstance(files, Sequence) and not isinstance(files, (str, bytes))
            else ()
        )
        normalized = normalize_execution_record(run_id, raw, translation_files=files)
        if run_id not in merged:
            order.append(run_id)
        merged[run_id] = normalized
    return tuple(merged[run_id] for run_id in order)


def aggregate_execution_metrics(records: Sequence[Mapping[str, object]]) -> dict:
    """Aggregate unique contributing run execution metrics, not artifact lifetime cost."""
    unique = merge_execution_records(records)
    aggregate = {key: 0 for key in _METRIC_FIELDS}
    known_files = set()
    files_are_explicit = False
    for record in unique:
        for key in (
            "requests",
            "input_tokens",
            "output_tokens",
            "translation_units_total",
        ):
            aggregate[key] += _int_metric(record, key)
        files = record.get("translation_files")
        if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
            files_are_explicit = True
            known_files.update(str(path) for path in files if str(path))
        else:
            aggregate["translation_files_total"] += _int_metric(
                record, "translation_files_total"
            )
    if files_are_explicit:
        # New evidence records keep paths, so the same resource touched in two contributing
        # runs still counts as one resource. Legacy records without paths remain best-effort.
        aggregate["translation_files_total"] += len(known_files)
    return aggregate


class TranslationEvidenceStore:
    """Small run-level sidecar for deduplicated Provider execution provenance."""

    FILENAME = "translation-evidence.json"

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = Path(runs_root).resolve()

    def _path(self, run_id: str) -> Path:
        return self.runs_root / str(run_id) / self.FILENAME

    def load(self, run_id: str) -> Tuple[dict, ...]:
        path = self._path(run_id)
        if not path.is_file():
            return ()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ()
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            return ()
        runs = payload.get("runs")
        if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
            return ()
        return merge_execution_records(
            tuple(item for item in runs if isinstance(item, Mapping))
        )

    def save(self, run_id: str, records: Sequence[Mapping[str, object]]) -> Tuple[dict, ...]:
        normalized = merge_execution_records(records)
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        AtomicIO.write_json(
            path,
            {
                "schema_version": 1,
                "run_id": str(run_id),
                "scope": "contributing_run_execution",
                "runs": list(normalized),
            },
        )
        return normalized

    def legacy_checkpoint_record(self, run_id: str) -> Optional[dict]:
        """Best-effort compatibility for runs created before the sidecar existed."""
        path = self.runs_root / str(run_id) / "checkpoint.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        metrics = payload.get("metrics")
        if not isinstance(metrics, Mapping) or _int_metric(metrics, "requests") <= 0:
            return None
        completed = metrics.get("completed_files")
        files = (
            tuple(str(item) for item in completed)
            if isinstance(completed, Sequence) and not isinstance(completed, (str, bytes))
            else ()
        )
        return normalize_execution_record(run_id, metrics, translation_files=files)

    def inherited_for_rebuild(
        self,
        *,
        parent_run_id: str,
        reuse_checkpoint_run_id: str,
        lineage: Sequence[str],
        reused_count: int,
    ) -> Tuple[dict, ...]:
        """Resolve only execution evidence that can actually contribute reused results."""
        if reused_count <= 0:
            return ()

        # New runs carry the complete deduplicated set. Prefer the logical parent, then the
        # physical checkpoint ancestor used by compatibility fallback.
        for candidate in (parent_run_id, reuse_checkpoint_run_id):
            explicit = self.load(candidate)
            if explicit:
                return explicit

        # Historical runs predate the sidecar. Walk their already-existing immutable lineage
        # and collect Provider-bearing checkpoints once each. This is deliberately best-effort;
        # it never invents per-unit cost attribution.
        candidates = []
        for candidate in (reuse_checkpoint_run_id, *lineage):
            candidate = str(candidate)
            if candidate and candidate not in candidates and not candidate.startswith("…("):
                candidates.append(candidate)
        legacy = []
        for candidate in candidates:
            record = self.legacy_checkpoint_record(candidate)
            if record is not None:
                legacy.append(record)
        return merge_execution_records(legacy)
