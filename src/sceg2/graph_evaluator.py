from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constraint_judge import ConstraintCheck, ConstraintEvent, ConstraintJudge
from .evidence_extractor import EvidenceExtractor
from .evidence_matcher import EvidenceMatcher, GroupMatch
from .evidence_units import DialogueTurn, EvidenceUnit
from .knowledge_judge import KnowledgeCheck, KnowledgeEvent, KnowledgeJudge
from .generic_customer_service_expressions import USER_CONTINUE_CUES, USER_STOP_CUES
from .schema import GraphNode, RelationGroup, Requirement, StateGraph




@dataclass(slots=True)
class RequirementResult:
    requirement_id: str
    text: str
    required: bool
    score: float
    matched: bool
    group_matches: list[GroupMatch] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "text": self.text,
            "required": self.required,
            "score": round(self.score, 4),
            "matched": self.matched,
            "aliases": self.aliases,
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


class GraphEvaluator:
    def __init__(self, graph: StateGraph, runtime: dict[str, Any], extractor: EvidenceExtractor | None = None) -> None:
        self.graph = graph
        self.runtime = runtime
        self.extractor = extractor or EvidenceExtractor()
        broad_terms = self._broad_evidence_terms(graph)
        self.matcher = EvidenceMatcher(enable_fuzzy=True, broad_terms=broad_terms)
        self.strict_matcher = EvidenceMatcher(enable_fuzzy=False)
        self.knowledge_judge = KnowledgeJudge()
        self.constraint_judge = ConstraintJudge(runtime)

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
        context_events, suppressed_nodes = self._resolve_context(units)
        node_results = [self._evaluate_node(node, units, suppressed_nodes) for node in self.graph.nodes]
        knowledge_checks = self.knowledge_judge.judge(self.graph.knowledge, units)
        constraint_checks = self.constraint_judge.judge(self.graph.constraints, units)
        knowledge_events = [x for x in knowledge_checks if x.verdict == "冲突"]
        constraint_events = [x for x in constraint_checks if x.verdict == "违规"]
        scores, caps, relation_events = self._score(node_results, knowledge_events, constraint_events, context_events)
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

    def _evaluate_node(self, node: GraphNode, units: list[EvidenceUnit], suppressed_nodes: set[str]) -> NodeResult:
        if node.id in suppressed_nodes:
            return NodeResult(node.id, node.name, "不适用", 1.0, False, [], [], inactive_reason="条件转场后不再强制要求", aliases=list(node.aliases))
        active = self._node_active(node, units)
        if not active:
            return NodeResult(node.id, node.name, "不适用", 1.0, False, [], [], inactive_reason="未触发条件节点", aliases=list(node.aliases))

        req_results = [self._evaluate_requirement(req, units) for req in node.requirements]
        all_groups = [g for r in req_results for g in r.group_matches]
        required_reqs = [r for r in req_results if r.required]
        if not required_reqs:
            score = 1.0 if req_results else 0.0
        else:
            weight_sum = sum(self._requirement_weight(node, r.requirement_id) for r in required_reqs) or float(len(required_reqs))
            score = sum(r.score * self._requirement_weight(node, r.requirement_id) for r in required_reqs) / weight_sum
        if score == 0.0 and self._generic_condition_branch_handled(node, units):
            score = float(self.runtime.get("thresholds", {}).get("node_satisfied", 0.75))
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

    def _evaluate_requirement(self, req: Requirement, units: list[EvidenceUnit]) -> RequirementResult:
        groups = self.matcher.match_groups(req.evidence_groups, units)
        required_groups = [g for g in groups if g.required]
        if not required_groups:
            score = 1.0 if groups else 0.0
        else:
            weight_sum = sum(self._evidence_group_weight(req, g.group_id) for g in required_groups) or float(len(required_groups))
            score = sum(g.score * self._evidence_group_weight(req, g.group_id) for g in required_groups) / weight_sum
        threshold = float(self.runtime.get("thresholds", {}).get("requirement_satisfied", self.runtime.get("thresholds", {}).get("node_satisfied", 0.75)))
        return RequirementResult(req.id, req.text, req.required, score, score >= threshold, groups, list(req.aliases))

    def _requirement_weight(self, node: GraphNode, requirement_id: str) -> float:
        for req in node.requirements:
            if req.id == requirement_id:
                return max(0.0, req.weight)
        return 1.0

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


    def _first_condition_trigger_turn(self, node: GraphNode, units: list[EvidenceUnit]) -> int | None:
        if node.activation.mode not in {"user_triggered", "condition"}:
            return None
        for pat in node.activation.patterns:
            for u in units:
                if u.speaker == "user" and self.strict_matcher._match_pattern(pat, u, units):
                    if not self._condition_trigger_is_only_neutral_context(node, pat, u.text):
                        return u.turn_index
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

    def _node_active(self, node: GraphNode, units: list[EvidenceUnit]) -> bool:
        mode = node.activation.mode
        if mode == "always":
            return True
        if mode == "optional":
            return False
        if mode in {"user_triggered", "condition"}:
            if not node.activation.patterns:
                return False
            user_units = [u for u in units if u.speaker == "user"]
            # Activation is a gate, not completion evidence.  Use strict
            # schema matching so a branch is not triggered by a loosely similar
            # user sentence from a different branch.
            for pat in node.activation.patterns:
                for u in user_units:
                    if not self.strict_matcher._match_pattern(pat, u, units):
                        continue
                    if self._condition_trigger_is_only_neutral_context(node, pat, u.text):
                        continue
                    return True
            return False
        return True

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

        LongCat sometimes emits a terminal policy without an explicit
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

    def _score(self, node_results: list[NodeResult], knowledge_events: list[KnowledgeEvent], constraint_events: list[ConstraintEvent], context_events: list[ContextEvent]) -> tuple[dict[str, float], list[dict[str, Any]], list[RelationEvent]]:
        active_required = [n for n in node_results if n.active]
        node_completion = 100.0 * (sum(n.score for n in active_required) / max(1, len(active_required)))
        relation_score, relation_events = self._relation_score(node_results)
        relation_score = max(0.0, relation_score - self._context_penalty(context_events))
        knowledge_score = max(0.0, 100.0 - sum(self._severity_penalty(e.severity, "knowledge") for e in knowledge_events))
        constraint_score = max(0.0, 100.0 - sum(self._severity_penalty(e.severity, "constraint") for e in constraint_events))
        weights = self.runtime.get("weights", {})
        total = (
            node_completion * float(weights.get("node_completion", 0.5))
            + relation_score * float(weights.get("relation_score", 0.15))
            + knowledge_score * float(weights.get("knowledge_score", 0.2))
            + constraint_score * float(weights.get("constraint_score", 0.15))
        )
        total, caps = self._apply_caps(total, knowledge_events, constraint_events, node_results, context_events)
        return (
            {
                "total": round(total, 2),
                "node_completion": round(node_completion, 2),
                "relation_score": round(relation_score, 2),
                "knowledge_score": round(knowledge_score, 2),
                "constraint_score": round(constraint_score, 2),
            },
            caps,
            relation_events,
        )

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
            if edge.relation not in {"strict_order", "soft_order", "branch", "terminal"}:
                continue
            a = result_by_id.get(edge.source)
            b = result_by_id.get(edge.target)
            if not a or not b or not a.active or not b.active:
                continue
            pair_scores.append((a.score + b.score) / 2.0)
            if a.status == "缺失" and b.status != "缺失":
                p = (8.0 if edge.relation == "strict_order" else 4.0) * edge.weight
                penalty += p
                events.append(RelationEvent(edge.relation, edge.source, edge.target, "前置缺失", p, "后续节点有证据，但前置节点缺少核心证据"))
            if b.status == "缺失" and a.status != "缺失":
                p = (6.0 if edge.relation == "strict_order" else 3.0) * edge.weight
                penalty += p
                events.append(RelationEvent(edge.relation, edge.source, edge.target, "后续缺失", p, "前置节点已触达，但后续节点缺少核心证据"))
            if a.first_hit_turn is not None and b.first_hit_turn is not None and a.first_hit_turn > b.first_hit_turn:
                p = (10.0 if edge.relation == "strict_order" else 4.0) * edge.weight
                penalty += p
                events.append(RelationEvent(edge.relation, edge.source, edge.target, "顺序异常", p, "后置节点早于前置节点出现", f"{a.first_hit_turn}>{b.first_hit_turn}"))
        group_score, group_events = self._relation_group_score(result_by_id)
        events.extend(group_events)
        edge_base = 100.0 if not pair_scores else 100.0 * sum(pair_scores) / len(pair_scores)
        if self.graph.relation_groups:
            base = edge_base * 0.55 + group_score * 0.45
        else:
            base = edge_base
        score = max(0.0, base - penalty)
        if not events and (self.graph.edges or self.graph.relation_groups):
            events.append(RelationEvent("summary", "", "", "结构正常", 0.0, "已评估状态图节点之间的顺序、承接、分支和关系组完整性"))
        return score, events

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
        if group.group_type == "any_of":
            ok = bool(completed)
            score = max((n.score for n in nodes), default=0.0) * 100.0
            if not ok and group.required:
                return score, RelationEvent("group:any_of", group.id, ",".join(group.nodes), "替代分支未命中", 8.0, group.description or "关系组要求至少一个分支完成")
            return score, None
        if group.group_type in {"ordered", "strict_order"}:
            penalty = 0.0
            first_hits = [n.first_hit_turn for n in nodes]
            for i in range(1, len(first_hits)):
                if first_hits[i - 1] is not None and first_hits[i] is not None and first_hits[i - 1] > first_hits[i]:
                    penalty += 8.0
            score = max(0.0, scored_mean - penalty)
            if penalty:
                return score, RelationEvent("group:ordered", group.id, ",".join(group.nodes), "组内顺序异常", penalty, group.description or "关系组内节点出现顺序不符合图结构")
            return score, None
        required_count = group.min_completed if group.min_completed is not None else len(nodes)
        ok = len(completed) >= min(required_count, len(nodes))
        if not ok and group.required:
            return scored_mean, RelationEvent("group:all_of", group.id, ",".join(group.nodes), "关系组未完整", 6.0, group.description or "关系组内必需节点没有充分完成")
        return scored_mean, None

    def _severity_penalty(self, severity: str, kind: str) -> float:
        base = {"low": 5.0, "medium": 12.0, "high": 28.0, "critical": 45.0}.get(severity, 12.0)
        return base if kind == "constraint" else base * 0.8

    def _apply_caps(self, total: float, knowledge_events: list[KnowledgeEvent], constraint_events: list[ConstraintEvent], node_results: list[NodeResult], context_events: list[ContextEvent]) -> tuple[float, list[dict[str, Any]]]:
        """Apply strict task-success caps.

        The evaluator follows a task-oriented dialogue principle: missing a
        required subtask is not just a small additive loss.  It should cap the
        whole dialogue score because task success is normally treated as a
        gating signal, while dialogue quality is secondary.  The cap is still
        schema-driven: it only looks at active nodes, required groups, event
        severities and context policies; it never uses domain-specific words.
        """
        caps: list[dict[str, Any]] = []
        active_nodes = [n for n in node_results if n.active]
        missing_nodes = [n for n in active_nodes if n.status == "缺失"]
        partial_nodes = [n for n in active_nodes if n.status == "部分完成"]
        required_groups = 0
        missing_groups = 0
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
        missing_ratio = missing_groups / max(1, required_groups)

        knowledge = bool(knowledge_events)
        high_knowledge = any(e.severity in {"high", "critical"} for e in knowledge_events)
        high_constraint = any(e.severity in {"high", "critical"} for e in constraint_events)
        critical_constraint = any(e.severity == "critical" for e in constraint_events)
        missing = bool(missing_nodes)
        context_problem = any(e.status != "已处理" for e in context_events)

        cap_value: float | None = None
        reason = ""
        if high_constraint and knowledge and missing:
            cap_value, reason = 42.0, "同时存在流程缺失、事实冲突和高风险限制问题"
        elif critical_constraint and (knowledge or missing or context_problem):
            cap_value, reason = 45.0, "严重限制违规与其他问题叠加"
        elif high_constraint and (knowledge or missing or context_problem):
            cap_value, reason = 50.0, "高风险限制问题与其他问题叠加"
        elif critical_constraint:
            cap_value, reason = 50.0, "存在严重限制违规"
        elif high_constraint:
            cap_value, reason = 58.0, "存在高风险限制违规"
        elif context_problem and (knowledge or missing):
            cap_value, reason = 58.0, "条件转场处理问题与其他问题叠加"
        elif context_problem:
            cap_value, reason = 68.0, "存在条件转场处理问题"
        elif high_knowledge and missing:
            cap_value, reason = 58.0, "高风险事实冲突与流程缺失叠加"
        elif knowledge and missing:
            cap_value, reason = 64.0, "同时存在流程缺失和事实冲突"
        elif high_knowledge:
            cap_value, reason = 66.0, "存在高风险事实冲突"
        elif knowledge:
            cap_value, reason = 74.0, "存在事实冲突"
        elif missing:
            if len(missing_nodes) >= 2 or missing_ratio >= 0.25:
                cap_value, reason = 66.0, "多个核心履约证据缺失"
            elif missing_ratio >= 0.12 or partial_nodes:
                cap_value, reason = 72.0, "核心履约证据缺失且存在部分完成节点"
            else:
                cap_value, reason = 78.0, "存在核心履约证据缺失"
        elif partial_nodes and missing_ratio > 0:
            cap_value, reason = 88.0, "存在部分履约证据不足"

        if cap_value is not None and total > cap_value:
            total = cap_value
            caps.append({"cap": cap_value, "reason": reason, "missing_required_groups": missing_groups, "required_groups": required_groups})
        return total, caps
