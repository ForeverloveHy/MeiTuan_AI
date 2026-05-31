from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
import re

from .evidence_matcher import EvidenceMatcher
from .evidence_units import EvidenceUnit
from .schema import ConstraintRule
from .generic_customer_service_expressions import (
    HARD_TERMINAL_CUES,
    TURN_MANAGEMENT_CUES,
    USER_CONTINUE_CUES,
    USER_STOP_CUES,
)


ConstraintVerdict = Literal["安全", "违规", "证据不足"]


def _compact(text: object) -> str:
    return "".join(str(text or "").lower().split())


def _as_patterns(values: Any) -> list[dict[str, Any]]:
    """Normalize schema scope entries into matcher patterns.

    The schema can use either raw strings or normal evidence-pattern dicts.  This
    lets LongCat express violation_scope in the same style as existing trigger /
    prohibited / safe_context fields while keeping the local executor generic.
    """
    out: list[dict[str, Any]] = []
    for item in values or []:
        if isinstance(item, dict):
            out.append(dict(item))
        else:
            s = str(item or "").strip()
            if s:
                out.append({"any": [s]})
    return out


def _pattern_values(patterns: Any) -> list[str]:
    out: list[str] = []
    for pat in _as_patterns(patterns):
        for key in ("any", "all", "regex_any"):
            out.extend(str(x or "") for x in pat.get(key, []) or [])
        if pat.get("reason"):
            out.append(str(pat.get("reason")))
    return [x for x in out if x.strip()]


def _schema_values(rule: ConstraintRule) -> list[str]:
    values: list[str] = [rule.id, rule.name, rule.description, *getattr(rule, "aliases", [])]
    for seq in (rule.trigger, rule.safe_context, rule.prohibited, getattr(rule, "unresolved", []) or []):
        values.extend(_pattern_values(seq))
    scope = getattr(rule, "violation_scope", {}) or {}
    for key in ("protected_objects", "forbidden_actions", "safe_actions", "ambiguous_zone", "trigger_scope"):
        values.extend(_pattern_values(scope.get(key) or []))
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        c = _compact(v)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# These are language operators, not business objects.  They are deliberately
# small and only help identify speech acts; concrete business boundaries must
# come from constraint.violation_scope or the explicit constraint patterns.
_EXPLICIT_BOUNDARY_DENIALS = (
    "不能", "无法", "不可以", "不保证", "不能保证", "无法保证", "不承诺", "不能承诺",
)
_RULE_QUALIFIERS = ("按规则", "按规定", "以页面", "以系统", "以实际展示")
_BOUNDARY_NEGATIONS = (*_EXPLICIT_BOUNDARY_DENIALS, *_RULE_QUALIFIERS)
_COMMITMENT_OPERATORS = (
    "保证", "确保", "一定", "肯定", "承诺", "帮你", "帮您", "我帮", "我来",
    "替你", "替您", "安排", "协调", "申请", "争取", "私下", "直接给",
)
_PRESSURE_OPERATORS = (
    "必须", "一定要", "不能不", "不做不行", "强制", "先听我", "听我说完",
    "否则", "不然", "要不然", "后果", "急也", "忙也",
)
def _has_explicit_boundary_denial(text: str) -> bool:
    t = _compact(text)
    return any(_compact(x) in t for x in _EXPLICIT_BOUNDARY_DENIALS)


def _has_rule_qualifier(text: str) -> bool:
    t = _compact(text)
    return any(_compact(x) in t for x in _RULE_QUALIFIERS)


def _has_active_commitment_or_pressure(text: str, commitment_ops: tuple[str, ...] = _COMMITMENT_OPERATORS, pressure_ops: tuple[str, ...] = _PRESSURE_OPERATORS) -> bool:
    # A boundary denial such as "不能承诺 X" is an explicit refusal, not an
    # active commitment.  Rule qualifiers such as "按规则" are weaker: if they
    # appear together with "帮您/申请/保证" they must not suppress violations.
    if _has_explicit_boundary_denial(text):
        return False
    return _contains_operator(text, commitment_ops) or _contains_operator(text, pressure_ops)


def _looks_like_safe_boundary(text: str) -> bool:
    t = _compact(text)
    return any(_compact(x) in t for x in _BOUNDARY_NEGATIONS)


def _safe_boundary_suppresses_violation(text: str, commitment_ops: tuple[str, ...] = _COMMITMENT_OPERATORS, pressure_ops: tuple[str, ...] = _PRESSURE_OPERATORS) -> bool:
    if _has_explicit_boundary_denial(text):
        return True
    if _has_rule_qualifier(text) and not _has_active_commitment_or_pressure(text, commitment_ops, pressure_ops):
        return True
    return False


def _looks_like_rule_explanation(text: str) -> bool:
    """Suppress false positives for neutral rule explanations.

    This does not contain task nouns.  It distinguishes factual explanation from
    imperatives/threats by speech-act cues.
    """
    t = _compact(text)
    if not t:
        return False
    if any(x in t for x in ("你必须", "您必须", "必须马上", "马上", "赶紧", "否则", "不然", "要不然", "后果")):
        return False
    return any(x in t for x in ("是指", "指的是", "意思是", "规则是", "要求是", "条件是", "规则", "条件", "说明", "需要在", "需在", "达到", "满足", "生效"))


def _user_allows_continue(text: str) -> bool:
    t = _compact(text)
    if not t:
        return False
    if any(x in t for x in USER_STOP_CUES):
        return False
    return any(x in t for x in USER_CONTINUE_CUES)


def _is_turn_management_rule(rule: ConstraintRule) -> bool:
    schema_text = _compact(" ".join(_schema_values(rule)))
    return any(_compact(x) in schema_text for x in TURN_MANAGEMENT_CUES)


def _allow_continue_reset_for_rule(rule: ConstraintRule) -> bool:
    if not _is_turn_management_rule(rule):
        return False
    schema_text = _compact(" ".join(_schema_values(rule)))
    if str(getattr(rule, "severity", "") or "").lower() in {"critical", "high"} and any(_compact(x) in schema_text for x in HARD_TERMINAL_CUES):
        return False
    return True


def _contains_operator(text: str, operators: tuple[str, ...]) -> bool:
    t = _compact(text)
    return any(_compact(x) in t for x in operators)


def _pattern_has_action_operator(pattern: dict[str, Any]) -> bool:
    # Only inspect the pattern's executable matching fields.  LongCat often puts
    # an action word in `reason` (for example "禁止承诺 X") while the actual
    # prohibited pattern is object-only (`all: [X]`).  A safe boundary sentence
    # such as "不能承诺 X" must not be treated as an explicit violation merely
    # because the reason contains "承诺".
    values: list[str] = []
    for key in ("any", "all", "regex_any"):
        values.extend(str(x or "") for x in pattern.get(key, []) or [])
    text = _compact(" ".join(values))
    # These are generic speech-act/action operators, not business objects.
    return any(_compact(x) in text for x in (*_COMMITMENT_OPERATORS, *_PRESSURE_OPERATORS, "承诺", "保证", "一定", "肯定"))


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
        }


ConstraintEvent = ConstraintCheck


class ConstraintJudge:
    """Schema-driven constraint executor.

    Business boundaries belong to schema fields:
    - prohibited / safe_context / trigger / unresolved for exact evidence rules;
    - violation_scope.protected_objects / forbidden_actions / safe_actions /
      ambiguous_zone for structured grey-zone execution.

    Python only supplies small Chinese speech-act operators and turn-management
    logic.  It does not contain domain nouns such as products, roles, rankings,
    fees, entries, or operation targets.
    """

    def __init__(self, runtime: dict[str, Any] | None = None) -> None:
        self.matcher = EvidenceMatcher()
        # Keep a minimal extension point for language operators only.  The old
        # constraint_generic_fallback business marker dictionary is intentionally
        # ignored; scope nouns must be generated into schema.violation_scope.
        operators = dict((runtime or {}).get("constraint_language_operators") or {})
        self.commitment_operators = tuple(operators.get("commitment") or _COMMITMENT_OPERATORS)
        self.pressure_operators = tuple(operators.get("pressure") or _PRESSURE_OPERATORS)

    def judge(self, rules: list[ConstraintRule], units: list[EvidenceUnit]) -> list[ConstraintCheck]:
        checks: list[ConstraintCheck] = []
        assistant_units = [u for u in units if u.speaker == "assistant"]
        for rule in rules:
            trigger_units = self._trigger_units(rule, units)
            produced = False
            safe_turns: set[int] = set()
            trigger_active = bool(trigger_units) or not rule.trigger
            first_trigger_turn = min((u.turn_index for u in trigger_units), default=None)
            rule_is_contextual = bool(rule.trigger)

            if trigger_active:
                for unit in assistant_units:
                    if self._is_safe(rule, unit, units):
                        safe_turns.add(unit.turn_index)
                        checks.append(ConstraintCheck(rule.id, rule.node_id, rule.name, rule.severity, "安全", unit.text, unit.turn_index, "命中限制 schema 的安全处理范围", aliases=self._rule_aliases(rule)))
                        produced = True

            emitted_violation = False
            for unit in assistant_units:
                if unit.turn_index in safe_turns:
                    continue
                if self._is_context_blocked(rule, rule_is_contextual, first_trigger_turn, unit, units):
                    continue

                reason = self._explicit_prohibited_reason(rule, unit, units, trigger_units, first_trigger_turn)
                if not reason:
                    reason = self._violation_scope_reason(rule, unit, units, trigger_units)
                if reason:
                    checks.append(ConstraintCheck(rule.id, rule.node_id, rule.name, rule.severity, "违规", unit.text, unit.turn_index, reason, aliases=self._rule_aliases(rule)))
                    produced = True
                    emitted_violation = True
                    if not getattr(rule, "allow_multiple", False):
                        break

            unresolved_patterns = list(getattr(rule, "unresolved", []) or []) + _as_patterns((getattr(rule, "violation_scope", {}) or {}).get("ambiguous_zone") or [])
            if trigger_active or unresolved_patterns:
                for unit in units:
                    if unresolved_patterns and any(self.matcher._match_pattern(pat, unit, units) for pat in unresolved_patterns):
                        checks.append(ConstraintCheck(rule.id, rule.node_id, rule.name, rule.severity, "证据不足", unit.text, unit.turn_index, "命中限制 schema 的暧昧区，需语义仲裁", aliases=self._rule_aliases(rule)))
                        produced = True
                        break
            if trigger_units and not produced and getattr(rule, "requires_resolution", False):
                u = trigger_units[0]
                checks.append(ConstraintCheck(rule.id, rule.node_id, rule.name, rule.severity, "证据不足", u.text, u.turn_index, "限制触发条件出现，但没有找到安全处理或违规证据", aliases=self._rule_aliases(rule)))
        return checks

    def _is_context_blocked(self, rule: ConstraintRule, rule_is_contextual: bool, first_trigger_turn: int | None, unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        if rule_is_contextual and first_trigger_turn is not None and unit.turn_index <= first_trigger_turn:
            return True
        if rule_is_contextual and not self._context_still_active(first_trigger_turn, unit, units, allow_continue_reset=_allow_continue_reset_for_rule(rule)):
            return True
        return False

    def _explicit_prohibited_reason(self, rule: ConstraintRule, unit: EvidenceUnit, units: list[EvidenceUnit], trigger_units: list[EvidenceUnit], first_trigger_turn: int | None) -> str | None:
        rule_is_contextual = bool(rule.trigger)
        for pat in rule.prohibited:
            requires_trigger = bool(pat.get("requires_trigger")) or (rule_is_contextual and not bool(pat.get("self_sufficient")))
            if requires_trigger and not trigger_units:
                continue
            if requires_trigger and first_trigger_turn is not None and unit.turn_index <= first_trigger_turn:
                continue
            if requires_trigger and not self._context_still_active(first_trigger_turn, unit, units, allow_continue_reset=_allow_continue_reset_for_rule(rule)):
                continue
            matched_prohibited = self.matcher._match_pattern(pat, unit, units)
            if not matched_prohibited:
                continue
            # A generic safe-boundary cue such as "不能/无法" should not mask an
            # explicit self-sufficient prohibition emitted by the schema itself
            # (for example, a coercive "不能停止/不能拒绝" speech act).  Concrete
            # business content is still entirely supplied by the schema pattern.
            if _safe_boundary_suppresses_violation(unit.text, self.commitment_operators, self.pressure_operators):
                # LongCat sometimes marks object-only patterns such as
                # {all:[X], self_sufficient:true} as prohibited.  A sentence
                # like "不能承诺 X" is a boundary explanation, not a promise.
                # Keep self-sufficient hits only when the prohibited pattern
                # itself contains a generic action/operator.
                if (not bool(pat.get("self_sufficient"))) or (not _pattern_has_action_operator(pat)):
                    continue
            if _looks_like_rule_explanation(unit.text) and not bool(pat.get("self_sufficient")):
                continue
            return str(pat.get("reason") or rule.description or "命中限制 schema 的禁止范围")
        return None

    def _violation_scope_reason(self, rule: ConstraintRule, unit: EvidenceUnit, units: list[EvidenceUnit], trigger_units: list[EvidenceUnit]) -> str | None:
        if unit.speaker != "assistant":
            return None
        scope = getattr(rule, "violation_scope", {}) or {}
        protected = _as_patterns(scope.get("protected_objects") or [])
        forbidden = _as_patterns(scope.get("forbidden_actions") or [])
        safe = _as_patterns(scope.get("safe_actions") or [])
        if not (protected or forbidden or safe or rule.prohibited or rule.safe_context):
            return None
        if _safe_boundary_suppresses_violation(unit.text, self.commitment_operators, self.pressure_operators) or self._scope_match(safe, unit, units):
            return None
        object_hit = self._scope_match(protected, unit, units) if protected else (
            self._explicit_schema_anchor_hit(rule, unit)
            or self._explicit_pattern_anchor_hit(rule, unit, units)
        )
        forbidden_hit = self._scope_match(forbidden, unit, units)
        # Generic speech-act operators only strengthen action matching when the
        # schema text says the rule is about that action family and there is a
        # schema-derived object/anchor in the assistant sentence.
        operator_hit = False
        if object_hit:
            schema_text = _compact(" ".join([*(_pattern_values(forbidden)), *_schema_values(rule)]))
            if any(_compact(x) in schema_text for x in ("承诺", "保证", "干预", "补偿", "代操作", "处理", "结果")):
                operator_hit = _contains_operator(unit.text, self.commitment_operators)
            if any(_compact(x) in schema_text for x in ("强迫", "施压", "必须", "继续")):
                operator_hit = operator_hit or _contains_operator(unit.text, self.pressure_operators)
        if object_hit and (forbidden_hit or operator_hit):
            return "命中 schema 化违例范围：受保护对象 + 禁止动作"
        if trigger_units and forbidden_hit and _contains_operator(unit.text, self.pressure_operators):
            return "命中 schema 化违例范围：触发后出现禁止施压动作"
        return None

    def _scope_match(self, patterns: list[dict[str, Any]], unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        return any(self.matcher._match_pattern(pat, unit, units) for pat in patterns)

    def _explicit_schema_anchor_hit(self, rule: ConstraintRule, unit: EvidenceUnit) -> bool:
        text = _compact(unit.text)
        if not text:
            return False
        for value in _schema_values(rule):
            if len(value) >= 3 and value in text:
                return True
        return False

    def _explicit_pattern_anchor_hit(self, rule: ConstraintRule, unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        """Use only this constraint's own schema patterns as fallback anchors.

        Some older graphs put the protected object in safe_context/prohibited
        instead of violation_scope.protected_objects.  A broad safe_context match
        must not by itself declare safety when the same utterance is a commitment;
        however it is still a schema-provided object anchor for the structured
        violation check.
        """
        patterns: list[dict[str, Any]] = []
        patterns.extend(_as_patterns(rule.safe_context))
        patterns.extend(_as_patterns(rule.prohibited))
        return any(self.matcher._match_pattern(pat, unit, units) for pat in patterns)

    def _context_still_active(self, first_trigger_turn: int | None, unit: EvidenceUnit, units: list[EvidenceUnit], allow_continue_reset: bool = True) -> bool:
        if first_trigger_turn is None:
            return True
        if unit.turn_index <= first_trigger_turn:
            return False
        if allow_continue_reset:
            for u in units:
                if u.speaker == "user" and first_trigger_turn < u.turn_index < unit.turn_index and _user_allows_continue(u.text):
                    return False
        return True

    def _rule_aliases(self, rule: ConstraintRule) -> list[str]:
        values = [rule.id, rule.name, *getattr(rule, "aliases", [])]
        out: list[str] = []
        seen: set[str] = set()
        for v in values:
            s = str(v or "")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _trigger_units(self, rule: ConstraintRule, units: list[EvidenceUnit]) -> list[EvidenceUnit]:
        if not rule.trigger:
            return []
        return [unit for unit in units if unit.speaker == "user" and any(self.matcher._match_pattern(pat, unit, units) for pat in rule.trigger)]

    def _is_safe(self, rule: ConstraintRule, unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        if not any(self.matcher._match_pattern(pat, unit, units) for pat in rule.safe_context):
            scope = getattr(rule, "violation_scope", {}) or {}
            if not self._scope_match(_as_patterns(scope.get("safe_actions") or []), unit, units):
                return False
        if _has_active_commitment_or_pressure(unit.text, self.commitment_operators, self.pressure_operators):
            # A broad safe object or a weak "按规则" qualifier cannot override a
            # commitment/action sentence.  Explicit denial such as "不能承诺" is
            # still safe.
            return False
        return True
