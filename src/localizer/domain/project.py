from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    game_version: str
    source_locale: str
    target_locale: str

    def __post_init__(self) -> None:
        for field_name in ("project_id", "name", "game_version", "source_locale", "target_locale"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
