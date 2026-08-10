from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

from localizer.ports.provider import ProviderResponse


class ResponseProtocolError(ValueError):
    retryable = True


class NumberingError(ResponseProtocolError):
    pass


class TruncatedResponse(ResponseProtocolError):
    pass


@dataclass(frozen=True)
class ParsedResponse:
    translations: Tuple[str, ...]
    raw_text: str


class ResponseParser:
    END_SENTINEL = "---END---"
    ITEM_RE = re.compile(r"(?m)^\[(\d+)\][ \t]*(.*)$")

    def parse(self, response: ProviderResponse, expected_count: int) -> ParsedResponse:
        if expected_count <= 0:
            raise ValueError("expected_count must be positive")
        if response.finish_reason == "length":
            raise TruncatedResponse("provider reported finish_reason=length")
        sentinel_index = response.text.find(self.END_SENTINEL)
        if sentinel_index < 0:
            raise TruncatedResponse("response is missing the end sentinel")
        body = response.text[:sentinel_index].rstrip()
        matches = list(self.ITEM_RE.finditer(body))
        if not matches:
            raise NumberingError("response contains no numbered items")
        numbers = [int(match.group(1)) for match in matches]
        expected_numbers = list(range(1, expected_count + 1))
        if numbers != expected_numbers:
            raise NumberingError(f"expected numbering {expected_numbers}, got {numbers}")
        translations = []
        for index, match in enumerate(matches):
            start = match.start(2)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            translation = body[start:end].strip()
            if not translation:
                raise ResponseProtocolError(f"translation item {index + 1} is empty")
            translations.append(translation)
        return ParsedResponse(tuple(translations), response.text)
