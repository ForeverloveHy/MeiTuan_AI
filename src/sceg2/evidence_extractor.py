from __future__ import annotations

import re
from typing import Any

from .evidence_units import DialogueTurn, EvidenceUnit
from .normalizer import normalize_text

_NUMBER_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[\u4e00-\u9fa5a-zA-Z%]+)?")
_RANGE_RE = re.compile(r"(?P<a>\d+(?:\.\d+)?)\s*(?:-|到|至|~)\s*(?P<b>\d+(?:\.\d+)?)\s*(?P<unit>[\u4e00-\u9fa5a-zA-Z%]+)?")


class EvidenceExtractor:
    """Format-only evidence extractor.

    2.0-v1 keeps semantic understanding out of code. The extractor only
    normalizes turns, records speaker, preserves the original text, and parses
    explicit numeric/range values. Any synonym, trigger phrase, risk phrase,
    safe phrase or task expression must come from the state graph / auxiliary
    tables generated during the offline graph-building stage.
    """

    def extract(self, turns: list[DialogueTurn]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for turn in turns:
            units.append(
                EvidenceUnit(
                    turn_index=turn.index,
                    speaker=turn.speaker,
                    text=turn.text,
                    normalized=normalize_text(turn.text),
                    numbers=self._extract_numbers(turn.text),
                )
            )
        return units

    def _extract_numbers(self, text: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        occupied: list[tuple[int, int]] = []
        for m in _RANGE_RE.finditer(text):
            occupied.append((m.start(), m.end()))
            values.append({"type": "range", "start": float(m.group("a")), "end": float(m.group("b")), "unit": m.group("unit") or ""})
        for m in _NUMBER_RE.finditer(text):
            if any(s <= m.start() < e for s, e in occupied):
                continue
            values.append({"type": "number", "value": float(m.group("num")), "unit": m.group("unit") or ""})
        return values
