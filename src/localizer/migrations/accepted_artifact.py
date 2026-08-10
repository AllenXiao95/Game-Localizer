from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Sequence

import polib

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
    TMGuardError,
)
from localizer.application.artifact import ReleaseBundle
from localizer.application.local_build import BuildMode
from localizer.application.project_runner import ProjectRunner
from localizer.config.models import ProjectConfig
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.infrastructure.workspace import validate_run_id


class ArtifactAdoptionRefused(RuntimeError):
    """The accepted artifact cannot safely seed the authoritative TM."""


class _OfflineProvider:
    def translate(self, prompt, batch):  # pragma: no cover - a successful proof never calls it
        raise RuntimeError("artifact baseline verification forbids Provider access")


class AcceptedArtifactAdopter:
    """Turn a user-approved release into reviewed, formal TM rows.

    The release is evidence, not merely a bag of translated files: its archive,
    manifest, source fingerprint and every rendered resource are verified before
    a row can be written.  Dry-run is the default at the CLI boundary.
    """

    def __init__(
        self,
        config: ProjectConfig,
        tm: SQLiteTranslationMemory,
        manifest: Path,
        *,
        resources_root: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.tm = tm
        self.manifest_path = Path(manifest).resolve(strict=True)
        self.bundle = ReleaseBundle.load(self.manifest_path)
        self.resources_root = (
            Path(resources_root).resolve(strict=True)
            if resources_root is not None
            else (self.manifest_path.parent / "resources").resolve(strict=True)
        )

    def analyze(self, *, accepted_by: str = "") -> tuple[dict, tuple[TMEntry, ...]]:
        try:
            self.bundle.verify()
            validate_run_id(self.bundle.run_id)
        except (OSError, ValueError) as exc:
            raise ArtifactAdoptionRefused(str(exc)) from exc
        if self.bundle.project_id != self.config.project.id:
            raise ArtifactAdoptionRefused(
                f"artifact belongs to {self.bundle.project_id!r}, not "
                f"{self.config.project.id!r}"
            )
        if not self.resources_root.is_dir():
            raise ArtifactAdoptionRefused(
                f"accepted artifact resources directory does not exist: {self.resources_root}"
            )

        manifest_raw = self.manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:  # ReleaseBundle normally catches this
            raise ArtifactAdoptionRefused("artifact manifest is not valid UTF-8 JSON") from exc
        descriptors = self._manifest_files(manifest)
        resources = ProjectRunner(self.config, provider=_OfflineProvider())._resources()
        source_fingerprint = self._source_fingerprint(resources)
        recorded_fingerprint = str(manifest.get("source_fingerprint") or "")
        if not recorded_fingerprint or source_fingerprint != recorded_fingerprint:
            raise ArtifactAdoptionRefused(
                "accepted artifact was built from a different source snapshot "
                f"(manifest={recorded_fingerprint or '<missing>'}, "
                f"current={source_fingerprint})"
            )

        expected_paths = {
            resource.adapter.scan(resource.source).relative_path for resource in resources
        }
        if set(descriptors) != expected_paths:
            missing = sorted(expected_paths - set(descriptors))
            extra = sorted(set(descriptors) - expected_paths)
            raise ArtifactAdoptionRefused(
                f"artifact/source resource sets differ; missing={missing[:10]}, extra={extra[:10]}"
            )
        actual_paths = {
            path.relative_to(self.resources_root).as_posix()
            for path in self.resources_root.rglob("*")
            if path.is_file()
        }
        if actual_paths != expected_paths:
            missing = sorted(expected_paths - actual_paths)
            extra = sorted(actual_paths - expected_paths)
            raise ArtifactAdoptionRefused(
                f"resources directory differs from manifest; missing={missing[:10]}, "
                f"extra={extra[:10]}"
            )

        revisions = {
            "prompt_revision": self.config.prompt.template,
            "rules_revision": self.config.rules.file,
            "glossary_revision": self.config.glossary.file,
        }
        revision_matches = {
            name: str(manifest.get(name) or "") == sha256(path.read_bytes()).hexdigest()
            for name, path in revisions.items()
        }

        entries = []
        for resource in resources:
            relative = resource.adapter.scan(resource.source).relative_path
            destination = self._safe_resource(relative)
            descriptor = descriptors[relative]
            payload = destination.read_bytes()
            if len(payload) != descriptor["size"]:
                raise ArtifactAdoptionRefused(f"artifact resource size mismatch: {relative}")
            if sha256(payload).hexdigest() != descriptor["sha256"]:
                raise ArtifactAdoptionRefused(f"artifact resource digest mismatch: {relative}")
            if resource.adapter.adapter_id != "gettext":
                raise ArtifactAdoptionRefused(
                    f"accepted-artifact adoption is not implemented for adapter "
                    f"{resource.adapter.adapter_id!r}"
                )
            translations = self._gettext_translations(
                resource.adapter, destination, resource.units
            )
            for unit in resource.units:
                translation = translations[unit.stable_identity]
                entries.append(
                    TMEntry(
                        stable_identity=unit.stable_identity,
                        project_id=unit.project_id,
                        adapter_id=unit.adapter_id,
                        relative_path=unit.relative_path,
                        logical_key=unit.logical_key,
                        source_text=unit.source_text,
                        source_fingerprint=unit.source_fingerprint,
                        translation=translation,
                        origin=HUMAN_REVIEW_FIELDS["origin"],
                        review_state=HUMAN_REVIEW_FIELDS["review_state"],
                        match_scope="coordinate_exact",
                        classification=HUMAN_REVIEW_FIELDS["classification"],
                        run_id=self.bundle.run_id,
                        model="accepted-artifact",
                        prompt_hash=str(manifest.get("prompt_revision") or ""),
                        rules_revision=str(manifest.get("rules_revision") or ""),
                        glossary_revision=str(manifest.get("glossary_revision") or ""),
                        quality_state=HUMAN_REVIEW_FIELDS["quality_state"],
                        is_formal=HUMAN_REVIEW_FIELDS["is_formal"],
                        human_authored=HUMAN_REVIEW_FIELDS["human_authored"],
                    )
                )

        entries_tuple = tuple(entries)
        existing = self.tm.rows_for([entry.stable_identity for entry in entries_tuple])
        remote_guarded = []
        exact = 0
        changed = 0
        replaced_legacy = 0
        mapped_legacy = 0
        for entry in entries_tuple:
            row = existing.get(entry.stable_identity)
            if row is None:
                continue
            if str(row.get("classification") or "").startswith("legacy"):
                mapped_legacy += 1
            reason = self.tm._remote_guard_reason(row)
            if reason:
                remote_guarded.append({"stable_identity": entry.stable_identity, "reason": reason})
            if (
                row["translation"] == entry.translation
                and row["source_fingerprint"] == entry.source_fingerprint
                and row["origin"] == "human"
                and bool(row["is_formal"])
                and bool(row["human_authored"])
            ):
                exact += 1
            else:
                changed += 1
                if str(row.get("classification") or "").startswith("legacy"):
                    replaced_legacy += 1
        legacy_rows = self.tm.connection.execute(
            "SELECT COUNT(*) FROM tm_entries WHERE project_id = ? "
            "AND classification LIKE 'legacy%'",
            (self.config.project.id,),
        ).fetchone()[0]
        recorded_at = datetime.now(timezone.utc).isoformat()
        summary = {
            "resources": len(resources),
            "accepted_units": len(entries_tuple),
            "new_rows": len(entries_tuple) - len(existing),
            "existing_exact": exact,
            "rows_to_replace": changed,
            "legacy_rows_to_promote": replaced_legacy,
            "remaining_legacy_rows": legacy_rows - mapped_legacy,
            "remote_guarded": len(remote_guarded),
        }
        report = {
            "schema_version": 1,
            "kind": "data_baseline",
            "status": "ready",
            "project_id": self.config.project.id,
            "recorded_at": recorded_at,
            "accepted_by": accepted_by or None,
            "artifact": {
                "manifest": str(self.manifest_path),
                "manifest_sha256": sha256(manifest_raw).hexdigest(),
                "archive": str(self.bundle.artifact),
                "archive_sha256": self.bundle.artifact_sha256,
                "run_id": self.bundle.run_id,
                "version": self.bundle.version,
                "quality_gate_passed": self.bundle.quality_gate_passed,
                "source_fingerprint": source_fingerprint,
            },
            "revision_matches_current": revision_matches,
            "summary": summary,
            "remote_guarded": remote_guarded,
        }
        return report, entries_tuple

    def adopt(
        self,
        *,
        accepted_by: str,
        backup_path: Path,
        report_path: Path,
        allow_remote_override: bool = False,
    ) -> dict:
        if not accepted_by.strip():
            raise ArtifactAdoptionRefused("--accepted-by is required when --apply is used")
        if self.tm.is_authoritative():
            raise ArtifactAdoptionRefused(
                "SQLite TM is already authoritative; accepted-artifact adoption must happen first"
            )
        report, entries = self.analyze(accepted_by=accepted_by.strip())
        if report["remote_guarded"] and not allow_remote_override:
            raise ArtifactAdoptionRefused(
                f"{len(report['remote_guarded'])} accepted coordinates are protected by "
                "remote/locked human decisions"
            )
        backup = self._backup_database(backup_path)
        try:
            result = self.tm.apply_human_review(
                entries,
                allow_remote_override=allow_remote_override,
                reject_guarded=True,
            )
            if len(result.written) != len(entries):
                raise TMGuardError(
                    f"accepted artifact applied {len(result.written)} of {len(entries)} rows"
                )
            report["status"] = "passed"
            report["backup"] = {
                "path": str(backup),
                "sha256": sha256(backup.read_bytes()).hexdigest(),
            }
            report["summary"]["applied_rows"] = len(result.written)
            AtomicIO.write_json(report_path, report)
        except Exception:
            # The SQLite write itself is transactional.  The pre-write backup is retained
            # even when a later audit-report write fails.
            raise
        return report

    def _backup_database(self, target: Path) -> Path:
        destination = Path(target).resolve()
        if destination.exists():
            raise ArtifactAdoptionRefused(f"TM backup already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        os.close(fd)
        Path(temp_name).unlink(missing_ok=True)
        try:
            backup = sqlite3.connect(temp_name)
            try:
                self.tm.connection.backup(backup)
            finally:
                backup.close()
            return AtomicIO.replace_file(Path(temp_name), destination)
        finally:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass

    def _safe_resource(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ArtifactAdoptionRefused(f"unsafe artifact resource path: {relative!r}")
        candidate = (self.resources_root / Path(*pure.parts)).resolve(strict=True)
        try:
            candidate.relative_to(self.resources_root)
        except ValueError as exc:
            raise ArtifactAdoptionRefused(
                f"artifact resource escapes resources root: {relative!r}"
            ) from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ArtifactAdoptionRefused(f"artifact resource is not a regular file: {relative}")
        return candidate

    @staticmethod
    def _manifest_files(manifest: Mapping[str, Any]) -> Dict[str, dict]:
        raw = manifest.get("files")
        if not isinstance(raw, list) or not raw:
            raise ArtifactAdoptionRefused("artifact manifest has no files")
        result = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ArtifactAdoptionRefused("artifact file descriptor is not an object")
            relative = str(item.get("relative_path") or "")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or not pure.parts or ".." in pure.parts:
                raise ArtifactAdoptionRefused(f"unsafe manifest path: {relative!r}")
            if relative in result:
                raise ArtifactAdoptionRefused(f"duplicate manifest resource: {relative}")
            digest = str(item.get("sha256") or "")
            size = item.get("size")
            if len(digest) != 64 or not isinstance(size, int) or size < 0:
                raise ArtifactAdoptionRefused(f"invalid manifest descriptor: {relative}")
            result[relative] = {"sha256": digest, "size": size}
        return result

    @staticmethod
    def _source_fingerprint(resources: Sequence[Any]) -> str:
        digest = sha256()
        for resource in sorted(
            resources, key=lambda item: item.adapter.scan(item.source).relative_path
        ):
            relative = resource.adapter.scan(resource.source).relative_path
            digest.update(relative.encode("utf-8"))
            digest.update(resource.source.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _gettext_translations(adapter, path: Path, units: Sequence[Any]) -> Dict[str, str]:
        try:
            catalog = polib.mofile(str(path)) if path.suffix.lower() == ".mo" else polib.pofile(str(path))
        except Exception as exc:
            raise ArtifactAdoptionRefused(f"cannot parse accepted Gettext resource: {path}") from exc
        by_key = {
            adapter._logical_key(entry.msgctxt, entry.msgid): entry
            for entry in catalog
            if not entry.obsolete and entry.msgid
        }
        result = {}
        for unit in units:
            key = adapter._logical_key(
                str(unit.metadata.get("msgctxt") or ""),
                str(unit.metadata.get("msgid") or ""),
            )
            entry = by_key.get(key)
            if entry is None:
                raise ArtifactAdoptionRefused(
                    f"accepted artifact is missing coordinate {unit.relative_path}:{unit.logical_key}"
                )
            translation = (
                entry.msgstr
                if unit.plural_index is None
                else entry.msgstr_plural.get(unit.plural_index, "")
            )
            if not isinstance(translation, str) or not translation.strip():
                raise ArtifactAdoptionRefused(
                    f"accepted artifact has an empty translation at "
                    f"{unit.relative_path}:{unit.logical_key}"
                )
            result[unit.stable_identity] = translation
        return result


class AcceptedArtifactVerifier:
    """Prove that the adopted TM reproduces the accepted resources without network."""

    def __init__(
        self,
        config: ProjectConfig,
        manifest: Path,
        *,
        resources_root: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.manifest_path = Path(manifest).resolve(strict=True)
        self.bundle = ReleaseBundle.load(self.manifest_path)
        self.resources_root = (
            Path(resources_root).resolve(strict=True)
            if resources_root is not None
            else (self.manifest_path.parent / "resources").resolve(strict=True)
        )

    def verify(self, *, run_id: str, report_path: Path) -> dict:
        validate_run_id(run_id)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        descriptors = AcceptedArtifactAdopter._manifest_files(manifest)
        runner = ProjectRunner(self.config, provider=_OfflineProvider())
        plan = runner.plan()
        if plan.pending:
            raise ArtifactAdoptionRefused(
                f"adopted TM is incomplete: {len(plan.pending)} units would call the Provider"
            )
        result = runner.run(mode=BuildMode.PREVIEW, run_id=run_id, plan=plan)
        if not result.build.quality_gate.passed:
            raise ArtifactAdoptionRefused("offline reproduction failed the current QualityGate")
        rendered_root = result.build.output_root / "resources"
        mismatches = []
        for relative, descriptor in sorted(descriptors.items()):
            candidate = rendered_root / Path(*PurePosixPath(relative).parts)
            if not candidate.is_file():
                mismatches.append({"path": relative, "reason": "missing"})
                continue
            digest = sha256(candidate.read_bytes()).hexdigest()
            if digest != descriptor["sha256"]:
                mismatches.append(
                    {
                        "path": relative,
                        "reason": "sha256",
                        "expected": descriptor["sha256"],
                        "actual": digest,
                    }
                )
        actual_paths = {
            path.relative_to(rendered_root).as_posix()
            for path in rendered_root.rglob("*")
            if path.is_file()
        }
        for relative in sorted(actual_paths - set(descriptors)):
            mismatches.append({"path": relative, "reason": "extra"})
        if mismatches:
            raise ArtifactAdoptionRefused(
                f"offline reproduction differs from accepted artifact in "
                f"{len(mismatches)} resources; first={mismatches[0]}"
            )
        report = {
            "schema_version": 1,
            "kind": "behavior_baseline",
            "status": "passed",
            "project_id": self.config.project.id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "resources_compared": len(descriptors),
                "units_resolved_without_provider": len(plan.translations),
                "pending_units": 0,
                "provider_requests": 0,
                "quality_gate_passed": True,
                "digest_mismatches": 0,
            },
            "artifact": {
                "manifest": str(self.manifest_path),
                "manifest_sha256": sha256(self.manifest_path.read_bytes()).hexdigest(),
                "run_id": self.bundle.run_id,
                "source_fingerprint": str(manifest.get("source_fingerprint") or ""),
            },
            "verification_run_id": run_id,
            "output_root": str(result.build.output_root),
        }
        AtomicIO.write_json(report_path, report)
        return report
