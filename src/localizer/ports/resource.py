from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, Tuple

from localizer.domain.translation_unit import TranslationUnit


@dataclass(frozen=True)
class ResourceDescriptor:
    adapter_id: str
    path: Path
    relative_path: str
    size: int
    confidence: float


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderResult:
    destination: Path
    unit_count: int
    validation: ValidationResult


class ResourceAdapter(Protocol):
    adapter_id: str

    def probe(self, path: Path) -> float:
        ...

    def scan(self, path: Path) -> ResourceDescriptor:
        ...

    def extract(self, path: Path) -> Sequence[TranslationUnit]:
        ...

    def render(
        self, units: Sequence[TranslationUnit], source: Path, destination: Path
    ) -> RenderResult:
        ...

    def validate(self, path: Path) -> ValidationResult:
        ...


def default_destination(adapter: "ResourceAdapter", source: Path, output_root: Path) -> Path:
    return Path(output_root) / adapter.scan(source).relative_path


def resolve_destination(
    adapter: "ResourceAdapter", source: Path, output_root: Path
) -> Path:
    """输出路径由 Adapter 决定，缺省才用「源相对路径」。

    内核原先把输出路径写死成 `output_root / 源相对路径`，这排除了一切
    「目录名或文件名编码了语言」的格式 —— Paradox（english/x_l_english.yml →
    simp_chinese/x_l_simp_chinese.yml）、Bannerlord、RimShot 都是这样。
    Adapter 可选实现 plan_destination()；gettext 与 paratranz_json 一行都不用改。
    """
    planner = getattr(adapter, "plan_destination", None)
    if planner is None:
        return default_destination(adapter, source, output_root)
    return Path(planner(source, output_root))
