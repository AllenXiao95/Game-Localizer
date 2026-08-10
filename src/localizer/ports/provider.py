from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence

from localizer.domain.translation_unit import TranslationUnit


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    finish_reason: Optional[str] = None
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    provider_metadata: Mapping[str, object] = field(default_factory=dict)


class TranslationProvider(Protocol):
    def translate(self, prompt: str, units: Sequence[TranslationUnit]) -> ProviderResponse:
        ...
