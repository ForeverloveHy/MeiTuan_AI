from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from .element_engine import ElementEngine, DialogueAtom, ElementMatch, match_side, parse_elements, text_hit
from .evidence_units import EvidenceUnit
from .schema import ConstraintRule

ConstraintVerdict = Literal["安全", "违规", "证据不足", "软问题"]


@dataclass(slots=True)
class ConstraintCheck:
    constraint_id: str
    node_id: str | None
    name: str
    severity: str
    verdict: ConstraintVerdict
    evidence: str
    turn_index: int | None
    reason: str
    aliases: list[str] | None = None
    enforcement: str = "hard"
    constraint_kind: str = ""
    evidence_flow: str = "negative_object_to_dialogue_scan"
    trigger_verdict: str = "miss"
    positive_verdict: str = "miss"
    negative_verdict: str = "miss"
    requires_arbitration: bool = False
    metrics: dict[str, Any] | None = None
    score: float | None = None
    element_audit: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "node_id": self.node_id,
            "name": self.name,
            "severity": self.severity,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "turn_index": self.turn_index,
            "reason": self.reason,
            "aliases": self.aliases or [],
            "enforcement": self.enforcement,
            "constraint_kind": self.constraint_kind,
            "evidence_flow": self.evidence_flow,
            "trigger_verdict": self.trigger_verdict,
            "positive_verdict": self.positive_verdict,
            "negative_verdict": self.negative_verdict,
            "requires_arbitration": self.requires_arbitration,
            "metrics": self.metrics or {},
            "score": self.score,
            "element_audit": self.element_audit or {},
        }


ConstraintEvent = ConstraintCheck


class ConstraintJudge:
    """Element-level hard/soft constraint executor.

    Hard constraints use target-driven logic:
    trigger_object -> dialogue atoms, then negative_object/negative_elements ->
    violation, with positive/safe side checked at the same time.  Soft
    constraints are global element statistics and only receive a small score
    effect.
    """

    def __init__(self, runtime: dict[str, Any] | None = None, engine: ElementEngine | None = None) -> None:
        self.runtime = runtime or {}
        self.engine = engine or ElementEngine()

    def judge(self, rules: list[ConstraintRule], units: list[EvidenceUnit], atoms: list[DialogueAtom] | None = None) -> list[ConstraintCheck]:
        dialogue_atoms = atoms or self.engine.build_atoms(units)
        checks: list[ConstraintCheck] = []
        for rule in rules:
            if str(rule.enforcement or "hard") == "soft":
                check = self._judge_soft(rule, dialogue_atoms)
                if check is not None:
                    checks.append(check)
                continue
            check = self._judge_hard(rule, dialogue_atoms)
            if check is not None:
                checks.append(check)
        return checks

    def _judge_hard(self, rule: ConstraintRule, atoms: list[DialogueAtom]) -> ConstraintCheck | None:
        if str(rule.constraint_kind or "") == "structural_metric":
            return self._judge_structural_metric(rule, atoms)
        trigger_needed = str(rule.trigger_policy or "self_sufficient") in {"requires_user_trigger", "context_dependent"}
        trigger = self._trigger_match(rule, atoms, required=trigger_needed)
        if trigger_needed:
            if trigger.verdict == "miss":
                return None
            if trigger.verdict == "review":
                return self._check(rule, "证据不足", trigger, ElementMatch("miss"), ElementMatch("miss"), "限制 trigger_object 部分命中，送审")
            candidate_atoms = [a for a in atoms if trigger.atom is None or a.turn_index > trigger.atom.turn_index]
        else:
            candidate_atoms = list(atoms)

        negative_elements = list(rule.negative_elements or rule.primary_elements or [])
        if not negative_elements and rule.negative_object:
            negative_elements = _elements_from_object(rule.negative_object)
        if not negative_elements and rule.prohibited:
            negative_elements = _elements_from_legacy_patterns(rule.prohibited)
        positive_elements = list(rule.positive_elements or [])
        if not positive_elements and rule.negative_object:
            positive_elements = _elements_from_object({"primary_elements": rule.negative_object.get("safe_exception_elements") or rule.negative_object.get("safe_elements") or []})
        if not positive_elements and rule.safe_context:
            positive_elements = _elements_from_legacy_patterns(rule.safe_context)
        neg_groups = list(getattr(rule, "negative_element_groups", []) or getattr(rule, "element_groups", []) or [])
        pos_groups = list(getattr(rule, "positive_element_groups", []) or [])
        if not negative_elements and not positive_elements and not neg_groups and not pos_groups:
            return ConstraintCheck(rule.id, rule.node_id, rule.name, rule.severity, "证据不足", "", None, "硬限制 atom 缺少正负元素组/元素规则，不能本地判定", aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind, requires_arbitration=True)

        scope = rule.detection_scope or {}
        default_speaker = str(scope.get("speaker") or "assistant")
        requested_speaker = _requested_speaker(negative_elements or positive_elements, default_speaker)
        candidate_atoms = _scope_atoms_by_speaker(candidate_atoms, requested_speaker)

        # Legacy executable constraint patterns are still schema-provided rules.
        # Preserve their all/any semantics instead of flattening them into one
        # oversized element list, which would make a correct violation look like
        # a low-ratio partial hit.
        legacy_negative = _legacy_constraint_match(rule.prohibited, candidate_atoms) if rule.prohibited else ElementMatch("miss")
        legacy_positive = _legacy_constraint_match(rule.safe_context, candidate_atoms) if rule.safe_context else ElementMatch("miss")
        if legacy_negative.verdict == "hit":
            same_atom_positive = legacy_positive.verdict == "hit" and legacy_positive.atom is legacy_negative.atom
            if not same_atom_positive:
                return self._check(rule, "违规", legacy_negative, legacy_positive, legacy_negative, "命中硬限制 legacy prohibited 结构规则，且同一候选内安全侧未命中", trigger)
        if legacy_positive.verdict == "hit" and legacy_negative.verdict == "miss" and not negative_elements:
            return self._check(rule, "安全", legacy_positive, legacy_positive, legacy_negative, "命中硬限制 legacy safe_context 结构规则，且负向规则未命中", trigger)

        negative = match_side(self.engine, rule.id, "constraint_negative", negative_elements, rule.secondary_elements, [], rule.match_policy, candidate_atoms, element_groups=neg_groups)
        # Safety/positive side only suppresses a violation when it binds to the
        # same candidate atom as the negative object. Otherwise an unrelated
        # negation elsewhere in the dialogue would mask a real violation.
        same_atom_atoms = [negative.atom] if negative.atom is not None else candidate_atoms
        positive = match_side(self.engine, rule.id, "constraint_positive", positive_elements, rule.secondary_elements, [], rule.match_policy, same_atom_atoms, element_groups=pos_groups) if (positive_elements or pos_groups) else ElementMatch("miss", reason="未声明安全/正向侧")
        if negative.verdict == "miss" and positive.verdict == "miss" and positive_elements:
            positive = match_side(self.engine, rule.id, "constraint_positive", positive_elements, rule.secondary_elements, [], rule.match_policy, candidate_atoms, element_groups=pos_groups)
        best = negative if negative.score >= positive.score else positive

        # Hard constraints are negative-object first.  A safety-side partial
        # match must not create an arbitration item when the forbidden object
        # itself is strictly absent.  This avoids sending safe sentences such as
        # a safe negated mechanism explanation to LLM merely because it
        # contains a generic negation.
        if positive.verdict == "hit" and negative.verdict in {"miss", "review"}:
            return self._check(rule, "安全", positive, positive, negative, "命中硬限制安全处理/拒绝越界表达，负向对象未形成严格违规", trigger)
        if negative.verdict == "miss":
            # Hard constraints must be considered and recorded, but absence of a
            # strict negative object is safe rather than a gray violation.
            return self._check(rule, "安全", positive if positive.atom else negative, positive, negative, "已扫描硬限制，负向对象未命中；候选与元素明细已记录", trigger)
        if negative.verdict == "hit" and positive.verdict == "hit" and any(getattr(e, "fact", False) for e in positive.primary_hits):
            return self._check(rule, "安全", positive, positive, negative, "同一候选内安全侧 fact 明确命中，按否定/安全开关覆盖负向表面词", trigger)
        if negative.verdict == "hit" and positive.verdict == "miss":
            return self._check(rule, "违规", negative, positive, negative, "命中硬限制负向对象，且同一候选内正向/安全侧严格未命中", trigger)
        if negative.verdict == "hit" and positive.verdict == "review":
            # Safety side must be strict.  Generic negation words inside a
            # forbidden claim such as “我保证结果没问题” only create a partial
            # positive/review and must not hide a deterministic violation.
            return self._check(rule, "违规", negative, positive, negative, "命中硬限制负向对象；安全侧仅为部分命中，不能抵消违规", trigger)
        if negative.verdict == "hit" and positive.verdict == "hit":
            return self._check(rule, "证据不足", best, positive, negative, "同一候选内正负侧同时命中，需仲裁确认作用域", trigger)
        if negative.verdict == "review":
            return self._check(rule, "安全", negative, positive, negative, "硬限制负向对象仅部分命中，未达到违规层；候选已记录但不作为违规/灰区扣分", trigger)
        return None

    def _trigger_match(self, rule: ConstraintRule, atoms: list[DialogueAtom], required: bool = False) -> ElementMatch:
        # Hard constraints are self_sufficient by default.  A trigger is only
        # required when trigger_policy explicitly says requires_user_trigger or
        # context_dependent.  A stray trigger_object on a self_sufficient rule is
        # treated as descriptive context and must not block scanning the negative object.
        if not required:
            return ElementMatch("hit", reason="self_sufficient 限制不需要 trigger")
        elements = list(getattr(rule, "trigger_object", {}).get("primary_elements", []) if isinstance(getattr(rule, "trigger_object", {}), dict) else [])
        if not elements and isinstance(rule.trigger_object, dict):
            elements = _elements_from_object(rule.trigger_object)
        if not elements and rule.trigger:
            elements = _elements_from_legacy_patterns(rule.trigger)
        if not elements:
            return ElementMatch("review", reason="该限制声明需要 trigger，但 schema 未提供 trigger_object/trigger 元素")
        secondary = getattr(rule, "trigger_object", {}).get("secondary_pools", {}) if isinstance(getattr(rule, "trigger_object", {}), dict) else {}
        trigger_atoms = _scope_atoms_by_speaker(atoms, _requested_speaker(elements, "user"))
        return match_side(self.engine, rule.id + ".trigger", "constraint_trigger", elements, secondary or rule.secondary_elements, [], rule.match_policy, trigger_atoms, element_groups=getattr(rule, "trigger_element_groups", []) or [])

    def _judge_structural_metric(self, rule: ConstraintRule, atoms: list[DialogueAtom]) -> ConstraintCheck | None:
        metric = dict(rule.metric or {})
        mtype = str(metric.get("type") or "").strip()
        if mtype == "max_chars_per_assistant_turn":
            return self._judge_max_chars(rule, atoms, metric)
        if mtype == "semantic_repetition_between_assistant_turns":
            return self._judge_repetition(rule, atoms, metric)
        return ConstraintCheck(
            rule.id, rule.node_id, rule.name, rule.severity, "证据不足", "", None,
            f"结构指标类型 {mtype or 'unknown'} 暂无本地执行器",
            aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind,
            evidence_flow="structural_metric", requires_arbitration=True, metrics=metric,
        )

    def _judge_max_chars(self, rule: ConstraintRule, atoms: list[DialogueAtom], metric: dict[str, Any]) -> ConstraintCheck | None:
        max_chars = int(metric.get("max_chars") or metric.get("max_length") or 30)
        assistant_turns: dict[int, str] = {}
        for atom in atoms:
            if atom.speaker != "assistant" or atom.span_type != "turn":
                continue
            assistant_turns.setdefault(atom.turn_index, atom.text)
        worst: tuple[int, str, int] | None = None
        for turn, text in assistant_turns.items():
            count = _count_cjk_chars(text)
            if count > max_chars and (worst is None or count > worst[2]):
                worst = (turn, text, count)
        if worst is None:
            return ConstraintCheck(
                rule.id, rule.node_id, rule.name, rule.severity, "安全", "", None,
                f"所有客服单轮回复均未超过 {max_chars} 字结构阈值",
                aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind,
                evidence_flow="structural_metric:max_chars_per_assistant_turn",
                metrics={"max_chars": max_chars, "violations": 0}, score=1.0,
            )
        turn, text, count = worst
        return ConstraintCheck(
            rule.id, rule.node_id, rule.name, rule.severity, "违规", text, turn,
            f"客服单轮回复约 {count} 字，超过 {max_chars} 字结构阈值",
            aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind,
            evidence_flow="structural_metric:max_chars_per_assistant_turn",
            negative_verdict="hit", positive_verdict="miss",
            metrics={"max_chars": max_chars, "observed_chars": count, "violations": 1}, score=0.0,
        )

    def _judge_repetition(self, rule: ConstraintRule, atoms: list[DialogueAtom], metric: dict[str, Any]) -> ConstraintCheck | None:
        threshold = float(metric.get("similarity_threshold") or 0.85)
        ignore_short = bool(metric.get("ignore_short_acknowledgement", True))
        turns = [(a.turn_index, a.text) for a in atoms if a.speaker == "assistant" and a.span_type == "turn"]
        best: tuple[float, int, str, int, str] | None = None
        for i in range(len(turns)):
            ti, ai = turns[i]
            ni = _norm_for_similarity(ai)
            if ignore_short and len(ni) < 8:
                continue
            for j in range(i + 1, min(len(turns), i + 4)):
                tj, aj = turns[j]
                nj = _norm_for_similarity(aj)
                if ignore_short and len(nj) < 8:
                    continue
                sim = _char_jaccard(ni, nj)
                if sim >= threshold and (best is None or sim > best[0]):
                    best = (sim, ti, ai, tj, aj)
        if best is None:
            return ConstraintCheck(
                rule.id, rule.node_id, rule.name, rule.severity, "安全", "", None,
                "未发现相邻或近邻客服回复的高相似重复",
                aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind,
                evidence_flow="structural_metric:semantic_repetition_between_assistant_turns",
                metrics={"similarity_threshold": threshold, "violations": 0}, score=1.0,
            )
        sim, ti, ai, tj, aj = best
        return ConstraintCheck(
            rule.id, rule.node_id, rule.name, rule.severity, "违规", f"{ai} / {aj}", tj,
            f"客服第 {ti} 与第 {tj} 轮回复相似度约 {sim:.2f}，构成重复回复",
            aliases=list(rule.aliases), enforcement="hard", constraint_kind=rule.constraint_kind,
            evidence_flow="structural_metric:semantic_repetition_between_assistant_turns",
            negative_verdict="hit", positive_verdict="miss",
            metrics={"similarity_threshold": threshold, "similarity": round(sim, 4), "turn_a": ti, "turn_b": tj, "violations": 1}, score=0.0,
        )

    def _judge_soft(self, rule: ConstraintRule, atoms: list[DialogueAtom]) -> ConstraintCheck | None:
        metric = dict(rule.metric or {})
        mtype = str(metric.get("type") or "").strip()
        if mtype in {"max_chars_per_assistant_turn", "semantic_repetition_between_assistant_turns"}:
            hard_like = self._judge_structural_metric(rule, atoms)
            if hard_like is None:
                return None
            verdict = "软问题" if hard_like.verdict == "违规" else "安全"
            return ConstraintCheck(
                hard_like.constraint_id, hard_like.node_id, hard_like.name, hard_like.severity,
                verdict, hard_like.evidence, hard_like.turn_index, hard_like.reason,
                aliases=hard_like.aliases, enforcement="soft", constraint_kind="fuzzy_quality",
                evidence_flow=hard_like.evidence_flow, metrics=hard_like.metrics, score=hard_like.score,
            )
        elements = list(rule.global_elements or rule.primary_elements)
        if not elements and isinstance(rule.soft_rule, dict):
            elements = list(rule.soft_rule.get("global_elements") or rule.soft_rule.get("quality_elements") or [])
        if not elements:
            return None
        parsed = parse_elements(elements)
        group_rows = list(getattr(rule, "element_groups", []) or getattr(rule, "positive_element_groups", []) or [])
        if not parsed and not group_rows:
            return None
        assistant_atoms = [a for a in atoms if a.speaker == "assistant"]
        denom = max(1, len(assistant_atoms))
        hits: list[DialogueAtom] = []
        for atom in assistant_atoms:
            flat_hit = any(atom.has_element(e, rule.secondary_elements) for e in parsed)
            group_hit = False
            if group_rows:
                group_match = match_side(self.engine, rule.id + ".soft", "soft_global", [], rule.secondary_elements, [], rule.match_policy, [atom], element_groups=group_rows)
                group_hit = group_match.verdict in {"hit", "review"}
            if flat_hit or group_hit:
                hits.append(atom)
        ratio = len(hits) / denom
        metric = dict(rule.metric or {})
        direction = str(metric.get("direction") or metric.get("scoring_direction") or "higher_better")
        good = float(metric.get("good_threshold", metric.get("target_ratio", 0.25)))
        review = float(metric.get("review_threshold", 0.12))
        if direction in {"lower_better", "lower_is_better", "max", "upper_bound"}:
            soft_score_value = 1.0 if ratio <= good else max(0.0, 1.0 - (ratio - good) / max(1.0 - good, 0.01))
            ok = ratio <= good
            borderline = ratio <= max(good, review)
            high_reason = "软限制相关元素密度未超过建议上限"
            mid_reason = "软限制相关元素密度略高，作为小权重话术质量提示"
            low_reason = "软限制相关元素密度明显过高，作为小权重话术质量提示"
        elif direction in {"range", "range_better", "range_is_better"}:
            low = float(metric.get("min_threshold", metric.get("low_threshold", review)))
            high = float(metric.get("max_threshold", metric.get("high_threshold", good)))
            if low > high:
                low, high = high, low
            if low <= ratio <= high:
                soft_score_value = 1.0
                ok = True
            else:
                distance = min(abs(ratio - low), abs(ratio - high))
                soft_score_value = max(0.0, 1.0 - distance / max(high - low, 0.01))
                ok = False
            borderline = soft_score_value >= 0.6
            high_reason = "软限制相关元素比例处于建议区间"
            mid_reason = "软限制相关元素比例略偏离建议区间，作为小权重话术质量提示"
            low_reason = "软限制相关元素比例明显偏离建议区间，作为小权重话术质量提示"
        else:
            soft_score_value = min(1.0, ratio / max(good, 0.01))
            ok = ratio >= good
            borderline = ratio >= review
            high_reason = "软限制相关元素比例达到建议阈值"
            mid_reason = "软限制相关元素比例偏低，作为小权重话术质量提示"
            low_reason = "软限制相关元素明显不足，作为小权重话术质量提示"
        score = soft_score_value
        if ok:
            verdict: ConstraintVerdict = "安全"
            reason = high_reason
        elif borderline:
            verdict = "软问题"
            reason = mid_reason
        else:
            verdict = "软问题"
            reason = low_reason
        evidence = " / ".join(a.text for a in hits[:3])
        return ConstraintCheck(
            rule.id,
            rule.node_id,
            rule.name,
            rule.severity,
            verdict,
            evidence,
            hits[0].turn_index if hits else None,
            reason,
            aliases=list(rule.aliases),
            enforcement="soft",
            constraint_kind=rule.constraint_kind or "fuzzy_quality",
            evidence_flow="soft_global_element_statistics",
            metrics={"matching_atoms": len(hits), "assistant_atoms": denom, "ratio": round(ratio, 4), "good_threshold": good, "direction": direction},
            score=round(score, 4),
        )

    def _check(self, rule: ConstraintRule, verdict: ConstraintVerdict, basis: ElementMatch, positive: ElementMatch, negative: ElementMatch, reason: str, trigger: ElementMatch | None = None) -> ConstraintCheck:
        atom = basis.atom or positive.atom or negative.atom or (trigger.atom if trigger else None)
        return ConstraintCheck(
            rule.id,
            rule.node_id,
            rule.name,
            rule.severity,
            verdict,
            atom.text if atom else "",
            atom.turn_index if atom else None,
            reason,
            aliases=list(rule.aliases),
            enforcement="hard",
            constraint_kind=rule.constraint_kind,
            trigger_verdict=(trigger.verdict if trigger else "hit"),
            positive_verdict=positive.verdict,
            negative_verdict=negative.verdict,
            requires_arbitration=verdict == "证据不足",
            element_audit={"trigger": _match_audit(trigger) if trigger else {}, "safe_side": _match_audit(positive), "negative_side": _match_audit(negative)},
        )


def _match_audit(match: ElementMatch | None) -> dict[str, Any]:
    if match is None:
        return {}
    def elem(e: Any) -> dict[str, Any]:
        return {
            "value": getattr(e, "value", ""),
            "main": bool(getattr(e, "required", False)),
            "fact": bool(getattr(e, "fact", False)),
            "pool": list(getattr(e, "secondary_pool", []) or []),
            "type": getattr(e, "type", "surface"),
        }
    return {
        "verdict": match.verdict,
        "score": round(float(match.score or 0.0), 4),
        "reason": match.reason,
        "hit_elements": [elem(e) for e in match.primary_hits],
        "missing_elements": [elem(e) for e in match.missing],
        "candidate_results": list(getattr(match, "candidate_results", []) or []),
    }



def _count_cjk_chars(text: str) -> int:
    # Count meaningful characters rather than bytes; spaces and common punctuation
    # do not dominate Chinese phone-call length limits.
    return len(re.sub(r"[\s，,。！？!?；;：:、'\"“”‘’（）()\[\]{}<>《》]", "", str(text or "")))


def _norm_for_similarity(text: str) -> str:
    return re.sub(r"[\s，,。！？!?；;：:、'\"“”‘’（）()\[\]{}<>《》]", "", str(text or "").lower())


def _char_jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def _legacy_constraint_match(patterns: list[dict[str, Any]] | None, atoms: list[DialogueAtom]) -> ElementMatch:
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        speaker = str(pat.get("speaker") or "").strip()
        all_terms = [str(x).strip() for x in pat.get("all") or [] if str(x or "").strip()]
        any_terms = [str(x).strip() for x in pat.get("any") or [] if str(x or "").strip()]
        regex_terms = [str(x).strip() for x in pat.get("regex_any") or [] if str(x or "").strip()]
        for atom in atoms:
            if speaker and speaker not in {"any", "all", "both"} and atom.speaker != speaker:
                continue
            if all_terms and not all(text_hit(term, atom.text) for term in all_terms):
                continue
            if any_terms and not any(text_hit(term, atom.text) for term in any_terms):
                continue
            if regex_terms:
                ok = False
                for pattern in regex_terms:
                    try:
                        ok = bool(re.search(pattern, atom.text))
                    except re.error:
                        ok = False
                    if ok:
                        break
                if not ok:
                    continue
            if not all_terms and not any_terms and not regex_terms:
                continue
            return ElementMatch("hit", 1.0, atom=atom, reason="命中 schema legacy constraint pattern")
    return ElementMatch("miss", reason="legacy constraint pattern 未命中")


def _requested_speaker(elements: list[dict[str, Any]] | None, default: str | None) -> str | None:
    for item in elements or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or item.get("element_type") or "")
        if typ in {"speaker_role", "discourse_role"}:
            value = str(item.get("value") or item.get("name") or item.get("text") or "").lower().strip()
            if value in {"assistant", "agent", "客服"}:
                return "assistant"
            if value in {"user", "customer", "用户"}:
                return "user"
            if value in {"any", "all", "both", "任意", "双方"}:
                return None
    return default


def _scope_atoms_by_speaker(atoms: list[DialogueAtom], speaker: str | None) -> list[DialogueAtom]:
    if speaker is None or str(speaker).lower() in {"", "any", "all", "both", "任意", "双方"}:
        return list(atoms or [])
    return [a for a in (atoms or []) if getattr(a, "speaker", None) == speaker]

def _elements_from_object(obj: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(obj, dict):
        return []
    out: list[dict[str, Any]] = []
    for key in ("primary_elements", "required_elements", "elements"):
        val = obj.get(key)
        if isinstance(val, list):
            out.extend(x for x in val if isinstance(x, dict))
    for key in ("forbidden_surface_forms", "surface_forms", "semantic_equivalents", "trigger_context", "forbidden_action", "required_safe_action"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            out.append({"type": "surface", "value": val.strip()})
        elif isinstance(val, list):
            out.extend({"type": "surface", "value": str(x)} for x in val if str(x or "").strip())
    return out


def _elements_from_legacy_patterns(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        for key in ("all", "any"):
            for val in pat.get(key) or []:
                if str(val or "").strip():
                    out.append({"type": "surface", "value": str(val).strip()})
    return out
