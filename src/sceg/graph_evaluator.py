from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import re

from .constraint_judge import ConstraintCheck, ConstraintEvent, ConstraintJudge
from .evidence_extractor import EvidenceExtractor
from .evidence_matcher import EvidenceMatcher, GroupMatch, PatternHit
from .evidence_units import DialogueTurn, EvidenceUnit
from .knowledge_judge import KnowledgeCheck, KnowledgeEvent, KnowledgeJudge
from .generic_customer_service_expressions import USER_CONTINUE_CUES, USER_STOP_CUES
from .schema import GraphNode, RelationGroup, Requirement, StateGraph
from .target_scanner import PositiveObjectScanner
from .element_engine import ElementEngine, ElementMatch, match_side




@dataclass(slots=True)
class RequirementResult:
    requirement_id: str
    text: str
    required: bool
    score: float
    matched: bool
    group_matches: list[GroupMatch] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    element_audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "text": self.text,
            "required": self.required,
            "score": round(self.score, 4),
            "matched": self.matched,
            "aliases": self.aliases,
            "element_audit": self.element_audit,
            "evidence_flow": "positive_object_to_dialogue_scan",
            "groups": [
                {
                    "group_id": g.group_id,
                    "description": g.description,
                    "required": g.required,
                    "matched": g.matched,
                    "score": round(g.score, 4),
                    "hits": [{"turn_index": h.turn_index, "text": h.text} for h in g.hits],
                    "expected_patterns": list(getattr(g, "expected_patterns", []) or []),
                    "element_audit": self.element_audit,
                }
                for g in self.group_matches
            ],
        }


@dataclass(slots=True)
class NodeResult:
    node_id: str
    name: str
    status: str
    score: float
    active: bool
    group_matches: list[GroupMatch] = field(default_factory=list)
    requirement_results: list[RequirementResult] = field(default_factory=list)
    first_hit_turn: int | None = None
    inactive_reason: str = ""
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "status": self.status,
            "score": round(self.score, 4),
            "active": self.active,
            "first_hit_turn": self.first_hit_turn,
            "inactive_reason": self.inactive_reason,
            "aliases": self.aliases,
            "requirements": [r.to_dict() for r in self.requirement_results],
            "groups": [
                {
                    "group_id": g.group_id,
                    "description": g.description,
                    "required": g.required,
                    "matched": g.matched,
                    "score": round(g.score, 4),
                    "hits": [{"turn_index": h.turn_index, "text": h.text} for h in g.hits],
                    "expected_patterns": list(getattr(g, "expected_patterns", []) or []),
                }
                for g in self.group_matches
            ],
        }


@dataclass(slots=True)
class RelationEvent:
    relation: str
    source: str
    target: str
    status: str
    penalty: float
    reason: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "source": self.source,
            "target": self.target,
            "status": self.status,
            "penalty": round(self.penalty, 4),
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class ContextEvent:
    policy_id: str
    status: str
    trigger_turn: int | None
    handling_turn: int | None
    suppressed_nodes: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "status": self.status,
            "trigger_turn": self.trigger_turn,
            "handling_turn": self.handling_turn,
            "suppressed_nodes": self.suppressed_nodes,
            "reason": self.reason,
        }


@dataclass(slots=True)
class EvaluationResult:
    graph_id: str
    dialogue_id: str
    node_results: list[NodeResult]
    knowledge_events: list[KnowledgeEvent]
    constraint_events: list[ConstraintEvent]
    evidence_units: list[EvidenceUnit]
    scores: dict[str, float]
    caps: list[dict[str, Any]]
    relation_events: list[RelationEvent] = field(default_factory=list)
    context_events: list[ContextEvent] = field(default_factory=list)
    knowledge_checks: list[KnowledgeCheck] = field(default_factory=list)
    constraint_checks: list[ConstraintCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "dialogue_id": self.dialogue_id,
            "scores": self.scores,
            "caps": self.caps,
            "node_results": [n.to_dict() for n in self.node_results],
            "knowledge_events": [e.to_dict() for e in self.knowledge_events],
            "constraint_events": [e.to_dict() for e in self.constraint_events],
            "knowledge_checks": [e.to_dict() for e in self.knowledge_checks],
            "constraint_checks": [e.to_dict() for e in self.constraint_checks],
            "relation_events": [e.to_dict() for e in self.relation_events],
            "context_events": [e.to_dict() for e in self.context_events],
            "evidence_units": [e.to_dict() for e in self.evidence_units],
        }


def _element_match_audit(match: ElementMatch) -> dict[str, Any]:
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
        "candidate_atoms": [
            {
                "atom_id": getattr(a, "atom_id", ""),
                "turn_index": getattr(a, "turn_index", None),
                "speaker": getattr(a, "speaker", ""),
                "text": getattr(a, "text", ""),
            }
            for a in (match.candidate_atoms or [])
        ],
        "candidate_results": list(getattr(match, "candidate_results", []) or []),
    }


class GraphEvaluator:
    def __init__(self, graph: StateGraph, runtime: dict[str, Any], extractor: EvidenceExtractor | None = None) -> None:
        self.graph = graph
        self.runtime = runtime
        self.extractor = extractor or EvidenceExtractor()
        broad_terms = self._broad_evidence_terms(graph)
        self.matcher = EvidenceMatcher(enable_fuzzy=True, broad_terms=broad_terms)
        self.strict_matcher = EvidenceMatcher(enable_fuzzy=False)
        self.element_engine = ElementEngine()
        self.element_engine.configure_schema(graph)
        self.knowledge_judge = KnowledgeJudge(self.element_engine)
        self.constraint_judge = ConstraintJudge(runtime, self.element_engine)
        self.positive_object_scanner = PositiveObjectScanner()

    def _broad_evidence_terms(self, graph: StateGraph) -> set[str]:
        """Terms reused across many node groups are topic anchors.

        A broad topic word can help locate a requirement, but by itself it should
        not complete an assistant-side node.  This is computed from the current
        graph only; no task vocabulary is stored in code.
        """
        group_sets: list[set[str]] = []
        for node in graph.nodes:
            for req in node.requirements:
                for group in req.evidence_groups:
                    vals: set[str] = set()
                    for pat in group.patterns:
                        if pat.get("speaker") not in {None, "", "assistant"}:
                            continue
                        for value in pat.get("any") or []:
                            text = str(value or "").strip()
                            if text:
                                vals.add(text)
                    if vals:
                        group_sets.append(vals)
        counts: dict[str, int] = {}
        for vals in group_sets:
            for value in vals:
                counts[value] = counts.get(value, 0) + 1
        return {value for value, count in counts.items() if count >= 2}

    def evaluate(self, dialogue: dict[str, Any]) -> EvaluationResult:
        dialogue_id = str(dialogue.get("id") or dialogue.get("dialogue_id") or "dialogue")
        turns = [DialogueTurn.from_dict(t, i) for i, t in enumerate(dialogue.get("turns", []))]
        units = self.extractor.extract(turns)
        dialogue_atoms = self.element_engine.build_atoms(units)
        context_events, suppressed_nodes = self._resolve_context(units)
        node_results = [self._evaluate_node(node, units, suppressed_nodes, dialogue_atoms) for node in self.graph.nodes]
        structural_events, structural_suppressed = self._resolve_structural_transitions(node_results, units)
        if structural_suppressed:
            suppressed_nodes = set(suppressed_nodes) | structural_suppressed
            node_results = [self._evaluate_node(node, units, suppressed_nodes, dialogue_atoms) for node in self.graph.nodes]
        if structural_events:
            context_events = [*context_events, *structural_events]
        knowledge_checks = self.knowledge_judge.judge(self.graph.knowledge, units, dialogue_atoms)
        constraint_checks = self.constraint_judge.judge(self.graph.constraints, units, dialogue_atoms)
        knowledge_events = [x for x in knowledge_checks if x.verdict == "冲突"]
        constraint_events = [x for x in constraint_checks if x.verdict == "违规" and getattr(x, "enforcement", "hard") == "hard"]
        scores, caps, relation_events = self._score(node_results, knowledge_events, constraint_events, context_events, constraint_checks)
        return EvaluationResult(
            self.graph.graph_id,
            dialogue_id,
            node_results,
            knowledge_events,
            constraint_events,
            units,
            scores,
            caps,
            relation_events=relation_events,
            context_events=context_events,
            knowledge_checks=knowledge_checks,
            constraint_checks=constraint_checks,
        )

    def _evaluate_node(self, node: GraphNode, units: list[EvidenceUnit], suppressed_nodes: set[str], dialogue_atoms=None) -> NodeResult:
        if node.id in suppressed_nodes:
            return NodeResult(node.id, node.name, "不适用", 1.0, False, [], [], inactive_reason="条件转场后不再强制要求", aliases=list(node.aliases))
        active = self._node_active(node, units, dialogue_atoms or [])
        if not active:
            return NodeResult(node.id, node.name, "不适用", 1.0, False, [], [], inactive_reason="未触发条件节点", aliases=list(node.aliases))

        # Active-path scoring must be scoped to the user-triggered subtask.
        # A LLM graph may put several independent FAQ answers or several
        # information-acquisition atoms in one node.  When one user question
        # triggers that node, unrelated sibling atoms must not be scored as
        # missing.  The executor therefore routes active FAQ/branch nodes to
        # the relevant atom subset before scoring.
        scoped_reqs, skipped_reqs = self._scoped_requirements_for_node(node, units, dialogue_atoms or [])
        req_results = [self._evaluate_requirement(req, units, dialogue_atoms or []) for req in scoped_reqs]
        req_results.extend(skipped_reqs)
        all_groups = [g for r in req_results for g in r.group_matches]
        required_reqs = [r for r in req_results if r.required]
        if not required_reqs:
            score = 1.0 if req_results else 0.0
        else:
            weight_sum = sum(self._requirement_weight(node, r.requirement_id) for r in required_reqs) or float(len(required_reqs))
            score = sum(r.score * self._requirement_weight(node, r.requirement_id) for r in required_reqs) / weight_sum
        if score == 0.0 and self._generic_condition_branch_handled(node, units):
            score = float(self.runtime.get("thresholds", {}).get("node_satisfied", 0.75))
        # Atom/element method: a required atom with zero evidence is not a tiny
        # style loss.  Keep node scoring continuous, but cap the node so flow
        # omissions are visible locally instead of being hidden by sibling atoms.
        zero_required = [r for r in required_reqs if float(r.score or 0.0) <= 0.0001]
        if zero_required and len(required_reqs) > 1:
            cap = float(self.runtime.get("thresholds", {}).get("node_required_atom_missing_cap", 0.58))
            score = min(score, cap)
        satisfied = float(self.runtime.get("thresholds", {}).get("node_satisfied", 0.75))
        partial = float(self.runtime.get("thresholds", {}).get("node_partial", 0.35))
        if score >= satisfied:
            status = "已完成"
        elif score >= partial:
            status = "部分完成"
        else:
            status = "缺失"
        hit_turns = [h.turn_index for g in all_groups for h in g.hits]
        if not hit_turns and score >= satisfied:
            trigger_turn = self._first_condition_trigger_turn(node, units)
            hit_turns = [trigger_turn] if trigger_turn is not None else []
        return NodeResult(node.id, node.name, status, score, True, all_groups, req_results, min(hit_turns) if hit_turns else None, aliases=list(node.aliases))

    def _evaluate_requirement(self, req: Requirement, units: list[EvidenceUnit], dialogue_atoms=None) -> RequirementResult:
        match = self._match_requirement_elements(req, dialogue_atoms or [])
        score = match.score if match.verdict != "miss" else 0.0
        threshold = float(self.runtime.get("thresholds", {}).get("requirement_satisfied", self.runtime.get("thresholds", {}).get("node_satisfied", 0.75)))
        hit_atoms = list(getattr(match, "candidate_atoms", []) or [])
        if not hit_atoms and match.atom is not None:
            hit_atoms = [match.atom]
        hits = [
            PatternHit(
                pattern={"source": "element_rule", "verdict": match.verdict, "reason": match.reason},
                turn_index=getattr(a, "turn_index", 0),
                text=getattr(a, "text", ""),
                score=score,
            )
            for a in hit_atoms
            if match.verdict != "miss"
        ]
        group = GroupMatch(
            group_id=f"atom::{req.id}",
            description=req.text or req.id,
            required=req.required,
            matched=match.verdict == "hit",
            hits=hits,
            score=score,
            aliases=list(req.aliases),
            expected_patterns=[f"element_rule: {match.verdict}; {match.reason}"],
        )

        # Atom/element-only executor: legacy evidence_groups are not scored.
        # They may remain in old graph files as audit context, but the runtime
        # must not fall back to pattern matching or answer-key-like groups.

        # ElementEngine already applies the side-specific local thresholds and
        # returns hit/review/miss.  Do not reclassify a local hit as a miss only
        # because its continuous score is below the global node threshold; the
        # acceptance layer separately records low-confidence required atoms.
        return RequirementResult(req.id, req.text, req.required, score, match.verdict == "hit", [group], list(req.aliases), _element_match_audit(match))

    def _match_requirement_elements(self, req: Requirement, dialogue_atoms) -> ElementMatch:
        primary = list(getattr(req, "primary_elements", []) or [])
        if not primary and getattr(req, "positive_object", None):
            primary = list(req.positive_object.get("primary_elements") or req.positive_object.get("required_elements") or []) if isinstance(req.positive_object, dict) else []
        if not primary and getattr(req, "positive_object", None):
            # Last-resort structural target: schema-provided surface values become elements.
            obj = req.positive_object if isinstance(req.positive_object, dict) else {}
            vals = []
            for key in ("surface_forms", "semantic_equivalents", "aliases", "evidence_phrases"):
                val = obj.get(key)
                if isinstance(val, str):
                    vals.append(val)
                elif isinstance(val, list):
                    vals.extend(str(x) for x in val if str(x or "").strip())
            primary = [{"type": "surface", "value": v} for v in vals]
        rule = self.element_engine.make_rule(
            req.id,
            "node_positive",
            primary=primary,
            secondary=getattr(req, "secondary_elements", {}) or {},
            zero=[],
            policy=getattr(req, "match_policy", {}) or {},
            element_groups=getattr(req, "element_groups", []) or [],
        )
        scoped_atoms = self._scope_atoms_for_role(dialogue_atoms, "assistant", fallback_to_all=False)
        return self.element_engine.match_rule(rule, scoped_atoms)

    def _match_requirement_positive_object(self, req: Requirement, units: list[EvidenceUnit]) -> GroupMatch | None:
        positive_object = getattr(req, "positive_object", {}) or {}
        if not isinstance(positive_object, dict) or not positive_object:
            return None
        hit = self.positive_object_scanner.scan(positive_object, units, speaker="assistant")
        pattern = {
            "source": "requirement.positive_object",
            "object_type": positive_object.get("object_type") or positive_object.get("type") or "positive_object",
            "matched_terms": hit.matched_terms,
            "reason": hit.reason,
        }
        hits = [PatternHit(pattern=pattern, turn_index=hit.turn_index or 0, text=hit.text, score=hit.score)] if hit.matched and hit.turn_index is not None else []
        return GroupMatch(
            group_id=f"target::{req.id}",
            description=positive_object.get("description") or req.text or "positive_object",
            required=req.required,
            matched=hit.matched,
            hits=hits,
            score=hit.score if hit.matched else 0.0,
            aliases=[str(x) for x in positive_object.get("aliases", [])] if isinstance(positive_object.get("aliases"), list) else [],
            expected_patterns=[f"positive_object_scan: {hit.reason}"],
        )

    def _requirement_weight(self, node: GraphNode, requirement_id: str) -> float:
        for req in node.requirements:
            if req.id == requirement_id:
                return max(0.0, req.weight)
        return 1.0


    def _scoped_requirements_for_node(self, node: GraphNode, units: list[EvidenceUnit], dialogue_atoms=None) -> tuple[list[Requirement], list[RequirementResult]]:
        """Return the executable atom subset for the currently activated path.

        This is the key negative-pack purity guard.  User-triggered FAQ nodes
        and branch nodes often contain several independent atoms because a
        no-memory graph builder compresses many sibling questions into one node.
        Only atoms aligned to the user trigger / user-provided state should be
        required in the current sample; unrelated siblings are kept in the report
        as skipped, not counted as misses.
        """
        reqs = list(getattr(node, "requirements", []) or [])
        if not reqs:
            return [], []
        # Some schema atoms are core only when the call can continue normally.
        # Example shape: an early user safety/stop state should suppress later
        # business-detail obligations, but the same atom must be scored when no
        # such terminal context appears.  This is graph-declared and label-free.
        if any(isinstance(getattr(r, "match_policy", None), dict) and r.match_policy.get("conditional_required_when_no_early_user_stop") for r in reqs):
            early_stop = self._has_early_user_stop_context(units)
            if not early_stop:
                reqs = [replace(r, required=True) if isinstance(getattr(r, "match_policy", None), dict) and r.match_policy.get("conditional_required_when_no_early_user_stop") else r for r in reqs]
        dialogue_atoms = dialogue_atoms or []
        kind = str(getattr(node, "node_type", "") or "").lower()
        mode = str(getattr(node.activation, "mode", "") or "").lower()
        name = str(getattr(node, "name", "") or "")
        is_question_node = kind in {"faq", "out_of_scope"} or mode in {"user_triggered", "condition"} or any(x in name for x in ("FAQ", "追问", "问题", "分支", "路径", "表示"))
        is_faq_like = kind in {"faq", "out_of_scope"} or any(x in name for x in ("FAQ", "追问", "其他问题"))

        # Information request atoms are satisfied by the user's prior state if
        # the user has already provided the relevant object/value.  They should
        # not force the assistant to ask the same question again.
        skipped: list[RequirementResult] = []
        remaining: list[Requirement] = []
        for req in reqs:
            if (not is_faq_like) and self._user_already_provided_info(req, units, dialogue_atoms):
                skipped.append(self._skipped_requirement(req, "用户已在对话中提供该信息，信息获取类 atom 不再要求客服重复询问"))
            else:
                remaining.append(req)
        reqs = remaining
        if len(reqs) <= 1 or not is_question_node:
            return reqs, skipped

        user_text = self._activation_user_context_text(node, units)
        if not user_text:
            return reqs, skipped
        scored: list[tuple[float, Requirement]] = []
        for req in reqs:
            score = self._requirement_user_relevance(req, user_text)
            scored.append((score, req))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][0] if scored else 0.0
        # Multiple FAQ atoms may be relevant if the user asks a compound
        # question, but a low-similarity sibling must not become mandatory.
        threshold = float(self.runtime.get("thresholds", {}).get("faq_atom_route_similarity", 0.10))
        keep = [req for score, req in scored if score >= threshold and score >= max(0.06, best * 0.55)]
        if not keep and scored and best > 0:
            keep = [scored[0][1]]
        if not keep:
            return reqs, skipped
        keep_ids = {r.id for r in keep}
        for score, req in scored:
            if req.id not in keep_ids:
                skipped.append(self._skipped_requirement(req, f"未被本次用户触发内容选中；FAQ/分支 atom 路由相似度={score:.3f}"))
        return keep, skipped

    def _skipped_requirement(self, req: Requirement, reason: str) -> RequirementResult:
        hit = PatternHit(pattern={"source": "activation_subgraph_scope", "verdict": "skipped", "reason": reason}, turn_index=0, text="", score=1.0)
        group = GroupMatch(group_id=f"atom::{req.id}", description=req.text or req.id, required=False, matched=True, hits=[hit], score=1.0, aliases=list(req.aliases), expected_patterns=[reason])
        return RequirementResult(req.id, req.text, False, 1.0, True, [group], list(req.aliases), {"verdict": "skipped", "score": 1.0, "reason": reason, "hit_elements": [], "missing_elements": [], "candidate_atoms": []})

    def _has_early_user_stop_context(self, units: list[EvidenceUnit]) -> bool:
        """Return True when the user stops/unsafe-states the call at the start.

        This protects active-subgraph scoring: business detail atoms that are
        explicitly conditional on normal continuation should not be forced after
        the user opens with refusal, busy/unavailable, or safety risk.
        """
        early_user = [u for u in units if u.speaker == "user" and int(getattr(u, "turn_index", 999) or 999) <= 2]
        text = " ".join(str(u.text or "") for u in early_user)
        if not text:
            return False
        stop_terms = list(USER_STOP_CUES) + ["不方便", "没空", "忙", "开车", "骑车", "路上", "真跑不了", "跑不了", "先不跑", "晚点再说"]
        return any(t and t in text for t in stop_terms)

    def _activation_user_context_text(self, node: GraphNode, units: list[EvidenceUnit]) -> str:
        turn = self._first_condition_trigger_turn(node, units)
        user_units = [u for u in units if u.speaker == "user"]
        if not user_units:
            return ""
        if turn is None:
            # For optional FAQ / branch nodes, never route by concatenating all
            # user turns.  That makes one late question activate unrelated
            # sibling atoms.  Use only the most relevant question-like user
            # utterances as the routing context.
            selected = self._top_user_context_units_for_node(node, user_units)
        else:
            window = [u for u in user_units if max(0, turn - 2) <= u.turn_index <= turn + 4]
            selected = [u for u in window if self._is_questionish_user_text(u.text)] or window
        return "\n".join(str(u.text or "") for u in selected if str(u.text or "").strip())

    def _top_user_context_units_for_node(self, node: GraphNode, user_units: list[EvidenceUnit]) -> list[EvidenceUnit]:
        reqs = list(getattr(node, "requirements", []) or [])
        hint = " ".join([str(getattr(node.activation, "trigger_hint", "") or ""), str(getattr(node, "name", "") or "")])
        scored: list[tuple[float, EvidenceUnit]] = []
        for u in user_units:
            text = str(getattr(u, "text", "") or "")
            if not text.strip():
                continue
            req_score = max((self._requirement_user_relevance(req, text) for req in reqs), default=0.0)
            hint_score = self._token_similarity(hint, text)
            q_bonus = 0.04 if self._is_questionish_user_text(text) else 0.0
            scored.append((max(req_score, hint_score) + q_bonus, u))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.08:
            return [u for _, u in scored[:2]]
        question_units = [u for u in user_units if self._is_questionish_user_text(u.text)]
        if question_units:
            return question_units[-2:]
        return user_units[-2:]

    def _is_questionish_user_text(self, text: Any) -> bool:
        raw = str(text or "")
        if not raw.strip():
            return False
        return any(x in raw for x in ("?", "？", "吗", "什么", "怎么", "多少", "哪里", "哪", "是否", "能不能", "可不可以", "为什么", "啥"))

    def _requirement_user_relevance(self, req: Requirement, user_text: str) -> float:
        target_text = " ".join([req.id, req.text, " ".join(req.aliases), self._element_value_text(getattr(req, "element_groups", []) or [])])
        return self._token_similarity(target_text, user_text)

    def _element_value_text(self, groups: Any) -> str:
        vals: list[str] = []
        if isinstance(groups, dict):
            groups = [groups]
        for g in groups or []:
            if not isinstance(g, dict):
                continue
            for e in g.get("elements") or []:
                if isinstance(e, dict):
                    vals.append(str(e.get("value") or ""))
                    vals.extend(str(x) for x in (e.get("pool") or []) if str(x or "").strip())
        return " ".join(vals)

    def _token_similarity(self, a: Any, b: Any) -> float:
        def toks(x: Any) -> set[str]:
            raw = str(x or "").lower()
            out: set[str] = set()
            for part in re.split(r"[^a-z0-9\u4e00-\u9fff]+", raw):
                if not part:
                    continue
                out.add(part)
                for chunk in re.findall(r"[\u4e00-\u9fff]+", part):
                    if len(chunk) <= 6:
                        out.add(chunk)
                    out.update(chunk[i:i+2] for i in range(max(0, len(chunk)-1)))
                    out.update(chunk[i:i+3] for i in range(max(0, len(chunk)-2)))
                for chunk in re.findall(r"[a-z0-9]+", part):
                    out.add(chunk)
            return {x for x in out if x}
        ta, tb = toks(a), toks(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / max(1, min(len(ta), len(tb)))

    def _user_already_provided_info(self, req: Requirement, units: list[EvidenceUnit], dialogue_atoms=None) -> bool:
        # Final strict graph/data mode: information-acquisition atoms are real
        # assistant obligations unless runtime explicitly opts into the looser
        # "user already provided" shortcut.  Earlier versions used this shortcut
        # by default and produced surface negative passes: the expected missing
        # ask was accepted by audit metadata while node score stayed near 100.
        if not bool(self.runtime.get("thresholds", {}).get("info_request_user_provided_satisfies", False)):
            return False
        text = str(getattr(req, "text", "") or "")
        ask_like = any(x in text for x in ("询问", "确认", "检查", "是否", "哪", "还是", "提供", "手机号", "号码", "方式", "用什么", "发课"))
        if not ask_like:
            return False
        user_units = [u for u in units if u.speaker == "user"]
        if not user_units:
            return False
        user_text = "\n".join(u.text for u in user_units)
        # Require at least one discriminative main element/value from the atom to
        # appear in user text.  Generic ask verbs do not count.
        values: list[str] = []
        for g in getattr(req, "element_groups", []) or []:
            if not isinstance(g, dict):
                continue
            for e in g.get("elements") or []:
                if not isinstance(e, dict) or not bool(e.get("main")):
                    continue
                terms = [str(e.get("value") or ""), *[str(x) for x in (e.get("pool") or [])]]
                values.extend(t for t in terms if len("".join(str(t).split())) >= 2 and t not in {"询问", "确认", "检查", "提供", "方式", "是否"})
        if not values:
            return False
        compact_user = "".join(str(user_text or "").split())
        return any("".join(v.split()) and "".join(v.split()) in compact_user for v in values)

    def _evidence_group_weight(self, req: Requirement, group_id: str) -> float:
        for group in req.evidence_groups:
            if group.id == group_id:
                return max(0.0, group.weight)
        return 1.0

    def _group_weight(self, node: GraphNode, group_id: str) -> float:
        for req in node.requirements:
            for group in req.evidence_groups:
                if group.id == group_id:
                    return max(0.0, group.weight)
        return 1.0


    def _activation_element_match(self, node: GraphNode, dialogue_atoms) -> bool:
        activation = node.activation
        primary = list(getattr(activation, "primary_elements", []) or [])
        if not primary and getattr(activation, "trigger_object", None):
            obj = activation.trigger_object if isinstance(activation.trigger_object, dict) else {}
            primary = list(obj.get("primary_elements") or obj.get("required_elements") or [])
            if not primary:
                vals = []
                for key in ("surface_forms", "semantic_equivalents", "aliases"):
                    val = obj.get(key)
                    if isinstance(val, str): vals.append(val)
                    elif isinstance(val, list): vals.extend(str(x) for x in val if str(x or "").strip())
                primary = [{"type": "surface", "value": v} for v in vals]
        if not primary and not getattr(activation, "element_groups", None):
            return False
        rule = self.element_engine.make_rule(
            "activation::" + node.id,
            "node_trigger",
            primary=primary,
            secondary=getattr(activation, "secondary_elements", {}) or {},
            zero=[],
            policy=getattr(activation, "match_policy", {}) or {},
            element_groups=getattr(activation, "element_groups", []) or [],
        )
        scoped_atoms = self._scope_atoms_for_role(dialogue_atoms, self._requested_speaker(primary, default="user"))
        return self.element_engine.match_rule(rule, scoped_atoms).verdict == "hit"

    def _requested_speaker(self, elements: list[dict[str, Any]] | None, default: str = "assistant") -> str | None:
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

    def _scope_atoms_for_role(self, atoms, speaker: str | None, fallback_to_all: bool = True):
        if speaker is None or str(speaker).lower() in {"", "any", "all", "both", "任意", "双方"}:
            return list(atoms or [])
        scoped = [a for a in (atoms or []) if getattr(a, "speaker", None) == speaker]
        if scoped or not fallback_to_all:
            return scoped
        return list(atoms or [])


    def _first_activation_element_turn(self, node: GraphNode, units: list[EvidenceUnit]) -> int | None:
        activation = node.activation
        primary = list(getattr(activation, "primary_elements", []) or [])
        groups = getattr(activation, "element_groups", []) or []
        if not primary and not groups:
            return None
        user_units = [u for u in units if u.speaker == "user"]
        if not user_units:
            return None
        atoms = self.element_engine.build_atoms(user_units)
        rule = self.element_engine.make_rule(
            "activation_first_turn::" + node.id,
            "node_trigger",
            primary=primary,
            secondary=getattr(activation, "secondary_elements", {}) or {},
            zero=[],
            policy=getattr(activation, "match_policy", {}) or {},
            element_groups=groups,
        )
        match = self.element_engine.match_rule(rule, atoms)
        if match.verdict == "hit" and match.atom is not None:
            return match.atom.turn_index
        return None

    def _first_condition_trigger_turn(self, node: GraphNode, units: list[EvidenceUnit]) -> int | None:
        if node.activation.mode not in {"user_triggered", "condition", "optional"}:
            return None
        user_units = [u for u in units if u.speaker == "user"]
        for pat in node.activation.patterns:
            for u in user_units:
                if self.strict_matcher._match_pattern(pat, u, units):
                    if not self._condition_trigger_is_only_neutral_context(node, pat, u.text):
                        return u.turn_index
        element_turn = self._first_activation_element_turn(node, units)
        if element_turn is not None:
            return element_turn
        trigger_hit = self._scan_activation_trigger_object(node, units)
        if trigger_hit and trigger_hit.turn_index is not None:
            return trigger_hit.turn_index
        return None

    def _scan_activation_trigger_object(self, node: GraphNode, units: list[EvidenceUnit]):
        """Find node activation evidence from activation.trigger_object.

        Node trigger points are target-to-evidence checks, just like node
        requirements and hard-constraint triggers.  The object terms come from
        the schema; the evaluator only supplies the generic scanner.
        """
        trigger_object = getattr(node.activation, "trigger_object", {}) or {}
        if not isinstance(trigger_object, dict) or not trigger_object:
            return None
        hit = self.positive_object_scanner.scan(trigger_object, units, speaker="user")
        if hit.matched:
            return hit
        return None

    def _generic_condition_branch_handled(self, node: GraphNode, units: list[EvidenceUnit]) -> bool:
        """Fallback for active branch nodes with over-specific evidence groups."""
        trigger_turn = self._first_condition_trigger_turn(node, units)
        if trigger_turn is None:
            return False
        trigger_values: list[str] = []
        for pat in node.activation.patterns:
            trigger_values.extend(str(x or "") for x in list(pat.get("any") or []) + list(pat.get("all") or []))
        trigger_values = ["".join(x.split()) for x in trigger_values if "".join(x.split())]
        action_markers = ("处理", "操作", "配置", "选择", "保存", "入口", "页面", "引导", "说明", "按需", "需要", "可以", "建议")
        for u in units:
            if u.speaker != "assistant" or u.turn_index <= trigger_turn:
                continue
            t = "".join(str(u.text or "").split())
            if not t:
                continue
            if not any(v and v in t for v in trigger_values):
                continue
            if any(a in t for a in action_markers):
                return True
        return False

    def _node_active(self, node: GraphNode, units: list[EvidenceUnit], dialogue_atoms=None) -> bool:
        """Decide whether a node belongs to the reached state-graph path.

        The evaluator treats nodes as states, not as a flat checklist.  Mainline
        states are active by default; branch / FAQ / out-of-scope / terminal
        states are active only when their trigger or their own assistant evidence
        appears.  This prevents an unused branch from dragging positive samples
        down while still allowing a branch answer to be scored if the assistant
        actually gives it.
        """
        mode = str(node.activation.mode or "always").strip().lower()
        node_kind = str(getattr(node, "node_type", "process") or "process").strip().lower()
        dialogue_atoms = dialogue_atoms or []
        activation_hit = self._activation_element_match(node, dialogue_atoms)
        local_evidence_hit = self._node_has_local_evidence(node, dialogue_atoms)
        # Branch activation must follow the state graph trigger.  Earlier builds
        # let any assistant-side local evidence pull a condition branch into the
        # active path; generic safety or empathy expressions could therefore
        # activate a refusal/retention branch in an otherwise normal positive
        # call.  Keep assistant self-activation only for FAQ/out_of_scope style
        # nodes or when the graph explicitly opts in via match_policy.
        allow_self_activation = bool((getattr(node.activation, "match_policy", {}) or {}).get("allow_assistant_self_activation"))
        if node_kind in {"faq", "out_of_scope"}:
            allow_self_activation = True
        # Terminal states are path gates. They must be entered by an explicit
        # user/context trigger; otherwise an assistant's safe closing sentence
        # would activate a terminal branch and suppress normal mainline scoring.
        if node_kind == "terminal":
            trigger_or_local = activation_hit
        elif allow_self_activation:
            trigger_or_local = activation_hit or local_evidence_hit
        else:
            trigger_or_local = activation_hit

        if mode == "optional":
            return bool(trigger_or_local)

        if mode in {"user_triggered", "condition"}:
            if trigger_or_local:
                return True
            # A condition/user-triggered node can still be a mainline stage if
            # LLM used a conservative activation label but the topology says
            # it is on the required chain.  Branch-like node types never get
            # this promotion.
            if self._is_mainline_node(node) and bool(getattr(node, "required", False)) and self._has_unconditional_incoming(node.id) and not self._has_conditional_incoming(node.id):
                return True
            if self._is_mainline_node(node) and bool(getattr(node, "required", False)) and not self._incoming_edges(node.id):
                return True
            return False

        if mode == "always":
            # A node explicitly typed as a branch/FAQ/out_of_scope/terminal is
            # still trigger-driven even if the model forgot to set activation
            # mode.  This keeps structural branches strict.
            if not self._is_mainline_node(node):
                return bool(trigger_or_local)
            return True

        # Unknown modes are treated conservatively.  Mainline states stay active;
        # branch-like states require evidence.
        return True if self._is_mainline_node(node) else bool(trigger_or_local)

    def _is_mainline_node(self, node: GraphNode) -> bool:
        kind = str(getattr(node, "node_type", "process") or "process").strip().lower()
        return kind in {"", "start", "main", "mainline", "process", "normal", "core"}

    def _node_has_local_evidence(self, node: GraphNode, dialogue_atoms) -> bool:
        """Allow a branch node to become active if the assistant actually answered it.

        This is important for natural calls where the model may answer a FAQ
        before/without a clean user trigger.  It is still schema-driven because
        evidence is matched only against the node's own atoms.
        """
        # Branch activation by local assistant evidence must be stricter than
        # ordinary node scoring.  A partial/review hit is enough to contribute
        # score after a branch is already active, but it should not by itself
        # pull an unused branch into the current state path.  Otherwise common
        # fragments can falsely activate refusal, FAQ or terminal branches.
        hit_threshold = float(self.runtime.get("thresholds", {}).get("atom_hit", 0.72))
        for req in getattr(node, "requirements", []) or []:
            try:
                match = self._match_requirement_elements(req, dialogue_atoms or [])
            except Exception:
                continue
            if match.verdict == "hit" and float(match.score or 0.0) >= hit_threshold:
                return True
        return False

    def _edge_kind(self, edge) -> str:
        raw = str(getattr(edge, "relation", "") or "").strip().lower()
        aliases = {
            "before": "before",
            "required_after": "before",
            "soft_order": "before",
            "strict_order": "strict_before",
            "strict_before": "strict_before",
            "ordered": "strict_before",
            "condition_on": "condition_on",
            "conditional": "condition_on",
            "if_then": "condition_on",
            "branch": "branch_choice",
            "branch_choice": "branch_choice",
            "choice": "branch_choice",
            "optional_after": "optional_after",
            "terminal": "terminal_after",
            "terminal_after": "terminal_after",
            "suppress_after": "suppress_after",
            "stop_after": "suppress_after",
        }
        return aliases.get(raw, raw or "before")

    def _incoming_edges(self, node_id: str):
        return [e for e in self.graph.edges if e.target == node_id]

    def _outgoing_edges(self, node_id: str):
        return [e for e in self.graph.edges if e.source == node_id]

    def _has_unconditional_incoming(self, node_id: str) -> bool:
        return any(self._edge_kind(e) in {"before", "strict_before"} for e in self._incoming_edges(node_id))

    def _has_conditional_incoming(self, node_id: str) -> bool:
        return any(self._edge_kind(e) in {"condition_on", "branch_choice", "optional_after"} for e in self._incoming_edges(node_id))

    def _downstream_nodes(self, start_id: str) -> set[str]:
        out: set[str] = set()
        stack = [start_id]
        while stack:
            current = stack.pop()
            for edge in self._outgoing_edges(current):
                target = edge.target
                if target in out:
                    continue
                out.add(target)
                stack.append(target)
        out.discard(start_id)
        return out

    def _resolve_structural_transitions(self, node_results: list[NodeResult], units: list[EvidenceUnit] | None = None) -> tuple[list[ContextEvent], set[str]]:
        """Apply graph-level terminal/suppress transitions.

        Terminal/suppress edges are structural path gates.  Once the gate is
        reached, explicitly suppressed nodes or downstream nodes are marked
        not-applicable instead of being scored as missing.  The transition is
        still audited later by relation scoring if the dialogue continues into
        the suppressed path.
        """
        by_id = {n.node_id: n for n in node_results}
        suppressed: set[str] = set()
        events: list[ContextEvent] = []
        satisfied = float(self.runtime.get("thresholds", {}).get("node_partial", 0.35))

        def is_triggered(result: NodeResult | None) -> bool:
            return bool(result and result.active and (result.first_hit_turn is not None or float(result.score or 0.0) >= satisfied))

        for edge in self.graph.edges:
            kind = self._edge_kind(edge)
            if kind not in {"terminal_after", "suppress_after"}:
                continue
            source_node = by_id.get(edge.source)
            target_node = by_id.get(edge.target)
            # terminal_after means entering the target terminal state closes the
            # path.  suppress_after may be modelled as either source-triggered or
            # target-triggered, so accept the earliest triggered endpoint.
            trigger_node = None
            if kind == "terminal_after":
                target_schema = next((n for n in self.graph.nodes if n.id == edge.target), None)
                target_user_turn = self._first_condition_trigger_turn(target_schema, units or []) if target_schema is not None else None
                if target_node is not None and is_triggered(target_node) and target_user_turn is not None:
                    # Terminal states must be entered by user/context trigger, not merely by an assistant safety reminder.
                    trigger_node = NodeResult(target_node.node_id, target_node.name, target_node.status, target_node.score, target_node.active, target_node.group_matches, target_node.requirement_results, target_user_turn, aliases=target_node.aliases)
                else:
                    trigger_node = (source_node if is_triggered(source_node) and target_node is None else None)
                downstream_seed = edge.target
            else:
                candidates = [x for x in (source_node, target_node) if is_triggered(x)]
                trigger_node = min(candidates, key=lambda x: x.first_hit_turn if x.first_hit_turn is not None else 10**9) if candidates else None
                downstream_seed = edge.target
            if trigger_node is None:
                continue
            effect = getattr(edge, "terminal_effect", {}) or {}
            explicit = effect.get("suppress_nodes") if isinstance(effect, dict) else None
            if isinstance(explicit, list) and explicit:
                nodes = {str(x) for x in explicit if str(x or "").strip()}
            else:
                nodes = self._downstream_nodes(downstream_seed)
            nodes.discard(edge.source)
            nodes.discard(edge.target)
            if not nodes:
                continue
            suppressed.update(nodes)
            events.append(ContextEvent(
                f"state_transition::{edge.source}->{edge.target}",
                "已处理",
                trigger_node.first_hit_turn,
                trigger_node.first_hit_turn,
                sorted(nodes),
                "状态图终止/抑制转场已触发，后续节点不再作为当前路径必需项评分",
            ))
        return events, suppressed

    def _condition_trigger_is_only_neutral_context(self, node: GraphNode, pattern: dict[str, object], text: str) -> bool:
        """Prevent condition branches from firing on a neutral channel mention.

        A common real-LLM graph error is to build a repair/enable branch whose
        activation any-list mixes the neutral object/channel with the actual
        problem signal.  The evaluator should not activate that branch when the
        user merely says they use/see the channel.  The logic is structural and
        uses only generic UI/repair operators plus the current node/pattern.
        """
        node_text = "".join(str(x or "") for x in [node.id, node.name, *node.aliases]).lower()
        for req in node.requirements:
            node_text += " " + str(req.id).lower() + " " + str(req.text).lower()
        action_like = any(x in node_text for x in ("enable", "repair", "configure", "open", "guide", "配置", "引导", "修复", "处理"))
        if not action_like:
            return False
        values = [str(x or "") for x in list(pattern.get("any") or []) + list(pattern.get("all") or [])]
        compact_values = ["".join(v.split()) for v in values]
        problem_terms = ("不可见", "不能用", "无法使用")
        if not any(any(p in v for p in problem_terms) for v in compact_values):
            return False
        t = "".join(str(text or "").split())
        if any(x in t for x in ("已显示", "已经显示", "能看到", "看得到", "看到了", "可以看到")):
            return True
        if any(x in t for x in problem_terms):
            return False
        # The trigger matched only a neutral object/channel value, not the
        # problem condition that makes the branch actionable.
        return True

    def _allows_continue(self, text: str) -> bool:
        """Detect generic user permission to continue after a soft trigger.

        This is not task semantics.  It only prevents broad inconvenience words
        from being treated as a forced terminal branch when the user explicitly
        says the agent may continue.
        """
        compact = "".join(str(text or "").split())
        if not compact:
            return False
        if any(x in compact for x in USER_STOP_CUES):
            return False
        return any(x in compact for x in USER_CONTINUE_CUES)

    def _loose_context_value_match(self, value: object, text: str) -> bool:
        a = "".join(str(value or "").split())
        b = "".join(str(text or "").split())
        if not a or not b:
            return False
        if a in b:
            return True
        if min(len(a), len(b)) < 3:
            return False
        common = len(set(a) & set(b))
        return common >= 2 and common / max(1, min(len(set(a)), len(set(b)))) >= 0.5

    def _loose_context_pattern_match(self, pattern: dict[str, object], unit: EvidenceUnit) -> bool:
        speaker = pattern.get("speaker")
        if speaker and unit.speaker != speaker:
            return False
        values = list(pattern.get("any") or []) + list(pattern.get("all") or [])
        return bool(values) and any(self._loose_context_value_match(v, unit.text) for v in values)

    def _auto_terminal_suppress_nodes(self, units: list[EvidenceUnit], handled_turn: int) -> list[str]:
        """Infer remaining nodes suppressed by a handled terminal policy.

        LLM sometimes emits a terminal policy without an explicit
        suppress_nodes list.  In that case, after the user-triggered terminal
        condition is safely handled, nodes that still have no evidence up to
        the handling turn should not be forced as missing.  This is structural
        and schema-driven: it uses only current graph nodes and evidence groups.
        """
        suppress: list[str] = []
        prefix_units = [u for u in units if u.turn_index <= handled_turn]
        for node in self.graph.nodes:
            if str(getattr(node, "type", "")) == "terminal":
                continue
            has_prefix_hit = False
            for req in node.requirements:
                for group_match in self.matcher.match_groups(req.evidence_groups, prefix_units):
                    if group_match.hits:
                        has_prefix_hit = True
                        break
                if has_prefix_hit:
                    break
            if not has_prefix_hit:
                suppress.append(node.id)
        return suppress

    def _resolve_context(self, units: list[EvidenceUnit]) -> tuple[list[ContextEvent], set[str]]:
        events: list[ContextEvent] = []
        suppressed: set[str] = set()
        for raw in self.graph.terminal_policies:
            if not isinstance(raw, dict):
                continue
            policy_id = str(raw.get("id") or "terminal_policy")
            trigger_pats = list(raw.get("trigger") or [])
            safe_pats = list(raw.get("safe_response") or raw.get("handling") or raw.get("resolution") or [])
            forbidden_after = list(raw.get("forbidden_after_safe_response") or [])
            suppress_nodes = [str(x) for x in raw.get("suppress_nodes_after_safe_response") or raw.get("suppress_nodes") or []]
            trigger_units = [
                u
                for u in units
                if u.speaker == "user"
                and any(self.strict_matcher._match_pattern(p, u, units) for p in trigger_pats)
                and not self._allows_continue(u.text)
            ]
            if not trigger_units:
                continue
            trigger = trigger_units[0]
            later_assistant = [u for u in units if u.speaker == "assistant" and u.turn_index > trigger.turn_index]
            handling = next((u for u in later_assistant if any(self.matcher._match_pattern(p, u, units) for p in safe_pats)), None)
            if not handling:
                handling = next((u for u in later_assistant if any(self._loose_context_pattern_match(p, u) for p in safe_pats)), None)
            if not handling:
                events.append(ContextEvent(policy_id, "触发但未处理", trigger.turn_index, None, [], "用户触发条件，但没有找到图中要求的处理证据"))
                continue
            violation = None
            if forbidden_after:
                after_handling = [u for u in later_assistant if u.turn_index > handling.turn_index]
                violation = next((u for u in after_handling if any(self.matcher._match_pattern(p, u, units) for p in forbidden_after)), None)
            if violation:
                events.append(ContextEvent(policy_id, "已处理但后续违背转场", trigger.turn_index, handling.turn_index, [], "用户触发条件后已有处理，但后续又出现图中禁止的推进证据"))
            else:
                auto_nodes = self._auto_terminal_suppress_nodes(units, handling.turn_index)
                for nid in auto_nodes:
                    if nid not in suppress_nodes:
                        suppress_nodes.append(nid)
                terminal_node_ids = [n.id for n in self.graph.nodes if str(getattr(n, "node_type", "")) == "terminal"]
                conditional_node_ids = [n.id for n in self.graph.nodes if str(getattr(n.activation, "mode", "")) in {"user_triggered", "condition"}]
                for nid in [*terminal_node_ids, *conditional_node_ids]:
                    if nid not in suppress_nodes:
                        suppress_nodes.append(nid)
                suppressed.update(suppress_nodes)
                events.append(ContextEvent(policy_id, "已处理", trigger.turn_index, handling.turn_index, suppress_nodes, "用户触发条件后，客服给出了图中要求的处理证据"))
        handled_terminal_turns = [e.handling_turn for e in events if e.status == "已处理" and e.handling_turn is not None]
        if handled_terminal_turns:
            latest_handled = max(handled_terminal_turns)
            normalized: list[ContextEvent] = []
            for e in events:
                if e.status == "触发但未处理" and e.trigger_turn is not None and e.trigger_turn <= latest_handled:
                    normalized.append(ContextEvent(e.policy_id, "已处理", e.trigger_turn, latest_handled, e.suppressed_nodes, "已由后续更高优先级的安全转场处理覆盖"))
                else:
                    normalized.append(e)
            events = normalized
        return events, suppressed

    def _score(self, node_results: list[NodeResult], knowledge_events: list[KnowledgeEvent], constraint_events: list[ConstraintEvent], context_events: list[ContextEvent], constraint_checks: list[ConstraintCheck] | None = None) -> tuple[dict[str, float], list[dict[str, Any]], list[RelationEvent]]:
        active_required = [n for n in node_results if n.active]
        node_completion = 100.0 * (sum(n.score for n in active_required) / max(1, len(active_required)))
        relation_score, relation_events = self._relation_score(node_results)
        relation_score = max(0.0, relation_score - self._context_penalty(context_events))
        knowledge_score = max(0.0, 100.0 - sum(self._severity_penalty(e.severity, "knowledge") for e in knowledge_events))
        constraint_score = max(0.0, 100.0 - sum(self._severity_penalty(e.severity, "constraint") for e in constraint_events))
        soft_checks = [c for c in (constraint_checks or []) if getattr(c, "enforcement", "hard") == "soft"]
        soft_score = 100.0 if not soft_checks else 100.0 * sum(float(getattr(c, "score", 1.0) or 0.0) for c in soft_checks) / max(1, len(soft_checks))
        node_completion, relation_score, knowledge_score, constraint_score, component_caps = self._apply_component_score_caps(
            node_completion, relation_score, knowledge_score, constraint_score,
            knowledge_events, constraint_events, node_results, context_events,
        )
        weights = self._normalized_weights()
        total = (
            node_completion * weights["node_completion"]
            + relation_score * weights["relation_score"]
            + knowledge_score * weights["knowledge_score"]
            + constraint_score * weights["constraint_score"]
            + soft_score * weights["soft_constraint_score"]
        )
        total, caps = self._apply_caps(total, knowledge_events, constraint_events, node_results, context_events)
        caps = component_caps + caps
        return (
            {
                "total": round(total, 2),
                "node_completion": round(node_completion, 2),
                "relation_score": round(relation_score, 2),
                "knowledge_score": round(knowledge_score, 2),
                "constraint_score": round(constraint_score, 2),
                "soft_constraint_score": round(soft_score, 2),
            },
            caps,
            relation_events,
        )

    def _normalized_weights(self) -> dict[str, float]:
        defaults = {
            "node_completion": 0.5,
            "relation_score": 0.1,
            "knowledge_score": 0.2,
            "constraint_score": 0.15,
            "soft_constraint_score": 0.05,
        }
        configured = self.runtime.get("weights", {}) if isinstance(self.runtime.get("weights", {}), dict) else {}
        weights = {k: float(configured.get(k, v)) for k, v in defaults.items()}
        total = sum(max(0.0, v) for v in weights.values())
        if total <= 0:
            return defaults
        return {k: max(0.0, v) / total for k, v in weights.items()}

    def _context_penalty(self, events: list[ContextEvent]) -> float:
        penalty = 0.0
        for event in events:
            if event.status == "触发但未处理":
                penalty += 12.0
            elif event.status == "已处理但后续违背转场":
                penalty += 18.0
        return penalty

    def _relation_score(self, node_results: list[NodeResult]) -> tuple[float, list[RelationEvent]]:
        result_by_id = {n.node_id: n for n in node_results}
        pair_scores: list[float] = []
        penalty = 0.0
        events: list[RelationEvent] = []
        for edge in self.graph.edges:
            kind = self._edge_kind(edge)
            if kind not in {"before", "strict_before", "condition_on", "branch_choice", "optional_after", "terminal_after", "suppress_after"}:
                continue
            a = result_by_id.get(edge.source)
            b = result_by_id.get(edge.target)
            if not a or not b:
                continue
            # Optional/conditional edges are evaluated only when the target path is active.
            if kind in {"condition_on", "branch_choice", "optional_after"} and not b.active:
                continue
            # optional_after is a reachability hint for FAQ / optional context
            # nodes, not a strict order requirement.  Natural calls can ask FAQ
            # before a later mainline explanation, so do not let this edge lower
            # relation_score; node scoring and relation_groups still evaluate
            # the actual optional node if it is triggered.
            if kind == "optional_after":
                continue
            strict = kind in {"strict_before", "condition_on", "branch_choice"}
            if b.active and (not a.active or a.status == "缺失") and kind in {"strict_before", "condition_on", "branch_choice"}:
                p = (8.0 if strict else 4.0) * edge.weight
                penalty += p
                events.append(RelationEvent(kind, edge.source, edge.target, "前置未到达", p, "目标节点已有证据或被触发，但其状态图前置节点未到达或缺少核心证据"))
            if not a.active or not b.active:
                continue
            pair_scores.append((a.score + b.score) / 2.0)
            if a.status == "缺失" and b.status != "缺失":
                p = (8.0 if strict else 4.0) * edge.weight
                penalty += p
                events.append(RelationEvent(kind, edge.source, edge.target, "前置缺失", p, "后续节点有证据，但前置节点缺少核心证据"))
            if kind in {"before", "strict_before"} and b.status == "缺失" and a.status != "缺失":
                p = (6.0 if strict else 3.0) * edge.weight
                penalty += p
                events.append(RelationEvent(kind, edge.source, edge.target, "后续缺失", p, "前置节点已触达，但后续节点缺少核心证据"))
            if a.first_hit_turn is not None and b.first_hit_turn is not None and a.first_hit_turn > b.first_hit_turn:
                p = (10.0 if strict else 4.0) * edge.weight
                penalty += p
                events.append(RelationEvent(kind, edge.source, edge.target, "顺序异常", p, "后置节点早于前置节点出现", f"{a.first_hit_turn}>{b.first_hit_turn}"))
            if kind in {"terminal_after", "suppress_after"} and b.first_hit_turn is not None:
                suppressed_after = self._downstream_nodes(edge.target)
                continued = [nid for nid in suppressed_after if result_by_id.get(nid) and result_by_id[nid].first_hit_turn is not None and (result_by_id[nid].first_hit_turn or -1) > (b.first_hit_turn or -1)]
                if continued:
                    p = 8.0 * edge.weight
                    penalty += p
                    events.append(RelationEvent(kind, edge.target, ",".join(continued), "终止后仍推进", p, "终止/转场节点触发后仍继续进入后续被抑制节点"))
        group_score, group_events = self._relation_group_score(result_by_id)
        atom_penalty, atom_events = self._atom_relation_events(result_by_id)
        events.extend(group_events)
        events.extend(atom_events)
        edge_base = 100.0 if not pair_scores else 100.0 * sum(pair_scores) / len(pair_scores)
        if self.graph.relation_groups:
            base = edge_base * 0.55 + group_score * 0.45
        else:
            base = edge_base
        score = max(0.0, base - penalty - atom_penalty)
        if not events and (self.graph.edges or self.graph.relation_groups or any(getattr(n, "atom_relations", None) for n in self.graph.nodes)):
            events.append(RelationEvent("summary", "", "", "结构正常", 0.0, "已评估状态图节点和 atom 之间的顺序、承接、分支和关系完整性"))
        return score, events

    def _requirement_first_turn(self, result: RequirementResult | None) -> int | None:
        if result is None:
            return None
        turns = [h.turn_index for g in result.group_matches for h in g.hits if h is not None]
        return min(turns) if turns else None

    def _relation_ids(self, rel: dict[str, Any], *keys: str) -> list[str]:
        out: list[str] = []
        for key in keys:
            value = rel.get(key)
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, (list, tuple, set)):
                out.extend(str(x) for x in value if str(x or "").strip())
        return [x for x in dict.fromkeys(out) if x]

    def _atom_relation_events(self, result_by_id: dict[str, NodeResult]) -> tuple[float, list[RelationEvent]]:
        penalty = 0.0
        events: list[RelationEvent] = []
        for node in self.graph.nodes:
            node_result = result_by_id.get(node.id)
            if not node_result or not node_result.active:
                continue
            reqs = {r.requirement_id: r for r in node_result.requirement_results}
            for rel in getattr(node, "atom_relations", []) or []:
                if not isinstance(rel, dict):
                    continue
                rtype = str(rel.get("type") or rel.get("relation") or "").strip()
                rid = str(rel.get("id") or rtype or "atom_relation")
                left = self._relation_ids(rel, "source", "from", "left", "condition", "trigger", "terminal", "after")
                right = self._relation_ids(rel, "target", "to", "right", "consequence", "then", "blocked", "requires")
                options = self._relation_ids(rel, "options", "members", "atoms", "atom_ids")

                def matched(atom_id: str) -> bool:
                    return bool(reqs.get(atom_id) and reqs[atom_id].matched)

                def turn(atom_id: str) -> int | None:
                    return self._requirement_first_turn(reqs.get(atom_id))

                if rtype in {"any_of", "choice", "one_of"}:
                    atoms = options or left or right
                    if atoms and not any(matched(x) for x in atoms):
                        p = float(rel.get("penalty", 6.0))
                        penalty += p
                        events.append(RelationEvent("atom:any_of", node.id, ",".join(atoms), "替代 atom 未命中", p, f"{rid}: 该 atom 关系要求至少命中一个候选"))
                    continue

                if rtype in {"unordered_all", "all_of", "unordered"}:
                    atoms = options or left + right
                    missing = [x for x in atoms if x in reqs and not matched(x)]
                    if missing:
                        p = float(rel.get("penalty", 4.0)) * len(missing)
                        penalty += p
                        events.append(RelationEvent("atom:unordered_all", node.id, ",".join(missing), "组内 atom 缺失", p, f"{rid}: 无序 atom 组要求全部完成"))
                    continue

                if rtype in {"condition_on", "conditional", "if_then"}:
                    if left and right and any(matched(x) for x in left):
                        miss = [x for x in right if x in reqs and not matched(x)]
                        if miss:
                            p = float(rel.get("penalty", 6.0))
                            penalty += p
                            events.append(RelationEvent("atom:condition_on", ",".join(left), ",".join(miss), "条件触发后 atom 缺失", p, f"{rid}: 条件 atom 已触发，但后续 atom 未完成"))
                    continue

                if rtype in {"before", "strict_before", "before_or_same_turn"}:
                    for a in left[:1]:
                        for b in right[:1]:
                            ta, tb = turn(a), turn(b)
                            if matched(b) and not matched(a):
                                p = float(rel.get("penalty", 5.0))
                                penalty += p
                                events.append(RelationEvent("atom:" + rtype, a, b, "前置 atom 缺失", p, f"{rid}: 后置 atom 有证据但前置 atom 未命中"))
                            elif ta is not None and tb is not None:
                                bad_order = ta > tb if rtype == "before_or_same_turn" else ta >= tb
                                if bad_order:
                                    p = float(rel.get("penalty", 7.0))
                                    penalty += p
                                    events.append(RelationEvent("atom:" + rtype, a, b, "atom 顺序异常", p, f"{rid}: atom 出现顺序不符合关系", f"{ta}>{tb}"))
                    continue

                if rtype in {"blocking", "terminal_after", "terminal"}:
                    for a in left[:1]:
                        ta = turn(a)
                        if ta is None:
                            continue
                        blocked = [x for x in right if turn(x) is not None and (turn(x) or -1) > ta]
                        if blocked:
                            p = float(rel.get("penalty", 8.0))
                            penalty += p
                            events.append(RelationEvent("atom:" + rtype, a, ",".join(blocked), "终止后仍推进", p, f"{rid}: terminal/blocking atom 命中后仍出现被阻断 atom"))
        return penalty, events

    def _relation_group_score(self, result_by_id: dict[str, NodeResult]) -> tuple[float, list[RelationEvent]]:
        if not self.graph.relation_groups:
            return 100.0, []
        scores: list[float] = []
        events: list[RelationEvent] = []
        for group in self.graph.relation_groups:
            score, event = self._score_relation_group(group, result_by_id)
            scores.append(score * group.weight)
            if event:
                events.append(event)
        weight_sum = sum(max(0.0, g.weight) for g in self.graph.relation_groups) or float(len(self.graph.relation_groups))
        return max(0.0, min(100.0, sum(scores) / weight_sum)), events

    def _score_relation_group(self, group: RelationGroup, result_by_id: dict[str, NodeResult]) -> tuple[float, RelationEvent | None]:
        nodes = [result_by_id[n] for n in group.nodes if n in result_by_id and result_by_id[n].active]
        if not nodes:
            return 100.0, None
        completed = [n for n in nodes if n.status == "已完成"]
        scored_mean = 100.0 * sum(n.score for n in nodes) / len(nodes)
        gtype = str(group.group_type or "").lower()
        if gtype in {"any_of", "one_of", "choice", "branch_choice", "exclusive_branch"}:
            ok = bool(completed)
            score = max((n.score for n in nodes), default=0.0) * 100.0
            if gtype == "exclusive_branch":
                chosen = [n for n in nodes if n.first_hit_turn is not None or n.status in {"已完成", "部分完成"}]
                if len(chosen) > 1:
                    penalty = 10.0 * (len(chosen) - 1)
                    score = max(0.0, score - penalty)
                    return score, RelationEvent("group:exclusive_branch", group.id, ",".join(n.node_id for n in chosen), "互斥分支重复触发", penalty, group.description or "关系组要求在互斥分支中选择一条路径")
            if not ok and group.required:
                return score, RelationEvent("group:any_of", group.id, ",".join(group.nodes), "替代分支未命中", 8.0, group.description or "关系组要求至少一个分支完成")
            return score, None
        if gtype in {"ordered", "strict_order", "sequential", "before"}:
            penalty = 0.0
            first_hits = [n.first_hit_turn for n in nodes]
            for i in range(1, len(first_hits)):
                if first_hits[i - 1] is not None and first_hits[i] is not None and first_hits[i - 1] > first_hits[i]:
                    penalty += 8.0
            score = max(0.0, scored_mean - penalty)
            if penalty:
                return score, RelationEvent("group:ordered", group.id, ",".join(group.nodes), "组内顺序异常", penalty, group.description or "关系组内节点出现顺序不符合图结构")
            return score, None
        if gtype in {"optional_parallel", "optional", "parallel_optional"}:
            return scored_mean, None
        required_count = group.min_completed if group.min_completed is not None else len(nodes)
        ok = len(completed) >= min(required_count, len(nodes))
        if not ok and group.required:
            return scored_mean, RelationEvent("group:all_of", group.id, ",".join(group.nodes), "关系组未完整", 6.0, group.description or "关系组内必需节点没有充分完成")
        return scored_mean, None

    def _severity_penalty(self, severity: str, kind: str) -> float:
        base = {"low": 5.0, "medium": 12.0, "high": 28.0, "critical": 45.0}.get(severity, 12.0)
        return base if kind == "constraint" else base * 0.8

    def _apply_component_score_caps(
        self,
        node_completion: float,
        relation_score: float,
        knowledge_score: float,
        constraint_score: float,
        knowledge_events: list[KnowledgeEvent],
        constraint_events: list[ConstraintEvent],
        node_results: list[NodeResult],
        context_events: list[ContextEvent],
    ) -> tuple[float, float, float, float, list[dict[str, Any]]]:
        """Synchronize visible component scores with local issue families.

        The total score cap already separates positive and negative packages, but
        reports must also show *which dimension* caused the separation.  This
        helper is label-free: it only reads active node/atom misses, fact
        conflicts, hard-limit violations, and terminal/context events already
        emitted by the evaluator.  It never reads sample_type, injected_errors,
        evidence_span, or wrong_statement.
        """
        thresholds = self.runtime.get("thresholds", {}) if isinstance(self.runtime.get("thresholds", {}), dict) else {}
        profile = thresholds.get("score_separation_caps") if isinstance(thresholds.get("score_separation_caps"), dict) else {}
        if profile and not bool(profile.get("enabled", True)):
            return node_completion, relation_score, knowledge_score, constraint_score, []

        def cfg(name: str, default: float) -> float:
            try:
                return float(profile.get(name, default))
            except Exception:
                return float(default)

        def sev_rank(sev: str) -> int:
            return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(sev or "").lower(), 2)

        caps: list[dict[str, Any]] = []

        def cap_component(key: str, current: float, cap_value: float, reason: str, meta: dict[str, Any] | None = None) -> float:
            cap_value = max(0.0, min(100.0, float(cap_value)))
            if current > cap_value:
                caps.append({
                    "component": key,
                    "cap": round(cap_value, 4),
                    "from": round(float(current), 4),
                    "reason": reason,
                    "component_score_separation": True,
                    **(meta or {}),
                })
                return cap_value
            return current

        active_nodes = [n for n in node_results if n.active]
        missing_nodes = [n for n in active_nodes if n.status == "缺失"]
        partial_nodes = [n for n in active_nodes if n.status == "部分完成"]
        required_groups = 0
        missing_groups = 0
        partial_groups = 0
        for n in active_nodes:
            for req in n.requirement_results:
                if not req.required:
                    continue
                for group in req.group_matches:
                    if not group.required:
                        continue
                    required_groups += 1
                    if not group.matched:
                        missing_groups += 1
                    elif float(getattr(group, "score", 1.0) or 0.0) < float(thresholds.get("node_satisfied", 0.75)):
                        partial_groups += 1
        missing_ratio = missing_groups / max(1, required_groups)

        if missing_nodes or missing_groups:
            if len(missing_nodes) >= 2 or missing_groups >= 2 or missing_ratio >= 0.25:
                node_completion = cap_component(
                    "node_completion", node_completion, cfg("flow_missing_multiple_cap", 58.0),
                    "多个活动评估原子/节点缺失，节点小分同步拉开",
                    {"missing_required_groups": missing_groups, "required_groups": required_groups, "active_missing_nodes": [n.node_id for n in missing_nodes]},
                )
            else:
                node_completion = cap_component(
                    "node_completion", node_completion, cfg("flow_missing_single_cap", 66.0),
                    "活动评估原子/节点缺失，节点小分同步拉开",
                    {"missing_required_groups": missing_groups, "required_groups": required_groups, "active_missing_nodes": [n.node_id for n in missing_nodes]},
                )
        elif partial_nodes or partial_groups:
            # A local element-level ``hit`` whose score is slightly below the
            # satisfied threshold is useful audit information, but it is not a
            # confirmed flow-missing event.  Component-score separation should
            # be driven by real local misses/conflicts/violations; otherwise one
            # low-confidence but valid atom caps clean positive merchant calls
            # at 84 and hides the evaluator's raw high node score.  Keep the
            # partial evidence in node/atom ledgers and acceptance diagnostics,
            # but do not force a component cap unless the runtime explicitly
            # opts into the old conservative behaviour.
            if bool(profile.get("cap_partial_hits", False)):
                node_completion = cap_component(
                    "node_completion", node_completion, cfg("flow_partial_cap", 94.0),
                    "活动评估原子仅部分覆盖，节点小分同步体现低置信",
                    {"partial_required_groups": partial_groups, "active_partial_nodes": [n.node_id for n in partial_nodes]},
                )

        max_k = max((sev_rank(e.severity) for e in knowledge_events), default=0)
        if max_k >= 4:
            knowledge_score = cap_component("knowledge_score", knowledge_score, cfg("knowledge_critical_cap", 48.0), "严重事实冲突，知识小分同步拉开", {"knowledge_severity": "critical"})
        elif max_k == 3:
            knowledge_score = cap_component("knowledge_score", knowledge_score, cfg("knowledge_high_cap", 52.0), "高风险事实冲突，知识小分同步拉开", {"knowledge_severity": "high"})
        elif max_k == 2:
            knowledge_score = cap_component("knowledge_score", knowledge_score, cfg("knowledge_medium_cap", 60.0), "事实冲突，知识小分同步拉开", {"knowledge_severity": "medium"})
        elif max_k == 1:
            knowledge_score = cap_component("knowledge_score", knowledge_score, cfg("knowledge_low_cap", 68.0), "低风险事实冲突，知识小分同步拉开", {"knowledge_severity": "low"})

        max_c = max((sev_rank(e.severity) for e in constraint_events), default=0)
        if max_c >= 4:
            constraint_score = cap_component("constraint_score", constraint_score, cfg("constraint_critical_cap", 38.0), "严重硬限制违规，限制小分同步拉开", {"constraint_severity": "critical"})
        elif max_c == 3:
            constraint_score = cap_component("constraint_score", constraint_score, cfg("constraint_high_cap", 46.0), "高风险硬限制违规，限制小分同步拉开", {"constraint_severity": "high"})
        elif max_c == 2:
            constraint_score = cap_component("constraint_score", constraint_score, cfg("constraint_medium_cap", 54.0), "硬限制违规，限制小分同步拉开", {"constraint_severity": "medium"})
        elif max_c == 1:
            constraint_score = cap_component("constraint_score", constraint_score, cfg("constraint_low_cap", 62.0), "低风险限制违规，限制小分同步拉开", {"constraint_severity": "low"})

        context_problem = any(e.status != "已处理" for e in context_events)
        if context_problem:
            relation_score = cap_component("relation_score", relation_score, cfg("context_cap", 62.0), "条件终止/转场问题，结构关系小分同步拉开", {})

        return node_completion, relation_score, knowledge_score, constraint_score, caps

    def _apply_caps(self, total: float, knowledge_events: list[KnowledgeEvent], constraint_events: list[ConstraintEvent], node_results: list[NodeResult], context_events: list[ContextEvent]) -> tuple[float, list[dict[str, Any]]]:
        """Apply score-separation caps from local evidence events.

        A negative dialogue should not receive a positive-like score once the
        evaluator has independently found a real flow miss, fact conflict,
        hard-limit violation, or terminal/context problem.  Earlier builds used
        additive weighting first and only loose caps later, so a dialogue could
        be accepted as a negative sample while still scoring in the high 90s.

        This cap layer is deliberately label-free: it uses only graph execution
        results already produced by the evaluator.  It does not read
        injected_errors, evidence_span, wrong_statement, or expected labels.
        """
        thresholds = self.runtime.get("thresholds", {}) if isinstance(self.runtime.get("thresholds", {}), dict) else {}
        profile = thresholds.get("score_separation_caps") if isinstance(thresholds.get("score_separation_caps"), dict) else {}
        if profile and not bool(profile.get("enabled", True)):
            return total, []

        def cfg(name: str, default: float) -> float:
            try:
                return float(profile.get(name, default))
            except Exception:
                return float(default)

        caps: list[dict[str, Any]] = []
        candidates: list[tuple[float, str, dict[str, Any]]] = []

        active_nodes = [n for n in node_results if n.active]
        missing_nodes = [n for n in active_nodes if n.status == "缺失"]
        partial_nodes = [n for n in active_nodes if n.status == "部分完成"]
        required_groups = 0
        missing_groups = 0
        partial_groups = 0
        for n in active_nodes:
            for req in n.requirement_results:
                if not req.required:
                    continue
                for group in req.group_matches:
                    if not group.required:
                        continue
                    required_groups += 1
                    if not group.matched:
                        missing_groups += 1
                    elif float(getattr(group, "score", 1.0) or 0.0) < float(thresholds.get("node_satisfied", 0.75)):
                        partial_groups += 1
        missing_ratio = missing_groups / max(1, required_groups)

        def sev_rank(sev: str) -> int:
            return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(sev or "").lower(), 2)

        max_k = max((sev_rank(e.severity) for e in knowledge_events), default=0)
        max_c = max((sev_rank(e.severity) for e in constraint_events), default=0)
        context_problem = any(e.status != "已处理" for e in context_events)
        flow_problem = bool(missing_nodes or missing_groups)
        partial_problem = bool(partial_nodes or partial_groups)
        issue_families = sum(bool(x) for x in (flow_problem, bool(knowledge_events), bool(constraint_events), context_problem))

        if max_c >= 4:
            candidates.append((cfg("constraint_critical_cap", 38.0), "存在严重硬限制违规", {"constraint_severity": "critical"}))
        elif max_c == 3:
            candidates.append((cfg("constraint_high_cap", 46.0), "存在高风险硬限制违规", {"constraint_severity": "high"}))
        elif max_c == 2:
            candidates.append((cfg("constraint_medium_cap", 54.0), "存在硬限制违规", {"constraint_severity": "medium"}))
        elif max_c == 1:
            candidates.append((cfg("constraint_low_cap", 62.0), "存在低风险限制违规", {"constraint_severity": "low"}))

        if max_k >= 4:
            candidates.append((cfg("knowledge_critical_cap", 48.0), "存在严重事实冲突", {"knowledge_severity": "critical"}))
        elif max_k == 3:
            candidates.append((cfg("knowledge_high_cap", 52.0), "存在高风险事实冲突", {"knowledge_severity": "high"}))
        elif max_k == 2:
            candidates.append((cfg("knowledge_medium_cap", 60.0), "存在事实冲突", {"knowledge_severity": "medium"}))
        elif max_k == 1:
            candidates.append((cfg("knowledge_low_cap", 68.0), "存在低风险事实冲突", {"knowledge_severity": "low"}))

        if flow_problem:
            if len(missing_nodes) >= 2 or missing_groups >= 2 or missing_ratio >= 0.25:
                candidates.append((cfg("flow_missing_multiple_cap", 58.0), "多个核心履约证据缺失", {"missing_required_groups": missing_groups, "required_groups": required_groups}))
            else:
                candidates.append((cfg("flow_missing_single_cap", 66.0), "核心履约证据缺失", {"missing_required_groups": missing_groups, "required_groups": required_groups}))
        elif partial_problem:
            # Partial element hits are not treated as confirmed score-capping
            # issues by default.  They remain visible in the element audit, but
            # total score caps are reserved for locally confirmed misses,
            # knowledge conflicts, hard-limit violations, or context errors.
            if bool(profile.get("cap_partial_hits", False)):
                candidates.append((cfg("flow_partial_cap", 94.0), "存在核心履约证据低置信或部分完成", {"partial_required_groups": partial_groups, "partial_nodes": len(partial_nodes)}))

        if context_problem:
            if issue_families >= 2:
                candidates.append((cfg("context_combined_cap", 52.0), "条件转场处理问题与其他错误叠加", {}))
            else:
                candidates.append((cfg("context_cap", 62.0), "存在条件转场处理问题", {}))

        if issue_families >= 2:
            candidates.append((cfg("combined_issue_cap", 42.0), "多类核心错误叠加", {"issue_families": issue_families}))
        elif bool(constraint_events) and (bool(knowledge_events) or flow_problem):
            candidates.append((cfg("constraint_plus_other_cap", 42.0), "硬限制违规与其他错误叠加", {}))
        elif bool(knowledge_events) and flow_problem:
            candidates.append((cfg("knowledge_plus_flow_cap", 52.0), "事实冲突与流程缺失叠加", {}))

        if not candidates:
            return total, []

        cap_value, reason, meta = min(candidates, key=lambda x: x[0])
        if total > cap_value:
            total = cap_value
            caps.append({
                "cap": cap_value,
                "reason": reason,
                "missing_required_groups": missing_groups,
                "required_groups": required_groups,
                "partial_required_groups": partial_groups,
                "active_missing_nodes": [n.node_id for n in missing_nodes],
                "active_partial_nodes": [n.node_id for n in partial_nodes],
                "score_separation": True,
                **meta,
            })
        return total, caps
