from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .evidence_units import EvidenceUnit


def compact_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        vals: list[str] = []
        for key in ("any", "terms", "aliases", "values", "surface_forms"):
            vals.extend(_as_text_list(value.get(key)))
        text = str(value.get("text") or value.get("description") or "").strip()
        if text:
            vals.append(text)
        return _dedupe(vals)
    if isinstance(value, (list, tuple, set)):
        vals: list[str] = []
        for item in value:
            vals.extend(_as_text_list(item))
        return _dedupe(vals)
    return []


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = compact_text(text)
        if text and key and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _term_hit(term: str, text: str) -> bool:
    c_term = compact_text(term)
    c_text = compact_text(text)
    if not c_term or not c_text:
        return False
    if c_term in c_text:
        return True
    # Keep fuzzy tolerance narrow and schema-derived only. Very short terms stay exact.
    if len(c_term) < 4:
        return False
    term_chars = set(c_term)
    text_chars = set(c_text)
    if len(term_chars & text_chars) < 3:
        return False
    return len(term_chars & text_chars) / max(1, len(term_chars)) >= 0.86


@dataclass(slots=True)
class TargetScanHit:
    matched: bool = False
    turn_index: int | None = None
    text: str = ""
    matched_terms: list[str] = field(default_factory=list)
    reason: str = ""
    score: float = 0.0


class PositiveObjectScanner:
    """Schema-driven scanner for node positive objects.

    Nodes start from a target object: the graph states what action/fact/state is
    supposed to be supported, and the scanner searches assistant turns for text
    evidence of that target.  No business vocabulary is embedded here; all terms
    are read from requirement.positive_object.
    """

    DIRECT_KEYS = {
        "surface_forms",
        "semantic_equivalents",
        "examples",
        "example_phrases",
        "evidence_phrases",
    }
    SLOT_VALUE_KEYS = {"any", "terms", "aliases", "values", "surface_forms"}
    SKIP_KEYS = {
        "object_type",
        "type",
        "description",
        "evidence_logic",
        "logic",
        "min_slot_hits",
        "min_required_groups",
        "threshold",
        "none",
        "safe_exceptions",
        "notes",
    }

    def scan(self, positive_object: dict[str, Any], units: list[EvidenceUnit], *, speaker: str = "assistant") -> TargetScanHit:
        if not isinstance(positive_object, dict) or not positive_object:
            return TargetScanHit(reason="requirement 未声明 positive_object")
        scan_units = [u for u in units if not speaker or u.speaker == speaker]
        none_terms = _as_text_list(positive_object.get("none") or positive_object.get("safe_exceptions"))

        for unit in scan_units:
            if none_terms and any(_term_hit(term, unit.text) for term in none_terms):
                continue
            direct_hit = self._direct_hit(positive_object, unit.text)
            if direct_hit:
                return TargetScanHit(True, unit.turn_index, unit.text, direct_hit, "命中 requirement.positive_object 的直接表达", 1.0)

        groups = self._slot_groups(positive_object)
        if not groups:
            return TargetScanHit(reason="positive_object 没有可执行的目标词组")
        min_hits = int(positive_object.get("min_slot_hits") or positive_object.get("min_required_groups") or len(groups))
        min_hits = max(1, min(min_hits, len(groups)))
        best = TargetScanHit(reason="未找到足够目标槽位证据")
        for unit in scan_units:
            if none_terms and any(_term_hit(term, unit.text) for term in none_terms):
                continue
            matched_terms: list[str] = []
            matched_groups = 0
            for group in groups:
                hit = next((term for term in group if _term_hit(term, unit.text)), None)
                if hit:
                    matched_groups += 1
                    matched_terms.append(hit)
            score = matched_groups / max(1, len(groups))
            if score > best.score:
                best = TargetScanHit(False, unit.turn_index, unit.text, matched_terms, f"目标槽位命中 {matched_groups}/{len(groups)}，未达 {min_hits}", score)
            if matched_groups >= min_hits:
                return TargetScanHit(True, unit.turn_index, unit.text, matched_terms, f"命中 requirement.positive_object 目标槽位 {matched_groups}/{len(groups)}", 1.0)
        return best

    def _direct_hit(self, obj: dict[str, Any], text: str) -> list[str]:
        direct_terms: list[str] = []
        for key in self.DIRECT_KEYS:
            direct_terms.extend(_as_text_list(obj.get(key)))
        direct_terms = _dedupe(direct_terms)
        return [term for term in direct_terms if _term_hit(term, text)]

    def _slot_groups(self, obj: dict[str, Any]) -> list[list[str]]:
        groups: list[list[str]] = []
        explicit = obj.get("slot_groups") or obj.get("required_slots") or obj.get("slots")
        if isinstance(explicit, dict):
            for value in explicit.values():
                terms = _as_text_list(value)
                if terms:
                    groups.append(terms)
        elif isinstance(explicit, list):
            for value in explicit:
                terms = _as_text_list(value)
                if terms:
                    groups.append(terms)
        for key, value in obj.items():
            if key in self.SKIP_KEYS or key in self.DIRECT_KEYS:
                continue
            if key in {"slot_groups", "required_slots", "slots"}:
                continue
            if key.endswith(("_terms", "_aliases", "_markers", "_objects", "_actions", "_attributes", "_values")):
                terms = _as_text_list(value)
                if terms:
                    groups.append(terms)
            elif key in {"target_terms", "object_terms", "action_terms", "state_terms", "attribute_terms", "value_terms", "context_terms", "role_terms", "subject_aliases", "predicate_aliases", "value_aliases"}:
                terms = _as_text_list(value)
                if terms:
                    groups.append(terms)
        normalized: list[list[str]] = []
        seen: set[str] = set()
        for group in groups:
            terms = _dedupe(group)
            key = "|".join(compact_text(t) for t in terms)
            if terms and key not in seen:
                seen.add(key)
                normalized.append(terms)
        return normalized
