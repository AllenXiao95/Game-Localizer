from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.storage.glossary import GlossaryTerm
from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
)
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.rules.placeholder import PlaceholderRule
from localizer.rules.validation import ValidationRule


class TMSeedImportRefused(RuntimeError):
    """The seed cannot be safely interpreted or promoted to reviewed TM rows."""


@dataclass(frozen=True)
class TMSeedIssue:
    stable_identity: str
    relative_path: str
    logical_key: str
    code: str
    message: str


@dataclass(frozen=True)
class TMSeedReport:
    source_files: Tuple[str, ...]
    total: int
    accepted: int
    rejected: int
    issue_counts: Mapping[str, int]
    issues: Tuple[TMSeedIssue, ...]
    applied: bool = False
    accepted_by: str = ""
    backup: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "source_files": list(self.source_files),
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "issue_counts": dict(self.issue_counts),
            "issues": [asdict(issue) for issue in self.issues],
            "applied": self.applied,
            "accepted_by": self.accepted_by,
            "backup": self.backup,
        }


class TMSeedLoader:
    """Load the neutral, adapter-coordinate-preserving TM Seed JSON format."""

    TOP_LEVEL_FIELDS = {"schema_version", "defaults", "entries"}
    DEFAULT_FIELDS = {"adapter_id", "relative_path"}
    ENTRY_FIELDS = {
        "adapter_id",
        "relative_path",
        "logical_key",
        "source_text",
        "translation",
        "context",
    }

    def __init__(
        self,
        *,
        project_id: str,
        source_locale: str,
        target_locale: str,
        adapter_ids: Iterable[str],
    ) -> None:
        self.project_id = project_id
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.adapter_ids = frozenset(str(value) for value in adapter_ids)

    def load_many(self, paths: Sequence[Path]) -> Tuple[TranslationUnit, ...]:
        units: List[TranslationUnit] = []
        for path in paths:
            units.extend(self.load(path))
        return tuple(units)

    def load(self, path: Path) -> Tuple[TranslationUnit, ...]:
        source = Path(path).resolve(strict=True)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TMSeedImportRefused(f"cannot read TM Seed {source}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise TMSeedImportRefused(f"TM Seed {source} root must be an object")
        self._reject_unknown(raw, self.TOP_LEVEL_FIELDS, f"TM Seed {source}")
        if raw.get("schema_version") != 1:
            raise TMSeedImportRefused(
                f"TM Seed {source} requires schema_version 1"
            )
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, Mapping):
            raise TMSeedImportRefused(f"TM Seed {source} defaults must be an object")
        self._reject_unknown(defaults, self.DEFAULT_FIELDS, f"TM Seed {source} defaults")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise TMSeedImportRefused(f"TM Seed {source} entries must be an array")
        units = []
        for index, item in enumerate(entries):
            label = f"TM Seed {source} entry {index}"
            if not isinstance(item, Mapping):
                raise TMSeedImportRefused(f"{label} must be an object")
            self._reject_unknown(item, self.ENTRY_FIELDS, label)
            merged = {**defaults, **item}
            required = (
                "adapter_id",
                "relative_path",
                "logical_key",
                "source_text",
                "translation",
            )
            missing = [name for name in required if name not in merged]
            if missing:
                raise TMSeedImportRefused(f"{label} missing: {', '.join(missing)}")
            adapter_id = self._text(merged["adapter_id"], "adapter_id", label)
            if adapter_id not in self.adapter_ids:
                raise TMSeedImportRefused(
                    f"{label} adapter_id {adapter_id!r} is not configured in project.yaml; "
                    f"available: {sorted(self.adapter_ids)}"
                )
            relative_path = self._relative_path(merged["relative_path"], label)
            logical_key = self._text(merged["logical_key"], "logical_key", label)
            source_text = self._text(merged["source_text"], "source_text", label)
            translation = merged["translation"]
            if not isinstance(translation, str):
                raise TMSeedImportRefused(f"{label} translation must be a string")
            context = merged.get("context")
            if context is not None and not isinstance(context, str):
                raise TMSeedImportRefused(f"{label} context must be a string or null")
            units.append(
                TranslationUnit(
                    project_id=self.project_id,
                    adapter_id=adapter_id,
                    relative_path=relative_path,
                    logical_file=relative_path,
                    logical_key=logical_key,
                    source_text=source_text,
                    translation=translation,
                    context=context,
                    source_locale=self.source_locale,
                    target_locale=self.target_locale,
                    metadata={"tm_seed": str(source), "tm_seed_index": index},
                )
            )
        return tuple(units)

    @staticmethod
    def _reject_unknown(value: Mapping, allowed: set, label: str) -> None:
        unknown = sorted(str(key) for key in set(value) - allowed)
        if unknown:
            raise TMSeedImportRefused(f"{label} unknown fields: {', '.join(unknown)}")

    @staticmethod
    def _text(value: object, field: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TMSeedImportRefused(f"{label} {field} must be a non-empty string")
        return value

    @staticmethod
    def _relative_path(value: object, label: str) -> str:
        candidate = TMSeedLoader._text(value, "relative_path", label).replace("\\", "/")
        path = PurePosixPath(candidate)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise TMSeedImportRefused(
                f"{label} relative_path must be a safe source-relative path"
            )
        return path.as_posix()


class TMSeedImporter:
    """Validate existing translations and optionally attest them as reviewed TM rows."""

    def __init__(
        self,
        tm_path: Path,
        *,
        validation_rule: ValidationRule,
        glossary_terms: Sequence[GlossaryTerm] = (),
    ) -> None:
        self.tm_path = Path(tm_path).resolve()
        self.validation_rule = validation_rule
        self.glossary_terms = tuple(glossary_terms)

    def analyze(
        self,
        units: Sequence[TranslationUnit],
        *,
        source_files: Sequence[Path] = (),
    ) -> Tuple[TMSeedReport, Tuple[TMEntry, ...]]:
        issues: List[TMSeedIssue] = []
        entries: List[TMEntry] = []
        seen = set()
        rejected_units = 0
        for unit in units:
            unit_issues, normalized = self._validate(unit)
            if unit.stable_identity in seen:
                unit_issues.append(("duplicate_coordinate", "duplicate adapter/path/key coordinate"))
            seen.add(unit.stable_identity)
            if unit_issues:
                rejected_units += 1
                issues.extend(
                    TMSeedIssue(
                        unit.stable_identity,
                        unit.relative_path,
                        unit.logical_key,
                        code,
                        message,
                    )
                    for code, message in unit_issues
                )
                continue
            entries.append(
                TMEntry(
                    stable_identity=unit.stable_identity,
                    project_id=unit.project_id,
                    adapter_id=unit.adapter_id,
                    relative_path=unit.relative_path,
                    logical_key=unit.logical_key,
                    source_text=unit.source_text,
                    source_fingerprint=unit.source_fingerprint,
                    translation=normalized,
                    origin=HUMAN_REVIEW_FIELDS["origin"],
                    review_state=HUMAN_REVIEW_FIELDS["review_state"],
                    match_scope=HUMAN_REVIEW_FIELDS["match_scope"],
                    classification=HUMAN_REVIEW_FIELDS["classification"],
                    quality_state=HUMAN_REVIEW_FIELDS["quality_state"],
                    is_formal=HUMAN_REVIEW_FIELDS["is_formal"],
                    human_authored=HUMAN_REVIEW_FIELDS["human_authored"],
                )
            )
        counts = Counter(issue.code for issue in issues)
        report = TMSeedReport(
            tuple(str(Path(path).resolve()) for path in source_files),
            len(units),
            len(entries),
            rejected_units,
            dict(sorted(counts.items())),
            tuple(issues),
        )
        return report, tuple(entries)

    def apply(
        self,
        units: Sequence[TranslationUnit],
        *,
        accepted_by: str,
        source_files: Sequence[Path] = (),
        backup_path: Optional[Path] = None,
        report_path: Optional[Path] = None,
    ) -> TMSeedReport:
        actor = accepted_by.strip()
        if not actor:
            raise TMSeedImportRefused("--accepted-by is required with --apply")
        report, entries = self.analyze(units, source_files=source_files)
        if not report.total:
            raise TMSeedImportRefused("refusing an empty TM Seed/resource import")
        if report.rejected:
            raise TMSeedImportRefused(
                f"refusing TM Seed import: {report.rejected} coordinates failed validation; "
                "fix the resources/seed or rules, then run the dry-run again"
            )
        backup = self._backup(backup_path) if self.tm_path.is_file() else None
        with SQLiteTranslationMemory(self.tm_path) as tm:
            result = tm.apply_human_review(entries, reject_guarded=True)
        if len(result.written) != len(entries):
            raise TMSeedImportRefused(
                f"TM write count mismatch: wrote {len(result.written)}/{len(entries)}"
            )
        applied = TMSeedReport(
            report.source_files,
            report.total,
            report.accepted,
            report.rejected,
            report.issue_counts,
            report.issues,
            True,
            actor,
            str(backup) if backup else None,
        )
        if report_path is not None:
            AtomicIO.write_json(Path(report_path), applied.as_dict())
        return applied

    def _validate(self, unit: TranslationUnit) -> Tuple[List[Tuple[str, str]], str]:
        issues: List[Tuple[str, str]] = []
        translation = unit.translation or ""
        if not translation.strip():
            return [("empty_translation", "translation is empty")], translation
        placeholder_rule = PlaceholderRule.for_adapter(unit.adapter_id)
        if Counter(placeholder_rule.extract(unit.source_text)) != Counter(
            placeholder_rule.extract(translation)
        ):
            issues.append(("placeholder_mismatch", "source and translation placeholders differ"))
        summary = self.validation_rule.validate_text(
            translation,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
        )
        issues.extend(
            (issue.code, issue.message)
            for issue in summary.issues
            if issue.severity == "error"
        )
        normalized = summary.text
        if (
            normalized.strip() == unit.source_text.strip()
            and placeholder_rule.is_translatable(unit.source_text)
        ):
            issues.append(("untranslated", "translation is identical to source text"))
        for term in self.glossary_terms:
            if term.is_violated_by(
                unit.source_text,
                normalized,
                relative_path=unit.relative_path,
            ):
                issues.append(
                    ("glossary_violation", f"required term is missing: {term.source!r}")
                )
        return issues, normalized

    def _backup(self, destination: Optional[Path]) -> Path:
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = self.tm_path.parent / "backups" / f"tm-before-seed-{stamp}.sqlite3"
        target = Path(destination).resolve()
        if target.exists():
            raise TMSeedImportRefused(f"backup already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(self.tm_path))
        backup = sqlite3.connect(str(target))
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()
        return target


def write_seed_report(path: Path, report: TMSeedReport) -> None:
    AtomicIO.write_json(Path(path), report.as_dict())
