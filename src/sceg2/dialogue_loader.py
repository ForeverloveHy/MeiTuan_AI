from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json


def _normalize_dialogue(data: dict[str, Any], stem: str = "dialogue") -> dict[str, Any] | None:
    """Normalize legacy and 2.0 dialogue formats.

    Core logic stays format-driven: both `turns` and legacy `dialogue` are
    accepted, and dataset labels are copied into the common `sample_type`.
    """
    if not isinstance(data, dict):
        return None
    turns = data.get("turns")
    if turns is None:
        turns = data.get("dialogue")
    if not isinstance(turns, list):
        return None
    out = dict(data)
    out["turns"] = turns
    out.setdefault("id", data.get("dialogue_id") or stem)
    sample_type = data.get("sample_type") or data.get("pack_type") or data.get("expected_quality") or data.get("quality")
    if sample_type:
        sample_type_s = str(sample_type).lower()
        if sample_type_s in {"positive", "e0", "perfect", "正包"}:
            out["sample_type"] = "positive"
        elif sample_type_s in {"negative", "负包"}:
            out["sample_type"] = "negative"
    return out


def load_dialogues(root: str | Path) -> list[dict[str, Any]]:
    p = Path(root)
    if p.is_file():
        data = read_json(p)
        if isinstance(data, list):
            return [x for i, x in enumerate((_normalize_dialogue(d, f"item_{i}") for i, d in enumerate(data))) if x]
        item = _normalize_dialogue(data, p.stem)
        return [item] if item else []
    out: list[dict[str, Any]] = []
    for file in sorted(p.rglob("*.json")):
        if file.name.startswith("graph"):
            continue
        try:
            data = read_json(file)
        except Exception:
            continue
        item = _normalize_dialogue(data, file.stem)
        if item:
            out.append(item)
    return out
