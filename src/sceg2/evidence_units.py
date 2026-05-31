from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Speaker = Literal["user", "assistant", "system", "unknown"]
Polarity = Literal["positive", "negative", "uncertain"]


@dataclass(slots=True)
class DialogueTurn:
    speaker: Speaker
    text: str
    index: int

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> "DialogueTurn":
        raw = str(data.get("speaker", data.get("role", "unknown"))).lower()
        if raw in {"客服", "assistant", "agent", "bot"}:
            speaker: Speaker = "assistant"
        elif raw in {"用户", "user", "customer"}:
            speaker = "user"
        else:
            speaker = "unknown"
        return cls(speaker=speaker, text=str(data.get("text", data.get("content", ""))), index=index)


@dataclass(slots=True)
class EvidenceUnit:
    turn_index: int
    speaker: Speaker
    text: str
    normalized: str
    kinds: set[str] = field(default_factory=set)
    polarity: Polarity = "uncertain"
    numbers: list[dict[str, Any]] = field(default_factory=list)
    markers: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "speaker": self.speaker,
            "text": self.text,
            "normalized": self.normalized,
            "kinds": sorted(self.kinds),
            "polarity": self.polarity,
            "numbers": self.numbers,
            "markers": self.markers,
        }
