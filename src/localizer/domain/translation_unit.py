from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TranslationUnit:
    project_id: str
    adapter_id: str
    relative_path: str
    logical_key: str
    source_text: str
    source_locale: str
    target_locale: str
    translation: Optional[str] = None
    context: Optional[str] = None
    logical_file: Optional[str] = None
    placeholders: Tuple[str, ...] = field(default_factory=tuple)
    plural_index: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "project_id": self.project_id,
            "adapter_id": self.adapter_id,
            "relative_path": self.relative_path,
            "logical_key": self.logical_key,
            "source_locale": self.source_locale,
            "target_locale": self.target_locale,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        object.__setattr__(self, "relative_path", self.relative_path.replace("\\", "/"))
        object.__setattr__(self, "placeholders", tuple(self.placeholders))

    @property
    def stable_identity(self) -> str:
        raw = "\x1f".join(
            (self.project_id, self.adapter_id, self.relative_path, self.logical_key)
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    @property
    def source_fingerprint(self) -> str:
        return sha256(self.source_text.encode("utf-8")).hexdigest()

    def with_placeholders(self, values: Sequence[str]) -> "TranslationUnit":
        return TranslationUnit(
            **{
                **self.__dict__,
                "placeholders": tuple(values),
            }
        )
