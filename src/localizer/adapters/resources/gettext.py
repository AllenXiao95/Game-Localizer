from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import polib

from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.adapters.resources.registry import register_adapter
from localizer.adapters.resources.options import GettextOptions, normalize_options
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult


@register_adapter
class GettextAdapter:
    adapter_id = "gettext"
    options_model = GettextOptions

    def __init__(
        self,
        *,
        project_id: str,
        source_root: Path,
        source_locale: str,
        target_locale: str,
        options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.options = normalize_options(self.options_model, options)
        self.layout = self.options.get("layout", "standard")
        self.empty_source = self.options.get("empty_source", "skip")
        self.source_filter = self.options.get("source_filter", "all")
        self.project_id = project_id
        self.source_root = Path(source_root).resolve()
        self.source_locale = source_locale
        self.target_locale = target_locale

    def probe(self, path: Path) -> float:
        return 1.0 if Path(path).suffix.lower() in {".po", ".mo"} else 0.0

    def scan(self, path: Path) -> ResourceDescriptor:
        candidate = Path(path).resolve(strict=True)
        if not candidate.is_file() or self.probe(candidate) == 0:
            raise ValueError(f"not a gettext resource: {candidate}")
        return ResourceDescriptor(
            adapter_id=self.adapter_id,
            path=candidate,
            relative_path=self._relative_path(candidate),
            size=candidate.stat().st_size,
            confidence=1.0,
        )

    def extract(self, path: Path) -> Sequence[TranslationUnit]:
        candidate = Path(path).resolve(strict=True)
        catalog = self._load(candidate)
        relative_path = self._relative_path(candidate)
        units: List[TranslationUnit] = []
        for entry_index, entry in enumerate(catalog):
            if entry.obsolete or not entry.msgid:
                continue
            logical_base = self._logical_key(entry.msgctxt, entry.msgid)
            metadata = {
                "entry_index": entry_index,
                "msgid": entry.msgid,
                "msgctxt": entry.msgctxt,
                "comment": entry.comment,
                "tcomment": entry.tcomment,
                "flags": tuple(entry.flags),
            }
            if entry.msgid_plural:
                plural_indexes = sorted(entry.msgstr_plural, key=lambda value: int(value))
                if not plural_indexes:
                    plural_indexes = ["0", "1"]
                for raw_index in plural_indexes:
                    plural_index = int(raw_index)
                    if self.layout == "keyed_source":
                        source_text = entry.msgstr_plural.get(raw_index, "")
                        translation = ""
                    else:
                        source_text = entry.msgid if plural_index == 0 else entry.msgid_plural
                        translation = entry.msgstr_plural.get(raw_index, "")
                    if not source_text.strip():
                        if self.empty_source == "error":
                            raise ValueError(
                                f"empty Gettext source at {relative_path}:{logical_base}"
                            )
                        continue
                    if not self._source_is_selected(source_text):
                        continue
                    units.append(
                        self._unit(
                            relative_path=relative_path,
                            logical_key=f"{logical_base}[plural={plural_index}]",
                            source_text=source_text,
                            translation=translation,
                            context=entry.msgctxt,
                            plural_index=plural_index,
                            metadata=metadata,
                        )
                    )
            else:
                source_text = entry.msgstr if self.layout == "keyed_source" else entry.msgid
                translation = "" if self.layout == "keyed_source" else entry.msgstr
                if not source_text.strip():
                    if self.empty_source == "error":
                        raise ValueError(
                            f"empty Gettext source at {relative_path}:{logical_base}"
                        )
                    continue
                if not self._source_is_selected(source_text):
                    continue
                units.append(
                    self._unit(
                        relative_path=relative_path,
                        logical_key=logical_base,
                        source_text=source_text,
                        translation=translation,
                        context=entry.msgctxt,
                        plural_index=None,
                        metadata=metadata,
                    )
                )
        return tuple(units)

    def render(
        self, units: Sequence[TranslationUnit], source: Path, destination: Path
    ) -> RenderResult:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        catalog = self._load(source_path)
        by_key = {unit.logical_key: unit for unit in units}
        for entry in catalog:
            if entry.obsolete or not entry.msgid:
                continue
            logical_base = self._logical_key(entry.msgctxt, entry.msgid)
            if entry.msgid_plural:
                for raw_index in list(entry.msgstr_plural):
                    key = f"{logical_base}[plural={int(raw_index)}]"
                    unit = by_key.get(key)
                    if unit is not None and unit.translation is not None:
                        entry.msgstr_plural[raw_index] = unit.translation
            else:
                unit = by_key.get(logical_base)
                if unit is not None and unit.translation is not None:
                    entry.msgstr = unit.translation

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=f"{destination_path.suffix}.tmp",
            dir=str(destination_path.parent),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            if destination_path.suffix.lower() == ".mo":
                if isinstance(catalog, polib.MOFile):
                    catalog.save(str(temp_path))
                else:
                    catalog.save_as_mofile(str(temp_path))
            else:
                catalog.save(str(temp_path))
            validation = self.validate(temp_path, expected_suffix=destination_path.suffix)
            if not validation.valid:
                raise ValueError("rendered gettext file is invalid: " + "; ".join(validation.errors))
            AtomicIO.replace_file(temp_path, destination_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        final_validation = self.validate(destination_path)
        return RenderResult(destination_path, len(units), final_validation)

    def validate(self, path: Path, *, expected_suffix: str = "") -> ValidationResult:
        candidate = Path(path)
        suffix = expected_suffix.lower() or candidate.suffix.lower()
        try:
            if suffix == ".mo":
                polib.mofile(str(candidate))
            elif suffix == ".po":
                polib.pofile(str(candidate))
            else:
                return ValidationResult(False, (f"unsupported gettext suffix: {suffix}",))
        except Exception as exc:
            return ValidationResult(False, (str(exc),))
        return ValidationResult(True)

    def _load(self, path: Path):
        suffix = path.suffix.lower()
        if suffix == ".mo":
            return polib.mofile(str(path))
        if suffix == ".po":
            return polib.pofile(str(path))
        raise ValueError(f"unsupported gettext suffix: {suffix}")

    def _relative_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.source_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"resource is outside source root {self.source_root}: {path}") from exc

    def _unit(
        self,
        *,
        relative_path: str,
        logical_key: str,
        source_text: str,
        translation: str,
        context: str,
        plural_index: int,
        metadata: dict,
    ) -> TranslationUnit:
        return TranslationUnit(
            project_id=self.project_id,
            adapter_id=self.adapter_id,
            relative_path=relative_path,
            logical_file=relative_path,
            logical_key=logical_key,
            source_text=source_text,
            translation=translation,
            context=context,
            source_locale=self.source_locale,
            target_locale=self.target_locale,
            plural_index=plural_index,
            metadata=metadata,
        )

    @staticmethod
    def _logical_key(context: str, msgid: str) -> str:
        return f"{context}\x04{msgid}" if context else msgid

    def _source_is_selected(self, source_text: str) -> bool:
        if self.source_filter == "all":
            return True
        # WOT v5/v6 的成熟选择规则：只把含西里尔且尚未含 CJK 的正文送入模型。
        # `?empty?`、纯数值、百分比、占位符和已经是中文的条目原样保留。
        return bool(re.search(r"[\u0400-\u04ff]", source_text)) and not bool(
            re.search(r"[\u3400-\u9fff]", source_text)
        )
