from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .evidence_units import EvidenceUnit
from .normalizer import contains_all, contains_any
from .schema import EvidenceGroup
from .generic_customer_service_expressions import (
    AWARENESS_VERBS,
    INQUIRY_MARKERS,
    SHORT_ACK_AFFIRMATIVE_MARKERS,
    SHORT_ACK_NEGATIVE_MARKERS,
    STRUCTURAL_SIGNAL_MARKERS,
)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _char_similarity(a: str, b: str) -> float:
    """Loose phrase overlap used only as a fallback for schema phrases.

    It is deliberately task-agnostic: the phrase still comes from the graph or
    auxiliary tables.  The local evaluator only tolerates small wording changes
    such as inserted negation/modifier words, so reports remain traceable to the
    original schema phrase.
    """
    ca = _compact_text(a)
    cb = _compact_text(b)
    if not ca or not cb:
        return 0.0
    if ca in cb or cb in ca:
        return 1.0
    # Very short schema tokens are kept exact to avoid over-matching.
    if min(len(ca), len(cb)) < 3:
        return 0.0
    set_a = set(ca)
    set_b = set(cb)
    common = len(set_a & set_b)
    if common < 2:
        return 0.0
    return common / max(1, min(len(set_a), len(set_b)))


def _phrase_matches(value: Any, text: str) -> bool:
    v = str(value or "").lower()
    t = str(text or "").lower()
    if not v:
        return False
    if v in t:
        return True
    return _char_similarity(v, t) >= 0.75




def _is_generic_inquiry_phrase(value: Any) -> bool:
    t = _compact_text(str(value or ""))
    if not t:
        return False
    return any(x in t for x in INQUIRY_MARKERS) and any(x in t for x in AWARENESS_VERBS)


def _has_generic_inquiry_equivalent(text: str) -> bool:
    t = _compact_text(text)
    return any(x in t for x in INQUIRY_MARKERS) and any(x in t for x in AWARENESS_VERBS)


def _relaxed_schema_any_value_matches(value: Any, text: str) -> bool:
    """Task-agnostic tolerance for LLM evidence-group paraphrases.

    The concrete value still comes from the schema.  This only handles two
    cross-domain language effects:
    1) awareness questions such as "知道吗" vs. "了解吗";
    2) long Chinese phrases with small filler/suffix changes.

    It is used only for node evidence groups that already require multiple
    evidence hits, never for knowledge or constraint table rules.
    """
    if _is_generic_inquiry_phrase(value):
        return _has_generic_inquiry_equivalent(text)
    compact_value = _compact_text(str(value or ""))
    if len(compact_value) >= 6 and _char_similarity(str(value or ""), str(text or "")) >= 0.82:
        return True
    return False


def _is_short_chinese_predicate(value: Any) -> bool:
    t = _compact_text(str(value or ""))
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{1,2}", t))


def _matched_any_has_structural_signal(values: list[Any]) -> bool:
    return any(any(m in str(v or "") for m in STRUCTURAL_SIGNAL_MARKERS) for v in values)

def _is_short_affirmation(text: str) -> bool:
    """Generic cross-turn acknowledgement detector.

    This is intentionally not a domain dictionary: it only recognizes short,
    common Chinese confirmations so a schema pattern can model
    "assistant asks / user confirms" without knowing the business task.
    """
    t = _compact_text(text)
    if not t or len(t) > 16:
        return False
    if any(x in t for x in SHORT_ACK_NEGATIVE_MARKERS):
        return False
    return any(x in t for x in SHORT_ACK_AFFIRMATIVE_MARKERS)


@dataclass(slots=True)
class PatternHit:
    pattern: dict[str, Any]
    turn_index: int
    text: str
    score: float = 1.0


@dataclass(slots=True)
class GroupMatch:
    group_id: str
    description: str
    required: bool
    matched: bool
    hits: list[PatternHit] = field(default_factory=list)
    score: float = 0.0
    aliases: list[str] = field(default_factory=list)
    expected_patterns: list[str] = field(default_factory=list)


class EvidenceMatcher:
    """Generic evidence matcher driven by graph evidence groups."""

    def __init__(self, enable_fuzzy: bool = False, broad_terms: set[str] | None = None) -> None:
        self.enable_fuzzy = enable_fuzzy
        self.broad_terms = {str(x) for x in (broad_terms or set()) if str(x)}

    def _value_matches(self, value: Any, text: str) -> bool:
        if self.enable_fuzzy:
            return _phrase_matches(value, text)
        v = str(value or "").lower()
        return bool(v and v in str(text or "").lower())

    def _execution_pattern_for_group(self, pattern: dict[str, Any]) -> dict[str, Any]:
        """Strengthen only node evidence groups, not table rules.

        This method is called exclusively by match_group().  Constraint and
        knowledge judges call _match_pattern() directly, so their safe/prohibited
        patterns keep the exact semantics supplied by the table.
        """
        strengthened: dict[str, Any] | None = None
        if (
            pattern.get("any")
            and pattern.get("speaker") in {None, "", "assistant"}
            and pattern.get("all")
            and not pattern.get("regex_any")
            and not pattern.get("number")
        ):
            all_values = list(pattern.get("all") or [])
            if any(_is_generic_inquiry_phrase(v) or _is_short_chinese_predicate(v) for v in all_values):
                strengthened = dict(pattern)
                strengthened["_allow_relaxed_all"] = True
                strengthened.setdefault("compiler_notes", [])
                strengthened["compiler_notes"] = [*list(strengthened.get("compiler_notes") or []), "runtime_relaxed_weak_all"]
        if (
            pattern.get("any")
            and pattern.get("speaker") in {None, "", "assistant"}
            and len(list(pattern.get("any") or [])) >= 4
            and not pattern.get("all")
            and not pattern.get("regex_any")
            and not pattern.get("number")
        ):
            strengthened = dict(strengthened or pattern)
            if not strengthened.get("min_any_hits"):
                strengthened["min_any_hits"] = 2
            weak_terms = [str(x) for x in strengthened.get("any") or [] if str(x) in self.broad_terms]
            if weak_terms and len(weak_terms) < len(list(strengthened.get("any") or [])):
                strengthened["_weak_any_terms"] = weak_terms
                strengthened["_require_nonweak_any_hit"] = True
            strengthened["_allow_relaxed_schema_any"] = True
            strengthened.setdefault("compiler_notes", [])
            strengthened["compiler_notes"] = [*list(strengthened.get("compiler_notes") or []), "runtime_broad_any_requires_two_hits"]
            return strengthened
        return strengthened or pattern

    def match_group(self, group: EvidenceGroup, units: list[EvidenceUnit]) -> GroupMatch:
        hits: list[PatternHit] = []
        for pattern in group.patterns:
            exec_pattern = self._execution_pattern_for_group(pattern)
            matched_single = False
            for unit in units:
                if self._match_pattern(exec_pattern, unit, units):
                    hits.append(PatternHit(pattern=exec_pattern, turn_index=unit.turn_index, text=unit.text))
                    matched_single = True
                    break
            if not matched_single:
                # 通用跨句合并：电话话术经常被约束为 15-20 字一句，
                # LLM 却可能把“两个要点都要说”编成同一个 any + min_any_hits。
                # 单句不命中时，允许同一 speaker 的多句共同满足该 evidence group。
                # 具体词仍完全来自 schema，不在本地写业务词典。
                hits.extend(self._match_pattern_across_turns(exec_pattern, units))
        matched = len(hits) >= max(1, group.min_hits)
        score = min(1.0, len(hits) / max(1, group.min_hits)) if group.patterns else 0.0
        return GroupMatch(
            group_id=group.id,
            description=group.description,
            required=group.required,
            matched=matched,
            hits=hits,
            score=score,
            aliases=list(group.aliases),
            expected_patterns=[self._pattern_label(p) for p in group.patterns],
        )

    def _match_pattern_across_turns(self, pattern: dict[str, Any], units: list[EvidenceUnit]) -> list[PatternHit]:
        if pattern.get("window_union"):
            return []
        if not pattern.get("any") or pattern.get("all") or pattern.get("regex_any") or pattern.get("number"):
            return []
        min_any_hits = int(pattern.get("min_any_hits") or 1)
        if min_any_hits <= 1:
            return []
        speaker = pattern.get("speaker")
        if speaker not in {"assistant", "user"}:
            return []
        kinds = pattern.get("kinds_any") or []
        polarity = pattern.get("polarity")
        none_values = [str(x or "").lower() for x in list(pattern.get("none") or [])]
        matched: list[tuple[Any, EvidenceUnit]] = []
        seen_values: set[str] = set()
        for value in pattern.get("any") or []:
            key = str(value)
            if key in seen_values:
                continue
            for unit in units:
                if unit.speaker != speaker:
                    continue
                if kinds and not any(k in unit.kinds for k in kinds):
                    continue
                if polarity and unit.polarity != polarity:
                    continue
                if none_values and contains_any(str(unit.text or "").lower(), none_values):
                    continue
                if self._value_matches(value, unit.text):
                    matched.append((value, unit))
                    seen_values.add(key)
                    break
        if len(matched) < min_any_hits:
            return []
        # 只保留能解释 min_any_hits 的最早几句，避免报告刷屏。
        out: list[PatternHit] = []
        for value, unit in sorted(matched, key=lambda x: x[1].turn_index)[:min_any_hits]:
            trace_pattern = dict(pattern)
            trace_pattern["_cross_turn_value"] = value
            trace_pattern["window_union"] = True
            out.append(PatternHit(pattern=trace_pattern, turn_index=unit.turn_index, text=unit.text, score=1.0 / min_any_hits))
        return out

    def match_groups(self, groups: list[EvidenceGroup], units: list[EvidenceUnit]) -> list[GroupMatch]:
        return [self.match_group(g, units) for g in groups]

    def _pattern_label(self, pattern: dict[str, Any]) -> str:
        parts: list[str] = []
        speaker = pattern.get("speaker")
        if speaker:
            parts.append(f"speaker={speaker}")
        if pattern.get("all"):
            parts.append("all=" + "/".join(str(x) for x in pattern.get("all") or []))
        if pattern.get("any"):
            parts.append("any=" + "/".join(str(x) for x in pattern.get("any") or []))
        if pattern.get("min_any_hits"):
            parts.append(f"min_any_hits={pattern.get('min_any_hits')}")
        if pattern.get("regex_any"):
            parts.append("regex=" + "/".join(str(x) for x in pattern.get("regex_any") or []))
        if pattern.get("window_union"):
            parts.append("window_union=true")
        if pattern.get("cross_turn"):
            parts.append(f"cross_turn={pattern.get('cross_turn')}")
        if pattern.get("reply_to_user_any"):
            parts.append("reply_to_user_any=" + "/".join(str(x) for x in pattern.get("reply_to_user_any") or []))
        return "；".join(parts) if parts else str(pattern)

    def _value_matches_relaxed_for_group(self, value: Any, text: str) -> bool:
        return self._value_matches(value, text) or _phrase_matches(value, text)

    def _relaxed_all_matches(self, pattern: dict[str, Any], all_values: list[str], matched_values: list[Any], text: str) -> bool:
        if not pattern.get("_allow_relaxed_all") or not pattern.get("any"):
            return False
        if not matched_values:
            return False
        for value in all_values:
            if _is_generic_inquiry_phrase(value):
                if not _has_generic_inquiry_equivalent(text):
                    return False
                continue
            if _is_short_chinese_predicate(value):
                if len(matched_values) >= 3 or _matched_any_has_structural_signal(matched_values):
                    continue
                return False
            return False
        return True


    def _match_pattern(self, pattern: dict[str, Any], unit: EvidenceUnit, all_units: list[EvidenceUnit]) -> bool:
        # A small schema-level extension: one evidence group may require evidence
        # across two adjacent turns, for example "assistant asks" + "user
        # confirms". The concrete ask terms are still supplied by the graph;
        # the evaluator only supplies a generic short-confirmation detector.
        if pattern.get("cross_turn") in {"assistant_ask_user_affirm", "ask_user_affirm"}:
            return self._match_assistant_ask_user_affirm(pattern, unit, all_units)

        speaker = pattern.get("speaker")
        if speaker and unit.speaker != speaker:
            return False
        kinds = pattern.get("kinds_any") or []
        if kinds and not any(k in unit.kinds for k in kinds):
            return False
        if pattern.get("polarity") and unit.polarity != pattern["polarity"]:
            return False
        if pattern.get("window_union"):
            return self._match_window_union(pattern, unit, all_units)

        text = unit.text
        text_lc = str(text or "").lower()
        all_values = [str(x or "").lower() for x in list(pattern.get("all") or [])]
        all_ok = not pattern.get("all") or contains_all(text_lc, all_values)
        matched_values: list[Any] = []
        if pattern.get("any"):
            any_values = list(pattern["any"])
            min_any_hits = int(pattern.get("min_any_hits") or 1)
            if all_ok:
                matched_values = [
                    value for value in any_values
                    if self._value_matches(value, text)
                    or (pattern.get("_allow_relaxed_schema_any") and _relaxed_schema_any_value_matches(value, text))
                ]
            else:
                matched_values = [value for value in any_values if self._value_matches_relaxed_for_group(value, text)]
            hit_count = len(matched_values)
            if hit_count < min_any_hits:
                return False
            if pattern.get("_require_nonweak_any_hit"):
                weak = {str(x) for x in pattern.get("_weak_any_terms") or []}
                if weak and not any(str(value) not in weak for value in matched_values):
                    return False
        if not all_ok and not self._relaxed_all_matches(pattern, all_values, matched_values, text):
            return False
        none_values = [str(x or "").lower() for x in list(pattern.get("none") or [])]
        if pattern.get("none") and contains_any(text_lc, none_values):
            return False
        regexes = pattern.get("regex_any") or []
        if regexes and not any(re.search(r, text, flags=re.I) for r in regexes):
            return False

        # Contextual short confirmation: graph supplies the prior user patterns,
        # core only checks adjacency and generic acknowledgement text.
        if pattern.get("reply_to_user_any"):
            if unit.speaker != "assistant":
                return False
            short_reply = pattern.get("assistant_any") or []
            if short_reply and not contains_any(text_lc, [str(x or "").lower() for x in list(short_reply)]):
                return False
            previous = [u for u in all_units if u.turn_index < unit.turn_index and u.speaker == "user"]
            if not previous:
                return False
            prev = previous[-1].text
            if not contains_any(str(prev or "").lower(), [str(x or "").lower() for x in list(pattern["reply_to_user_any"])]):
                return False

        number_rule = pattern.get("number")
        if number_rule and not self._match_number(number_rule, unit):
            return False
        return True

    def _match_assistant_ask_user_affirm(self, pattern: dict[str, Any], unit: EvidenceUnit, all_units: list[EvidenceUnit]) -> bool:
        if unit.speaker != "assistant":
            return False
        text = unit.text
        ask_any = list(pattern.get("ask_any") or pattern.get("any") or [])
        ask_all = list(pattern.get("ask_all") or pattern.get("all") or [])
        ask_regex_any = list(pattern.get("ask_regex_any") or [])
        text_lc = str(text or "").lower()
        if ask_all and not contains_all(text_lc, [str(x or "").lower() for x in ask_all]):
            return False
        if ask_any and not contains_any(text_lc, [str(x or "").lower() for x in ask_any]):
            return False
        if ask_regex_any and not any(re.search(r, text, flags=re.I) for r in ask_regex_any):
            return False
        if not ask_any and not ask_all and not ask_regex_any:
            return False
        window = int(pattern.get("reply_window") or 2)
        following = [u for u in all_units if u.speaker == "user" and unit.turn_index < u.turn_index <= unit.turn_index + window]
        if not following:
            return False
        user_any = list(pattern.get("user_any") or [])
        for nxt in following:
            if user_any and contains_any(str(nxt.text or "").lower(), [str(x or "").lower() for x in user_any]):
                return True
            if _is_short_affirmation(nxt.text):
                return True
        return False

    def _match_window_union(self, pattern: dict[str, Any], unit: EvidenceUnit, all_units: list[EvidenceUnit]) -> bool:
        speaker = pattern.get("speaker") or unit.speaker
        values = [str(x) for x in (pattern.get("any") or []) if str(x)]
        if not values:
            return False
        same_speaker = [u for u in all_units if u.speaker == speaker]
        joined = "\n".join(u.text for u in same_speaker)
        hit_count = sum(1 for value in values if self._value_matches(value, joined))
        if pattern.get("user_affirm_bonus"):
            ask_any = list(pattern.get("ask_any") or values)
            for ask in [u for u in all_units if u.speaker == "assistant"]:
                if ask_any and not contains_any(ask.text, ask_any):
                    continue
                following = [u for u in all_units if u.speaker == "user" and ask.turn_index < u.turn_index <= ask.turn_index + int(pattern.get("reply_window") or 2)]
                if any(_is_short_affirmation(u.text) for u in following):
                    hit_count += 1
                    break
        min_any_hits = int(pattern.get("min_any_hits") or 1)
        if hit_count < min_any_hits:
            return False
        # Anchor the hit to a turn that carries at least one of the values, so
        # reports can still trace back to a concrete utterance.
        return any(self._value_matches(value, unit.text) for value in values)

    def _match_number(self, rule: dict[str, Any], unit: EvidenceUnit) -> bool:
        if not unit.numbers:
            return False
        min_v = rule.get("min")
        max_v = rule.get("max")
        unit_sub = rule.get("unit_contains")
        for n in unit.numbers:
            values = [n.get("value")] if n.get("type") == "number" else [n.get("start"), n.get("end")]
            if unit_sub and unit_sub not in str(n.get("unit", "")):
                continue
            ok = True
            for v in values:
                if v is None:
                    ok = False
                    continue
                if min_v is not None and float(v) < float(min_v):
                    ok = False
                if max_v is not None and float(v) > float(max_v):
                    ok = False
            if ok:
                return True
        return False
