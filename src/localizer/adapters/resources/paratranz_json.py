from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.adapters.resources.registry import register_adapter
from localizer.adapters.resources.options import ParaTranzJsonOptions, normalize_options
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult


@register_adapter
class ParaTranzJsonAdapter:
    adapter_id = "paratranz_json"
    options_model = ParaTranzJsonOptions

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
        self.project_id = project_id
        self.source_root = Path(source_root).resolve()
        self.source_locale = source_locale
        self.target_locale = target_locale

    def probe(self, path: Path) -> float:
        candidate = Path(path)
        if candidate.suffix.lower() != ".json":
            return 0.0
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 0.0
        key_field = self.options.get("key_field", "key")
        source_field = self.options.get("source_field", "original")
        if isinstance(raw, list) and all(
            isinstance(item, Mapping) and key_field in item and source_field in item
            for item in raw
        ):
            return 1.0
        return 0.0

    def scan(self, path: Path) -> ResourceDescriptor:
        candidate = Path(path).resolve(strict=True)
        confidence = self.probe(candidate)
        if confidence == 0:
            raise ValueError(f"not a ParaTranz JSON resource: {candidate}")
        return ResourceDescriptor(
            self.adapter_id,
            candidate,
            self._relative(candidate),
            candidate.stat().st_size,
            confidence,
        )

    def extract(self, path: Path) -> Sequence[TranslationUnit]:
        candidate = Path(path).resolve(strict=True)
        raw = self._load(candidate)
        relative = self._relative(candidate)
        units: List[TranslationUnit] = []
        key_field = self.options["key_field"]
        source_field = self.options["source_field"]
        translation_field = self.options["translation_field"]
        context_field = self.options["context_field"]
        stage_field = self.options["stage_field"]
        id_field = self.options["id_field"]
        for index, item in enumerate(raw):
            units.append(
                TranslationUnit(
                    project_id=self.project_id,
                    adapter_id=self.adapter_id,
                    relative_path=relative,
                    logical_file=relative,
                    logical_key=str(item[key_field]),
                    source_text=str(item[source_field]),
                    translation=item.get(translation_field) or "",
                    context=item.get(context_field),
                    source_locale=self.source_locale,
                    target_locale=self.target_locale,
                    metadata={
                        "index": index,
                        "stage": item.get(stage_field, 0),
                        "paratranz_id": item.get(id_field),
                    },
                )
            )
        return tuple(units)

    def render(
        self, units: Sequence[TranslationUnit], source: Path, destination: Path
    ) -> RenderResult:
        raw = self._load(Path(source).resolve(strict=True))
        key_field = self.options["key_field"]
        translation_field = self.options["translation_field"]
        translations = {unit.logical_key: unit.translation for unit in units}
        for item in raw:
            value = translations.get(str(item[key_field]))
            if value is not None:
                item[translation_field] = value
        destination_path = AtomicIO.write_json(Path(destination), raw)
        validation = self.validate(destination_path)
        return RenderResult(destination_path, len(units), validation)

    def validate(self, path: Path) -> ValidationResult:
        try:
            self._load(Path(path))
        except Exception as exc:
            return ValidationResult(False, (str(exc),))
        return ValidationResult(True)

    def _load(self, path: Path) -> list:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("ParaTranz JSON root must be a list")
        for index, item in enumerate(raw):
            required = (self.options["key_field"], self.options["source_field"])
            if not isinstance(item, Mapping) or any(name not in item for name in required):
                raise ValueError(
                    f"ParaTranz item {index} requires fields {required}"
                )
            stage = item.get(self.options["stage_field"], 0)
            if stage not in {0, 1, 2, 3, 5, 9, -1}:
                raise ValueError(f"ParaTranz item {index} has unsupported stage {stage}")
        return raw

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.source_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"resource is outside source root {self.source_root}: {path}") from exc
