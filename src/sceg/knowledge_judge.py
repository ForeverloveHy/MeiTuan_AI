from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from .element_engine import ElementEngine, DialogueAtom, ElementMatch, SemanticElement, match_side, text_hit, compact, _drop_cn_modal_fillers
from .evidence_units import EvidenceUnit
from .schema import KnowledgeItem

KnowledgeVerdict = Literal["支持", "冲突", "证据不足", "未提及"]


def _value_check_has_comparable_runtime(vc: Any) -> bool:
    if not isinstance(vc, dict) or not vc:
        return False
    candidates = []
    for value, unit in [(vc.get("expected_value"), vc.get("unit")), (vc.get("expected"), vc.get("unit")), (vc.get("normalized_expected"), vc.get("unit"))]:
        candidates.append((value, unit))
    checks = vc.get("checks") or vc.get("value_checks") or []
    if isinstance(checks, dict):
        checks = [checks]
    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, dict):
                for value in [c.get("expected_value"), c.get("expected"), c.get("normalized_expected")]:
                    candidates.append((value, c.get("unit") or vc.get("unit")))
    for value, unit in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        if _build_fact_profile(text, str(unit or "")).get("has_comparable"):
            return True
    return False


@dataclass(slots=True)
class KnowledgeCheck:
    knowledge_id: str
    node_id: str | None
    name: str
    severity: str
    verdict: KnowledgeVerdict
    evidence: str
    turn_index: int | None
    reason: str
    claim_id: str | None = None
    aliases: list[str] | None = None
    evidence_flow: str = "dialogue_claim_to_knowledge_table"
    positive_verdict: str = "miss"
    negative_verdict: str = "miss"
    requires_arbitration: bool = False
    element_audit: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "node_id": self.node_id,
            "name": self.name,
            "severity": self.severity,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "turn_index": self.turn_index,
            "reason": self.reason,
            "claim_id": self.claim_id,
            "aliases": self.aliases or [],
            "evidence_flow": self.evidence_flow,
            "positive_verdict": self.positive_verdict,
            "negative_verdict": self.negative_verdict,
            "requires_arbitration": self.requires_arbitration,
            "element_audit": self.element_audit or {},
        }


KnowledgeEvent = KnowledgeCheck


class KnowledgeJudge:
    """Element-level knowledge verification.

    Knowledge is not a target-to-evidence completion check.  It starts from
    assistant dialogue atoms and compares their elements against each knowledge
    atom's positive and negative sides.  A deterministic support/refute result
    requires both sides to be checked: positive hit + negative strict miss means
    support; negative hit + positive strict miss means conflict; any mixed,
    partial, or negation-scope case goes to review.  The negative-object
    layer is intentionally not used here; it belongs to hard constraints only.
    """

    def __init__(self, engine: ElementEngine | None = None) -> None:
        self.engine = engine or ElementEngine()

    def judge(self, items: list[KnowledgeItem], units: list[EvidenceUnit], atoms: list[DialogueAtom] | None = None) -> list[KnowledgeCheck]:
        dialogue_atoms = atoms or self.engine.build_atoms(units)
        assistant_atoms = [a for a in dialogue_atoms if a.speaker == "assistant"]
        checks: list[KnowledgeCheck] = []
        for item in items:
            check = self._judge_item(item, assistant_atoms)
            if check is not None:
                checks.append(check)
        return checks

    def _judge_item(self, item: KnowledgeItem, atoms: list[DialogueAtom]) -> KnowledgeCheck | None:
        selector_groups = list(getattr(item, "selector_element_groups", []) or [])
        correct_groups = list(getattr(item, "correct_element_groups", []) or getattr(item, "positive_element_groups", []) or getattr(item, "element_groups", []) or [])
        wrong_groups = list(getattr(item, "wrong_element_groups", []) or getattr(item, "negative_element_groups", []) or [])
        pos_elements = list(item.positive_elements or item.primary_elements or [])
        neg_elements = list(item.negative_elements or [])
        if _value_check_has_comparable_runtime(getattr(item, "value_check", {})):
            # Comparable facts must be judged by value_check only.  This prevents
            # topic-selector hits plus model-emitted wrong_groups from turning
            # correct numeric/time statements into conflicts.
            wrong_groups = []
            neg_elements = []

        # New knowledge flow: dialogue atoms select knowledge atoms by selector;
        # only selected facts are checked against correct/wrong value sides.
        if selector_groups:
            selector = match_side(self.engine, item.id + ".selector", "knowledge_selector", [], item.secondary_elements, [], item.match_policy, atoms, element_groups=selector_groups)
            if selector.verdict == "miss":
                # Comparable / directional negative samples often state the
                # target with a shorthand object. Do not create a verdict from
                # labels; still run the graph's own value/direction checks over
                # assistant atoms with strict object/value trunk binding.
                value_based = self._judge_value_check(item, atoms)
                if value_based is not None:
                    return value_based
                directional_based = self._judge_directional_fact_conflict(item, atoms)
                if directional_based is not None:
                    return directional_based
                if wrong_groups:
                    negative_all = match_side(self.engine, item.id + ".wrong_all", "knowledge_negative", neg_elements, item.secondary_elements, [], _side_policy(item.match_policy, neg_elements), atoms, element_groups=wrong_groups)
                    negative_all = _downgrade_missing_strong_flip(negative_all, neg_elements)
                    if negative_all.verdict == "hit":
                        best_atom = negative_all.atom
                        return self._check(item, "冲突", best_atom.text if best_atom else "", best_atom.turn_index if best_atom else None, ElementMatch("miss"), negative_all, "错误值元素组在全局 assistant 话语中命中；selector 未命中但 wrong side 自带对象主干")
                return None
            selected_atoms = self._knowledge_value_scope(selector, atoms)
            positive = match_side(self.engine, item.id + ".correct", "knowledge_positive", pos_elements, item.secondary_elements, [], item.match_policy, selected_atoms, element_groups=correct_groups) if (pos_elements or correct_groups) else ElementMatch("miss")
            negative = match_side(self.engine, item.id + ".wrong", "knowledge_negative", neg_elements, item.secondary_elements, [], _side_policy(item.match_policy, neg_elements), selected_atoms, element_groups=wrong_groups) if (neg_elements or wrong_groups) else self._negation_probe(item, selected_atoms, positive)
            if negative.verdict == "miss" and wrong_groups:
                carried = _contextual_fact_side_match(self.engine, item.id + ".wrong_ctx", wrong_groups, selected_atoms)
                if carried.verdict != "miss":
                    negative = carried
                else:
                    negative_all = match_side(self.engine, item.id + ".wrong_all", "knowledge_negative", neg_elements, item.secondary_elements, [], _side_policy(item.match_policy, neg_elements), atoms, element_groups=wrong_groups)
                    negative_all = _downgrade_missing_strong_flip(negative_all, neg_elements)
                    if negative_all.verdict == "hit":
                        negative = negative_all
            if selector.verdict == "review" and positive.verdict == "miss" and negative.verdict == "miss":
                # Review-level selectors are kept as local gray candidates only
                # when a value side also has evidence.  This lets the second
                # filter/LLM see true ambiguity, but avoids flooding it with
                # topic-only mentions.
                return None
            negative = _downgrade_missing_strong_flip(negative, neg_elements)
            if negative.verdict == "hit":
                best_atom = negative.atom or positive.atom or selector.atom
            elif positive.verdict == "hit":
                best_atom = positive.atom or negative.atom or selector.atom
            elif negative.verdict == "review":
                best_atom = negative.atom or positive.atom or selector.atom
            else:
                best_atom = positive.atom or negative.atom or selector.atom
            evidence = best_atom.text if best_atom else ""
            turn_index = best_atom.turn_index if best_atom else None
            # A stated wrong fact is a local conflict even if another nearby
            # utterance also states the correct fact.  The evaluator judges
            # whether an erroneous customer-service claim appeared, not whether
            # the transcript contains any correct sibling statement.
            if negative.verdict == "hit":
                return self._check(item, "冲突", evidence, turn_index, positive, negative, "对话 atom 命中知识 selector，且错误值元素组达本地命中层")
            # A positive element group may be surface-hit inside a negated claim
            # such as “对象A不适合场景B”.  Before declaring support, run the
            # graph-driven value/direction checks over the selected local scope.
            # This keeps positive wording from masking an explicit wrong fact.
            value_based = self._judge_value_check(item, selected_atoms)
            if value_based is not None and value_based.verdict == "冲突":
                return value_based
            directional_based = self._judge_directional_fact_conflict(item, selected_atoms)
            if directional_based is not None:
                return directional_based
            if positive.verdict == "hit" and negative.verdict == "miss":
                if value_based is not None:
                    return value_based
                return self._check(item, "支持", evidence, turn_index, positive, negative, "对话 atom 命中知识 selector，且正确值元素组达本地命中层")
            if selector.verdict == "review" or positive.verdict == "review" or negative.verdict == "review":
                return self._check(item, "证据不足", evidence, turn_index, positive, negative, "知识 atom 命中仲裁层但未达到本地确定命中层")
            return None

        # Legacy positive/negative side flow, kept for old graphs.
        pos_groups = correct_groups
        neg_groups = wrong_groups
        if not pos_elements and not neg_elements and not pos_groups and not neg_groups:
            return KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "证据不足", "", None, "知识 atom 缺少正负元素组/元素规则，不能本地判定", aliases=list(item.aliases), requires_arbitration=True)
        positive = match_side(self.engine, item.id, "knowledge_positive", pos_elements, item.secondary_elements, [], item.match_policy, atoms, element_groups=pos_groups)
        neg_policy = _side_policy(item.match_policy, neg_elements)
        negative = match_side(self.engine, item.id, "knowledge_negative", neg_elements, item.secondary_elements, [], neg_policy, atoms, element_groups=neg_groups) if (neg_elements or neg_groups) else self._negation_probe(item, atoms, positive)
        negative = _downgrade_missing_strong_flip(negative, neg_elements)
        if negative.verdict == "hit":
            best_atom = negative.atom or positive.atom
        elif positive.verdict == "hit":
            best_atom = positive.atom or negative.atom
        elif negative.verdict == "review":
            best_atom = negative.atom or positive.atom
        else:
            best_atom = positive.atom or negative.atom
        evidence = best_atom.text if best_atom else ""
        turn_index = best_atom.turn_index if best_atom else None
        p = positive.verdict
        n = negative.verdict
        if n == "hit":
            return self._check(item, "冲突", evidence, turn_index, positive, negative, "客服 factual claim 命中知识 atom 负向情况")
        value_based = self._judge_value_check(item, atoms)
        if value_based is not None and value_based.verdict == "冲突":
            return value_based
        directional_based_legacy = self._judge_directional_fact_conflict(item, atoms)
        if directional_based_legacy is not None:
            return directional_based_legacy
        if p == "hit" and n == "miss":
            if value_based is not None:
                return value_based
            return self._check(item, "支持", evidence, turn_index, positive, negative, "客服 factual claim 与知识 atom 正向元素一致，且负向元素严格未命中")
        if p == "miss" and n == "miss":
            return None
        if n == "miss" and p == "review":
            return None
        return self._check(item, "证据不足", evidence, turn_index, positive, negative, "知识正负两侧未形成稳定结论，送审")

    def _knowledge_value_scope(self, selector: ElementMatch, atoms: list[DialogueAtom]) -> list[DialogueAtom]:
        """Limit value verification to selector-confirmed local contexts.

        A knowledge selector may have many valid candidate atoms (e.g. every
        later mention of the same product option).  The old implementation kept only the
        single highest-ranked selector atom, so a later wrong fact could be
        invisible.  We now keep the small local window around every selector
        candidate that reached hit/review.  This is still schema-grounded: value
        sides must re-match their own main trunk and fact gates before support or
        conflict can be produced.
        """
        if selector.atom is None:
            return atoms
        turn_indices: set[int] = {selector.atom.turn_index}
        best_score = max([float(x.get("score") or 0.0) for x in (selector.candidate_results or [])] or [float(selector.score or 0.0)])
        for cand in selector.candidate_results or []:
            verdict = str(cand.get("verdict") or "")
            score = float(cand.get("score") or 0.0)
            ti = cand.get("turn_index")
            if verdict in {"hit", "review"} and isinstance(ti, int) and score >= max(0.42, best_score * 0.75):
                turn_indices.add(ti)
        scope: list[DialogueAtom] = []
        for ti in sorted(turn_indices):
            scope.extend(a for a in atoms if a.turn_index == ti)
            # Allow one following and one previous assistant turn for compact
            # factual continuations, but keep the window anchored to selector
            # candidates rather than the entire transcript.
            next_turn = min((a.turn_index for a in atoms if a.turn_index > ti), default=None)
            if next_turn is not None and next_turn <= ti + 2:
                scope.extend(a for a in atoms if a.turn_index == next_turn)
            prev_turn = max((a.turn_index for a in atoms if a.turn_index < ti), default=None)
            if prev_turn is not None and ti - prev_turn <= 2:
                scope.extend(a for a in atoms if a.turn_index == prev_turn)
        seen: set[str] = set()
        out: list[DialogueAtom] = []
        for a in scope:
            key = a.atom_id
            if key not in seen:
                seen.add(key)
                out.append(a)
        return out


    def _judge_value_check(self, item: KnowledgeItem, atoms: list[DialogueAtom]) -> KnowledgeCheck | None:
        """Generic comparable-fact verification for numbers/time/money/counts.

        This replaces numeric wrong-fact enumeration.  The graph should state the
        expected fact once in value_check (expected_value/expected + optional
        unit).  Runtime then extracts comparable facts from selector-confirmed
        local evidence and compares them with the expected fact under the same
        subject trunk.  A different number/time category under the same trunk is
        a deterministic conflict; the graph does not need to list all wrong
        numbers such as 8/10/12/15.
        """
        vc = dict(getattr(item, "value_check", {}) or {})
        # A knowledge atom may contain several comparable facts under the same
        # selector trunk, e.g. reward condition = 7 days + 10 orders + 1.5 yuan.
        # The graph should not enumerate numeric wrong facts.  It declares the
        # expected comparable slots once; runtime compares actual values by unit.
        raw_checks = vc.get("checks") or vc.get("value_checks") or []
        if isinstance(raw_checks, dict):
            raw_checks = [raw_checks]
        checks: list[dict[str, Any]] = [x for x in raw_checks if isinstance(x, dict)]
        if not checks:
            checks = [vc]
        comparable_checks: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for check in checks:
            expected = str(check.get("normalized_expected") or check.get("expected_value") or check.get("expected") or "").strip()
            unit = str(check.get("unit") or "").strip()
            if not expected:
                continue
            expected_profile = _build_fact_profile(expected, unit)
            if expected_profile.get("has_comparable"):
                comparable_checks.append((expected, unit, expected_profile, check))
        if not comparable_checks:
            return None

        base_elements = [
            e for e in (item.primary_elements or item.positive_elements or [])
            if isinstance(e, dict) and str(e.get("type") or "") not in {"value", "quantity", "time", "value_mismatch", "polarity", "modality"}
        ]
        if not base_elements:
            # New one-graph/two-table prompts put the subject trunk in selector_groups.
            # Use selector main elements only; do not include correct/wrong groups here,
            # otherwise a wrong number would be invisible because the correct fact is missing.
            for group in getattr(item, "selector_element_groups", []) or []:
                for erow in (group.get("elements") or []) if isinstance(group, dict) else []:
                    if isinstance(erow, dict) and bool(erow.get("main")) and not bool(erow.get("fact")):
                        base_elements.append(erow)
        if not base_elements:
            return None

        base_policy = dict(item.match_policy or {})
        base_policy["must_have"] = [
            x for x in base_policy.get("must_have", [])
            if x in {"business_target", "target"}
        ]
        if not base_policy.get("must_have"):
            base_policy["must_have"] = ["business_target"] if any(e.get("type") == "business_target" for e in base_elements) else []

        selector_groups = list(getattr(item, "selector_element_groups", []) or [])
        support_rows: list[dict[str, Any]] = []
        for atom in atoms:
            trunk_hit = False
            if selector_groups:
                trunk_hit = _selector_group_trunk_hit(selector_groups, atom.text)
                # Selected local scope may contain a compact continuation that
                # only states the value slot, e.g. "每单多3元" after an earlier
                # "连续7天有额外奖励" selector.  Allow comparison when the atom
                # has a comparable value and no competing subject trunk is present.
                if not trunk_hit and not _selected_scope_value_continuation(item, atom.text):
                    continue
            else:
                subject = match_side(
                    self.engine,
                    item.id + ".value_subject",
                    "knowledge_subject",
                    base_elements,
                    item.secondary_elements,
                    [],
                    base_policy,
                    [atom],
                    element_groups=[],
                )
                if subject.verdict == "miss":
                    continue
                trunk_hit = True
            for expected, unit, expected_profile, check in comparable_checks:
                if not _value_check_slot_applies(check, item, atom.text):
                    continue
                if _cross_slot_value_context_conflict_guard(item, atom.text):
                    continue
                actual_profile = _build_fact_profile(atom.text, unit or expected_profile.get("unit") or "")
                if not _actual_fact_relevant(expected, atom.text, expected_profile, actual_profile):
                    continue
                cmp_result = _compare_fact_profiles(expected_profile, actual_profile)
                if cmp_result == "conflict":
                    return KnowledgeCheck(
                        item.id, item.node_id, item.name, item.severity, "冲突", atom.text, atom.turn_index,
                        f"同一知识对象下出现不同可比事实值；expected={expected_profile.get('canonical')}, actual={actual_profile.get('canonical')}",
                        aliases=list(item.aliases), positive_verdict="miss", negative_verdict="hit",
                        element_audit={"value_check": {"expected": expected_profile, "actual": actual_profile, "result": "conflict", "trunk_hit": trunk_hit}},
                    )
                if cmp_result == "support":
                    support_rows.append({"atom": atom, "expected": expected, "expected_profile": expected_profile, "actual_profile": actual_profile, "trunk_hit": trunk_hit})
        if support_rows:
            row = support_rows[0]
            atom = row["atom"]
            return KnowledgeCheck(
                item.id, item.node_id, item.name, item.severity, "支持", atom.text, atom.turn_index,
                f"同一知识对象下的可比事实值与期望一致：expected={row['expected']}",
                aliases=list(item.aliases), positive_verdict="hit", negative_verdict="miss",
                element_audit={"value_check": {"expected": row["expected_profile"], "actual": row["actual_profile"], "result": "support", "trunk_hit": row["trunk_hit"]}},
            )
        return None


    def _judge_directional_fact_conflict(self, item: KnowledgeItem, atoms: list[DialogueAtom]) -> KnowledgeCheck | None:
        """Detect generic qualitative direction flips under a selected subject.

        Numeric/time mismatches are handled by value_check.  This covers stable
        non-numeric flips such as high/low, suitable/not suitable, helpful/not
        helpful, or smoother/no difference.  It remains graph-driven:
        a conflict requires the selector subject trunk and an expected fact-side
        direction from correct_groups.
        """
        correct_groups = list(getattr(item, "correct_element_groups", []) or getattr(item, "positive_element_groups", []) or getattr(item, "element_groups", []) or [])
        selector_groups = list(getattr(item, "selector_element_groups", []) or [])
        expected_terms: list[str] = []
        for group in correct_groups:
            if not isinstance(group, dict):
                continue
            for e in group.get("elements") or []:
                if not isinstance(e, dict) or not bool(e.get("fact")):
                    continue
                expected_terms.append(str(e.get("value") or ""))
                expected_terms.extend(str(x) for x in (e.get("pool") or []))
        pairs = _directional_opposite_pairs()

        def _selector_terms() -> list[str]:
            terms: list[str] = []
            for group in selector_groups:
                if not isinstance(group, dict):
                    continue
                for e in group.get("elements") or []:
                    if not isinstance(e, dict) or not bool(e.get("main")):
                        continue
                    terms.append(str(e.get("value") or ""))
                    terms.extend(str(x) for x in (e.get("pool") or []))
            # Avoid very short generic terms causing false anchoring.
            return [t for t in dict.fromkeys(terms) if len(t) >= 3]

        subject_terms = _selector_terms()

        def _opposite_bound_to_subject(text: str, opposite: str) -> bool:
            if not subject_terms:
                return True
            pos = text.find(opposite)
            if pos < 0:
                return False
            # Prefer same-clause binding. In contrastive sentences separated by
            # “；/，/。”, a direction word may belong to the other object.
            clause_seps = ["；", ";", "。", ".", "!", "！", "?", "？", "\n"]
            left = max(text.rfind(sep, 0, pos) for sep in clause_seps)
            # Treat comma as a weaker clause boundary for high/low comparisons;
            # it prevents a direction term attached to one contrasted object from
            # binding to another object merely because it appears nearby.
            left = max(left, text.rfind("，", 0, pos), text.rfind(",", 0, pos))
            right_seps = clause_seps + ["，", ","]
            rights = [text.find(sep, pos + len(opposite)) for sep in right_seps]
            rights = [r for r in rights if r >= 0]
            right = min(rights) if rights else len(text)
            clause = text[left + 1:right]
            if any(t and t in clause for t in subject_terms):
                return True
            return False

        for atom in atoms:
            if selector_groups and not _selector_group_trunk_hit(selector_groups, atom.text):
                continue
            text = str(atom.text or "")
            for expected_side, opposite_side, label in pairs:
                if not any(t and t in et for et in expected_terms for t in expected_side):
                    continue
                hit_opposites = [o for o in opposite_side if o and o in text]
                # In contrastive sentences, a high/low word may belong to a
                # neighbouring object.  Require the
                # opposite direction term to be locally bound to this knowledge
                # selector object, otherwise a correct contrast is misread as a
                # conflict.
                if label == "高低方向反转" and hit_opposites:
                    hit_opposites = [o for o in hit_opposites if _opposite_bound_to_subject(text, o)]
                # Avoid treating a bare negation marker as a status flip for an
                # unrelated predicate; a bare negation marker must bind to the same status slot.
                if label == "状态反转":
                    slot_terms = [t for t in expected_terms if any(x in t for x in ("生效", "显示", "开通", "设置", "添加", "签署"))]
                    if slot_terms and not any((o + st[-2:]) in text or (o + st) in text for o in hit_opposites for st in slot_terms):
                        continue
                if hit_opposites:
                    return KnowledgeCheck(
                        item.id, item.node_id, item.name, item.severity, "冲突", atom.text, atom.turn_index,
                        f"同一知识对象下出现方向性反转事实：{label}", aliases=list(item.aliases),
                        positive_verdict="miss", negative_verdict="hit",
                        element_audit={"directional_check": {"expected_terms": expected_terms[:12], "opposite_terms_hit": hit_opposites, "label": label}},
                    )
        return None

    def _negation_probe(self, item: KnowledgeItem, atoms: list[DialogueAtom], positive: ElementMatch) -> ElementMatch:
        # When the graph did not provide an explicit negative side, treat generic
        # negation as a strong review signal if it appears on the same assistant
        # atom as a partial or hit positive fact. This is intentionally generic.
        if positive.atom and positive.verdict in {"hit", "review"}:
            if any(e.type in {"polarity", "modality"} and e.value == "negation" for e in positive.atom.elements):
                raw = str(getattr(positive.atom, "text", "") or "")
                # Same-atom negation of a matched knowledge fact is a local
                # conflict, not merely a gray item.  This catches statements like
                # “对象A不适合场景B” where surface matching sees all positive
                # terms but the polarity is flipped.
                if any(x in raw for x in ("不适合", "不推荐", "不建议", "无关", "不影响", "没有帮助", "没帮助", "未生效", "未显示", "没有显示")):
                    return ElementMatch("hit", 0.82, positive.atom, reason="正向知识候选同句出现明确否定/方向翻转，按本地冲突处理")
                return ElementMatch("review", 0.6, positive.atom, reason="正向知识候选同句出现强否定元素")
        for atom in atoms:
            if any(e.type in {"polarity", "modality"} and e.value == "negation" for e in atom.elements):
                # Only mention by itself is not enough for conflict, but it is enough to mark review when some fact side is nearby.
                if positive.verdict != "miss":
                    return ElementMatch("review", 0.5, atom, reason="知识相关候选附近出现否定元素")
        return ElementMatch("miss", reason="没有显式负向元素或否定作用域")

    def _check(self, item: KnowledgeItem, verdict: KnowledgeVerdict, evidence: str, turn_index: int | None, positive: ElementMatch, negative: ElementMatch, reason: str) -> KnowledgeCheck:
        return KnowledgeCheck(
            item.id,
            item.node_id,
            item.name,
            item.severity,
            verdict,
            evidence,
            turn_index,
            reason,
            claim_id=None,
            aliases=list(item.aliases),
            positive_verdict=positive.verdict,
            negative_verdict=negative.verdict,
            requires_arbitration=verdict == "证据不足",
            element_audit={"correct_side": _match_audit(positive), "wrong_side": _match_audit(negative)},
        )


def _match_audit(match: ElementMatch) -> dict[str, Any]:
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



CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
}

COMPARABLE_UNITS = ("单", "天", "元", "秒", "分钟", "小时", "点")

TEMPORAL_CATEGORY_TERMS = {
    "today": ("今天", "当天", "当日", "今日"),
    "next_day": ("次日", "第二天", "隔天", "明天"),
    "previous_day": ("前一天", "前日", "上一天", "前一日"),
    "immediate": ("立即", "马上", "立刻", "当场"),
}


def _infer_unit(value: str) -> str:
    raw = str(value or "")
    # Deadline expressions like “前一天18点前” contain both “天” and “点”;
    # the comparable numeric unit is the hour, while “前一天/当天/次日” is
    # handled as a temporal category.
    if "点" in raw or ":" in raw or "18:00" in raw:
        return "点"
    # Prefer the unit immediately attached to a number.  This avoids inferring
    # “单” from words such as “单日” when the actual comparable fact is “8天”.
    m = re.search(r"\d+(?:\.\d+)?\s*([个单天元秒分钟小时]+)", raw)
    if m:
        u = m.group(1)
        for unit in COMPARABLE_UNITS:
            if unit in u:
                return unit
    cn_pat = "|".join(sorted(map(re.escape, CN_NUM), key=len, reverse=True))
    m = re.search(rf"({cn_pat})\s*([个单天元秒分钟小时]+)", raw)
    if m:
        u = m.group(2)
        for unit in COMPARABLE_UNITS:
            if unit in u:
                return unit
    for unit in COMPARABLE_UNITS:
        if unit in raw:
            return unit
    return ""


def _cn_number_to_float(token: str) -> float | None:
    token = str(token or "").strip()
    if not token:
        return None
    if token in CN_NUM:
        return float(CN_NUM[token])
    # Handle simple forms like 二十五 if they ever appear.
    if "十" in token:
        left, _, right = token.partition("十")
        tens = CN_NUM.get(left, 1 if left == "" else None)
        ones = CN_NUM.get(right, 0 if right == "" else None)
        if tens is not None and ones is not None:
            return float(tens * 10 + ones)
    return None


def _normalize_number_text(num: float) -> str:
    if abs(num - int(num)) < 1e-9:
        return str(int(num))
    return (f"{num:.6f}".rstrip("0").rstrip("."))


def _extract_number_unit_pairs(text: str, target_unit: str = "") -> list[tuple[str, str]]:
    t = str(text or "")
    pairs: list[tuple[str, str]] = []
    unit_class = "个单天点元秒分钟小时"
    # Ranges often attach the unit only to the second number: “5到10秒”.
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:到|-|~|—|至)\s*(\d+(?:\.\d+)?)\s*([个单天点元秒分钟小时]+)", t):
        u = m.group(3) or target_unit
        if target_unit and target_unit not in u:
            continue
        for raw_num in (m.group(1), m.group(2)):
            try:
                pairs.append((_normalize_number_text(float(raw_num)), target_unit or u[:1]))
            except Exception:
                pass
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([个单天点元秒分钟小时:]*)", t):
        raw_num, raw_unit = m.group(1), m.group(2) or ""
        unit = raw_unit
        if unit == ":" or ":" in raw_unit:
            unit = "点"
        if target_unit and target_unit != unit:
            # A graph may declare expected_value="10", unit="天". Treat a bare
            # expected number as carrying the declared unit, but do not do this
            # for free-form actual text where the unit is genuinely absent.
            if unit == "" and compact(t) == compact(raw_num):
                unit = target_unit
            elif not (target_unit == "点" and (raw_num in {"18", "6"} or ":" in t)):
                continue
        try:
            n = float(raw_num)
        except Exception:
            continue
        if target_unit == "点" and _normalize_number_text(n) == "6" and ("下午" in t or "晚上" in t):
            n = 18.0
        pairs.append((_normalize_number_text(n), unit or target_unit))
    cn_pat = "|".join(sorted(map(re.escape, CN_NUM), key=len, reverse=True))
    for m in re.finditer(rf"({cn_pat})\s*(?:到|-|~|—|至)\s*({cn_pat})\s*([{unit_class}]+)", t):
        unit = m.group(3)
        if target_unit and target_unit not in unit:
            continue
        for raw_cn in (m.group(1), m.group(2)):
            num = _cn_number_to_float(raw_cn)
            if num is not None:
                pairs.append((_normalize_number_text(num), target_unit or unit[:1]))
    for m in re.finditer(rf"({cn_pat})\s*([{unit_class}]+)", t):
        num = _cn_number_to_float(m.group(1))
        unit = m.group(2)
        if num is None:
            continue
        if target_unit and target_unit not in unit:
            continue
        if target_unit == "点" and _normalize_number_text(num) == "6" and ("下午" in t or "晚上" in t):
            num = 18.0
        pairs.append((_normalize_number_text(num), target_unit or unit[:1]))
    if target_unit == "点" and ("下午6" in t or "下午六" in t or "晚上6" in t or "晚上六" in t):
        pairs.append(("18", "点"))
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for pair in pairs:
        if pair not in seen:
            seen.add(pair); out.append(pair)
    return out


def _extract_temporal_categories(text: str) -> set[str]:
    t = str(text or "")
    out: set[str] = set()
    for label, terms in TEMPORAL_CATEGORY_TERMS.items():
        if any(term in t for term in terms):
            out.add(label)
    return out


def _build_fact_profile(text: str, unit: str = "") -> dict[str, Any]:
    raw = str(text or "")
    inferred_unit = unit or _infer_unit(raw)
    pairs = _extract_number_unit_pairs(raw, inferred_unit)
    nums = [n for n, _u in pairs]
    units = [u for _n, u in pairs if u]
    temporal = _extract_temporal_categories(raw)
    canonical_parts = []
    if nums:
        canonical_parts.append("/".join(nums) + (inferred_unit or (units[0] if units else "")))
    if temporal:
        canonical_parts.append("|".join(sorted(temporal)))
    return {
        "text": raw,
        "unit": inferred_unit,
        "numbers": nums,
        "number_units": pairs,
        "temporal_categories": sorted(temporal),
        "has_comparable": bool(nums or temporal),
        "canonical": "+".join(canonical_parts) if canonical_parts else raw,
    }




def _contextual_fact_side_match(engine: ElementEngine, rule_id: str, groups: list[Any], atoms: list[DialogueAtom]) -> ElementMatch:
    """Match fact-side errors when the subject is carried by local context.

    Customer-service explanations often answer an immediately previous topic,
    e.g. the user asks about an option and the agent replies only with
    "小班实操不太适合".  The graph still declares the subject main element,
    but within a selector-confirmed local scope a fact element may be evaluated
    against a nearby subject mention.  This is deliberately limited to
    knowledge wrong/correct fact sides and does not apply to node completion.
    """
    if not groups or not atoms:
        return ElementMatch("miss", reason="没有可上下文承接的事实侧元素组")
    context_text = " ".join(str(a.text or "") for a in atoms)
    best: ElementMatch | None = None
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        if not bool(group.get("allow_context_carry", False)):
            continue
        elems = [e for e in (group.get("elements") or []) if isinstance(e, dict)]
        if not elems:
            continue
        main_elems = [e for e in elems if bool(e.get("main")) and not bool(e.get("fact"))]
        fact_elems = [e for e in elems if bool(e.get("fact"))]
        if not fact_elems or not main_elems:
            continue
        # The declared subject/object trunk must be present somewhere in the
        # selector-confirmed local scope; otherwise a bare negative phrase is not
        # enough.
        trunk_ok = True
        for e in main_elems:
            terms = [str(e.get("value") or ""), *[str(x) for x in (e.get("pool") or [])]]
            if not any(t and text_hit(t, context_text) for t in terms):
                trunk_ok = False
                break
        if not trunk_ok:
            continue
        for atom in atoms:
            # If the apparent wrong fact is explicitly rejected (e.g.
            # "不能按明天补齐"), it is not a wrong-claim hit.
            if any(x in str(atom.text or "") for x in ["不能", "不可以", "不可", "不算", "不是", "无需", "不用"]):
                continue
            hit_facts=[]; missing_facts=[]
            for e in fact_elems:
                terms=[str(e.get("value") or ""), *[str(x) for x in (e.get("pool") or [])]]
                terms=[t for t in terms if t and len(compact(t)) >= 2 and compact(t) not in {"不", "没", "少", "会", "能"}]
                if any(text_hit(t, atom.text) for t in terms):
                    hit_facts.append(e)
                else:
                    missing_facts.append(e)
            if not hit_facts:
                continue
            # If there are multiple fact gates, require at least half or one strong
            # phrase; this avoids demanding every explanatory atom in a compact
            # natural reply while still being stricter than keyword spotting.
            coverage = len(hit_facts) / max(1, len(fact_elems))
            if coverage < 0.5 and len(hit_facts) < 1:
                continue
            sem_hits=[]
            for e in hit_facts:
                sem_hits.append(SemanticElement("surface", str(e.get("value") or ""), aliases=[str(x) for x in (e.get("pool") or [])], required=bool(e.get("main")), fact=bool(e.get("fact")), secondary_pool=[str(x) for x in (e.get("pool") or [])]))
            sem_missing=[]
            for e in missing_facts:
                sem_missing.append(SemanticElement("surface", str(e.get("value") or ""), aliases=[str(x) for x in (e.get("pool") or [])], required=bool(e.get("main")), fact=bool(e.get("fact")), secondary_pool=[str(x) for x in (e.get("pool") or [])]))
            score=max(0.72, min(1.0, 0.72 + 0.2*coverage))
            m=ElementMatch("hit", score, atom, primary_hits=sem_hits, missing=sem_missing, reason="事实元素在 selector 局部上下文中命中，主干由邻近候选承接")
            if best is None or (m.verdict, m.score) > (best.verdict, best.score):
                best=m
    return best or ElementMatch("miss", reason="未形成上下文承接的事实侧命中")

def _strict_surface_hit(term: str, text: str) -> bool:
    """Strict surface hit used only for comparable fact trunk binding.

    Candidate recall can be broad, but numeric/time comparison must not bind
    a target object to a sibling option sentence just because
    all characters appear somewhere.  Therefore trunk binding allows exact
    compact inclusion and the small modal bridge normalization, but not the
    character-overlap fallback used by general text_hit().
    """
    a = compact(term)
    b = compact(text)
    if not a or not b:
        return False
    if a in b:
        return True
    a2 = _drop_cn_modal_fillers(a)
    b2 = _drop_cn_modal_fillers(b)
    return bool(a2 and b2 and a2 in b2)



def _selector_group_trunk_hit(selector_groups: list[Any], text: str) -> bool:
    """Selector trunk binding for knowledge value/direction checks.

    Strict all-main matching is ideal, but negative cases often use shorthand:
    a shortened object phrase instead of the full schema object phrase.
    This function therefore has two layers:
    1) exact/all-main hit remains the strongest pass;
    2) for sentences containing comparable values or direction words, at least
       one specific object-like main element must hit.  Generic attribute mains
       such as bare attributes or slot labels are never enough alone.
    """
    txt = str(text or "")
    for group in selector_groups or []:
        if not isinstance(group, dict):
            continue
        mains = [e for e in group.get("elements") or [] if isinstance(e, dict) and e.get("main") and not e.get("fact")]
        if not mains:
            continue
        hit_flags = []
        specific_hit = False
        non_generic_hit = False
        for e in mains:
            terms = [str(e.get("value") or ""), *[str(x) for x in (e.get("pool") or [])]]
            is_generic = _is_generic_selector_main(e)
            hit = any(_strict_surface_hit(t, txt) for t in terms if t)
            hit_flags.append(hit)
            if hit and not is_generic:
                non_generic_hit = True
            if not is_generic and any(_object_like_term_hit(t, txt) for t in terms if t):
                specific_hit = True
        # A selector group made only of generic slot words such as “成本/成本/状态”
        # must not by itself select a fact for deterministic conflict.  Otherwise
        # a sibling correct sentence like “方案A成本较低” can be misread as a
        # contradiction of “方案B成本略高”.
        if all(hit_flags) and non_generic_hit:
            return True
        if specific_hit and _has_value_or_direction_surface(txt):
            return True
    return False


_GENERIC_SELECTOR_SUFFIXES = ("状态", "情况", "方式", "时间", "类型", "条件", "机制", "目的", "作用", "路径", "入口", "选项", "场景", "范围", "数量", "数值", "结果", "对象", "原因", "关系", "方向")



def _is_generic_selector_main(e: dict[str, Any]) -> bool:
    terms = [compact(str(e.get("value") or "")), *[compact(str(x)) for x in (e.get("pool") or [])]]
    terms = [t for t in terms if t]
    if not terms:
        return True
    value = terms[0]
    if len(value) <= 2:
        return True
    return any(value.endswith(suf) for suf in _GENERIC_SELECTOR_SUFFIXES)


def _object_like_term_hit(term: str, text: str) -> bool:
    if _strict_surface_hit(term, text):
        return True
    a = compact(term); b = compact(text)
    if len(a) < 4 or not b:
        return False
    # Generic shorthand: the first two characters often remain when a long
    # object phrase is shortened in natural dialogue.  This is only used after
    # generic-slot filtering and only when a comparable value or direction word
    # is present, so it cannot by itself create a fact verdict.
    head = a[:2]
    return bool(head and head in b)


def _has_value_or_direction_surface(text: str) -> bool:
    t = str(text or "")
    if _build_fact_profile(t).get("has_comparable"):
        return True
    terms = set()
    for left, right, _label in _directional_opposite_pairs():
        terms.update(left); terms.update(right)
    return any(x and x in t for x in terms)


def _directional_opposite_pairs() -> list[tuple[tuple[str, ...], tuple[str, ...], str]]:
    return [
        (("较高", "略高", "更高", "偏高", "更贵"), ("较低", "更低", "偏低", "更便宜", "便宜"), "高低方向反转"),
        (("较低", "更低", "偏低", "更便宜", "便宜"), ("较高", "略高", "更高", "偏高", "更贵"), "高低方向反转"),
        (("适合", "适用", "适用于", "推荐", "鼓励选择"), ("不适合", "不太适合", "不推荐", "不建议"), "适用性反转"),
        (("更流畅", "更顺", "更顺畅", "互动更好", "实时互动"), ("差不多", "没区别", "区别不大", "关系不大", "不明显"), "互动/效果优势反转"),
        (("有助于", "有帮助", "帮助", "保住", "保留"), ("没帮助", "没有帮助", "无关", "不影响", "没用"), "作用/影响方向反转"),
        (("已", "已经", "已开放", "已开启", "已显示"), ("未", "未开放", "未开启", "未显示", "还没", "没有"), "状态反转"),
    ]



def _cross_slot_value_context_conflict_guard(item: KnowledgeItem, text: str) -> bool:
    """Avoid comparing values from a different local fact slot.

    A transcript may mention a reward / compensation condition next to ordinary
    contract quota facts, e.g. "连续7天每天10单才有额外奖励".  Those numbers
    are not an assertion that the base contract itself is 7 days or 10 orders.
    The guard is generic: if the assistant atom is explicitly about rewards,
    bonuses, subsidies, or extra compensation, only knowledge atoms whose own
    name/text/aliases are in the same reward slot may compare the numbers.
    """
    txt = str(text or "")
    reward_terms = ("奖励", "额外", "奖金", "补贴", "补偿", "每单多", "多1", "多一")
    if not any(t in txt for t in reward_terms):
        return False
    item_text = " ".join(str(x or "") for x in [getattr(item, "id", ""), getattr(item, "name", ""), getattr(item, "text", ""), " ".join(getattr(item, "aliases", []) or [])])
    return not any(t in item_text for t in reward_terms)

def _selected_scope_value_continuation(item: KnowledgeItem, text: str) -> bool:
    """Allow value-only continuation inside selector-confirmed local scope.

    This remains graph-driven: the continuation must contain a comparable value
    declared by value_check and one of the non-numeric slot anchors supplied by
    the graph/prompt, such as reward/amount/delay/effective-time/action slots.
    """
    vc = dict(getattr(item, "value_check", {}) or {})
    raw_checks = vc.get("checks") or vc.get("value_checks") or []
    if isinstance(raw_checks, dict):
        raw_checks = [raw_checks]
    if not raw_checks:
        raw_checks = [vc]
    if not bool(vc.get("allow_continuation", False)):
        return False
    txt = str(text or "")
    blockers = [str(x) for x in (vc.get("continuation_blockers") or vc.get("blockers") or []) if str(x or "").strip()]
    if blockers and any(text_hit(b, txt) for b in blockers):
        return False
    has_value = False
    for check in raw_checks:
        if not isinstance(check, dict):
            continue
        expected = str(check.get("expected_value") or check.get("expected") or "")
        unit = str(check.get("unit") or "")
        prof = _build_fact_profile(expected, unit)
        if prof.get("has_comparable") and _build_fact_profile(txt, unit or prof.get("unit") or "").get("has_comparable"):
            has_value = True
            break
    if not has_value:
        return False
    anchors = set()
    for group in getattr(item, "selector_element_groups", []) or []:
        if not isinstance(group, dict):
            continue
        for e in group.get("elements") or []:
            if not isinstance(e, dict):
                continue
            for term in [e.get("value"), *(e.get("pool") or [])]:
                term = str(term or "").strip()
                if term and len(compact(term)) <= 8:
                    anchors.add(term)
    return any(a and a in txt for a in anchors)



def _value_check_slot_applies(check: dict[str, Any], item: KnowledgeItem, text: str) -> bool:
    """Check whether an actual comparable value belongs to this declared slot.

    Selector recall may select a local window that also contains another numeric
    fact, e.g. “条件X下满足数值Y可获得结果Z” near the separate rule
    “业务协议A连续10天履约”.  Comparable value_check is therefore allowed to
    compare only when the graph-declared slot anchors or condition anchors are
    present in the same assistant atom.  This is still schema-driven: anchors
    come from the knowledge table, not from dataset labels.
    """
    anchors = [str(x).strip() for x in (check.get("slot_anchors") or check.get("anchors") or []) if str(x).strip()]
    condition = str(check.get("condition") or "").strip()
    txt = str(text or "")
    if not anchors and not condition:
        return True
    hits = 0
    specific_hits = 0
    for anchor in anchors:
        if _anchor_term_hit(anchor, txt):
            hits += 1
            if not _generic_anchor_term(anchor):
                specific_hits += 1
    if anchors:
        specific_count = sum(1 for a in anchors if not _generic_anchor_term(a))
        if specific_count > 0:
            # Numeric/time facts must bind to the intended object + attribute
            # slot, not just a nearby generic attribute.  However LLM often
            # writes slot_anchors without the shorthand variants already present
            # in selector_groups, e.g. anchor=“对象A” while the user-facing
            # wrong sentence says “标准延迟1到2秒”.  If at least one concrete
            # slot anchor hits and the graph selector trunk also binds this
            # sentence, allow the value comparison; this keeps the check
            # schema-grounded while avoiding false semantic misses.
            if specific_count >= 2:
                if specific_hits >= 2:
                    return True
                if specific_hits >= 1 and _selector_group_trunk_hit(getattr(item, "selector_element_groups", []) or [], txt):
                    return True
                return False
            return specific_hits > 0 or _selector_group_trunk_hit(getattr(item, "selector_element_groups", []) or [], txt)
        return hits > 0
    # If there are no explicit slot anchors, the caller has already selected a
    # local knowledge scope with selector_groups.  Do not require a composite
    # natural-language condition string to appear verbatim
    # in the wrong utterance “取消后当天就会生效”.
    return True


def _generic_anchor_term(term: str) -> bool:
    c = compact(term)
    if len(c) <= 2:
        return True
    return c in {"成本", "成本", "状态", "情况", "时间", "方式", "条件", "要求", "标准", "结果", "影响", "生效"} or any(c.endswith(x) for x in _GENERIC_SELECTOR_SUFFIXES)


def _anchor_term_hit(anchor: str, text: str) -> bool:
    a = compact(anchor)
    b = compact(text)
    if not a or not b:
        return False
    if _strict_surface_hit(a, b):
        return True
    # Allow a small gap in phrases such as “连续履约” -> “连续10天履约”.
    if len(a) >= 4:
        parts = [x for x in re.split(r"[，,。；;：:/、()（）\[\]{}]|和|与|及|或", a) if x]
        if len(parts) >= 2 and all(p in b for p in parts):
            return True
        if a[:2] in b and a[-2:] in b:
            return True
    return False

def _actual_fact_relevant(expected_text: str, actual_text: str, expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Avoid turning a topic/time mention into a fact conflict.

    Value_check compares facts after selector recall.  Selector recall may find a
    broad topic utterance that mentions a date or time without asserting the
    target comparable fact.  Numeric/time conflicts therefore need the comparable
    value to be attached to the same expected fact slot, not merely present in
    the selector window.
    """
    if not actual.get("has_comparable"):
        return False
    exp_nums = set(expected.get("numbers") or [])
    act_nums = set(actual.get("numbers") or [])
    # If the expected fact has a numeric component (18点、12单、5到10秒等),
    # an actual sentence without any comparable number is only a topic mention.
    if exp_nums and not act_nums:
        return False
    exp_text = str(expected_text or "")
    act_text = str(actual_text or "")
    # A comparable value mentioned under an explicit negation is usually a
    # rejected value rather than an asserted value, e.g. "不能按明天补齐".
    # Value_check only compares asserted comparable facts; explicit negative
    # directions belong to wrong_groups / safety groups.
    if any(x in act_text for x in ["不能", "不可以", "不可", "不算", "不是", "无需", "不用"]):
        return False
    # For temporal-only facts, require a slot anchor derived from the expected
    # fact text itself instead of a built-in business vocabulary. This keeps
    # local value_check generic: if the expected value is “next-cycle starts”,
    # the anchor is the non-temporal action/object residue such as “starts”; if
    # the expected value is only a bare time, no extra anchor is imposed.
    if not exp_nums and expected.get("temporal_categories") and actual.get("temporal_categories"):
        anchors = _semantic_slot_anchors(exp_text)
        if anchors and not any(a in act_text for a in anchors):
            return False
    return True



def _semantic_slot_anchors(text: str) -> list[str]:
    """Extract non-comparable slot anchors from an expected fact value.

    The function deliberately avoids a task-specific action list. It removes
    dates, numeric values and common temporal/filler particles, then uses the
    remaining compact lexical residue as the local slot anchor for value_check.
    """
    raw = compact(text)
    if not raw:
        return []
    cleaned = re.sub(r"\d+(?:\.\d+)?", " ", raw)
    cleaned = re.sub(r"[一二三四五六七八九十百千万两]+", " ", cleaned)
    temporal_terms = (
        "今天", "当天", "当日", "明天", "次日", "翌日", "第二天", "下一天",
        "昨日", "昨天", "前天", "后天", "上周", "下周", "本周", "本月",
        "次月", "下月", "前一天", "后一天", "下一工作日", "工作日",
        "上午", "下午", "晚上", "中午", "凌晨", "早上", "点前", "点后",
        "之前", "以后", "以内", "之内", "之后", "前", "后", "点", "时",
        "分", "分钟", "小时", "天", "日", "周", "月", "年", "周期",
    )
    for term in sorted(temporal_terms, key=len, reverse=True):
        cleaned = cleaned.replace(term, " ")
    filler_terms = ("为", "是", "在", "从", "到", "至", "于", "的", "了", "会", "可", "可以", "必须", "需要", "进行")
    for term in sorted(filler_terms, key=len, reverse=True):
        cleaned = cleaned.replace(term, " ")
    parts = [x for x in re.split(r"\s+|[，,。；;：:/、()（）\[\]{}]+", cleaned) if len(x) >= 2]
    # Keep compact sub-spans only; very long strings are usually composite and
    # should not become a hidden sentence-level anchor.
    anchors = []
    for part in parts:
        if 2 <= len(part) <= 8 and part not in anchors:
            anchors.append(part)
    return anchors[:4]

def _compare_fact_profiles(expected: dict[str, Any], actual: dict[str, Any]) -> str | None:
    if not actual.get("has_comparable"):
        return None
    exp_temp = set(expected.get("temporal_categories") or [])
    act_temp = set(actual.get("temporal_categories") or [])
    if exp_temp and act_temp:
        if exp_temp & act_temp:
            # Continue to numeric check if both also have numbers.
            pass
        else:
            return "conflict"
    exp_nums = set(expected.get("numbers") or [])
    act_nums = set(actual.get("numbers") or [])
    if exp_nums and act_nums:
        if act_nums <= exp_nums or exp_nums <= act_nums or (exp_nums & act_nums and not (act_nums - exp_nums)):
            return "support"
        return "conflict"
    if exp_temp and act_temp and (exp_temp & act_temp):
        return "support"
    return None

# Legacy helper names kept for compatibility with older diagnostics/tests.
def _normalize_expected_values(value: str) -> set[str]:
    return set(_build_fact_profile(value).get("numbers") or []) or {str(value or "").strip()}


def _extract_numeric_values(text: str, unit: str = "") -> list[str]:
    return [n for n, _u in _extract_number_unit_pairs(text, unit)]


def _side_policy(base: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Tighten knowledge-side policies for strong polarity elements.

    Negation is not a negative-object layer in knowledge.  It is an ordinary
    but strong semantic element.  If a negative fact side declares a negation or
    other polarity/modality flip, that flip must be present before the negative
    side can be a deterministic hit; otherwise a shared business target +
    attribute would falsely trigger the negative side.
    """
    policy = dict(base or {})
    must = list(policy.get("must_have") or [])
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        typ = str(element.get("type") or "")
        val = str(element.get("value") or "")
        if typ in {"polarity", "modality"} and val in {"negation", "negative", "deny"}:
            if typ not in must:
                must.append(typ)
    policy["must_have"] = must
    return policy


def _downgrade_missing_strong_flip(match: ElementMatch, elements: list[dict[str, Any]]) -> ElementMatch:
    """For knowledge negative sides, missing the declared polarity flip is a strict miss.

    A negative side often shares the same business_target/attribute with the
    positive side and differs only by polarity/value.  If the polarity flip is
    absent, partial object overlap should not force arbitration.
    """
    if match.verdict != "review":
        return match
    strong = {
        (str(e.get("type") or ""), str(e.get("value") or ""))
        for e in elements or [] if isinstance(e, dict)
        and str(e.get("type") or "") in {"polarity", "modality"}
        and str(e.get("value") or "") in {"negation", "negative", "deny"}
    }
    if not strong:
        return match
    hit_keys = {(e.type, e.value) for e in match.primary_hits}
    if not (strong & hit_keys):
        return ElementMatch("miss", 0.0, reason="知识负向侧缺少声明的否定/反转元素，按严格未命中处理")
    return match
