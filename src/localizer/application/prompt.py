from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from localizer.domain.translation_unit import TranslationUnit


@dataclass(frozen=True)
class PromptComposer:
    template: str
    background: str = ""
    glossary: str = ""

    def compose(self, units: Sequence[TranslationUnit], *, repair: bool = False) -> str:
        lines = [f"[{index}] {unit.source_text}" for index, unit in enumerate(units, 1)]
        protocol = (
            "Return exactly one item for every input using sequential [n] prefixes. "
            "Finish with a line containing ---END---."
        )
        repair_note = "\nThis is a protocol repair attempt; fix numbering and completeness." if repair else ""
        return "\n\n".join(
            part
            for part in (
                self.template.strip(),
                f"Background:\n{self.background.strip()}" if self.background.strip() else "",
                f"Glossary:\n{self.glossary.strip()}" if self.glossary.strip() else "",
                protocol + repair_note,
                "\n".join(lines),
            )
            if part
        )
