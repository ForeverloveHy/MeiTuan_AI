from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dataset_interface import AcceptanceResult
from .graph_evaluator import EvaluationResult, NodeResult


@dataclass(slots=True)
class OracleCandidate:
    candidate_id: str
    kind: str
    node_id: str | None
    question: str
    evidence: list[str]
    need: float
    strength: float
    requirement_id: str | None = None
    knowledge_id: str | None = None
    constraint_id: str | None = None
    context_id: str | None = None
    source: str = "local"
    error_family: str | None = None
    evaluability: str | None = None
    expected_detector: str | None = None
    requires_arbitration: bool = False
    positive_verdict: str | None = None
    negative_verdict: str | None = None
    trigger_verdict: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "node_id": self.node_id,
            "requirement_id": self.requirement_id,
            "knowledge_id": self.knowledge_id,
            "constraint_id": self.constraint_id,
            "context_id": self.context_id,
            "question": self.question,
            "evidence": self.evidence,
            "need": round(self.need, 4),
            "strength": round(self.strength, 4),
            "source": self.source,
            "error_family": self.error_family,
            "evaluability": self.evaluability,
            "expected_detector": self.expected_detector,
        }
        if self.requires_arbitration:
            data["requires_arbitration"] = True
        if self.positive_verdict is not None:
            data["positive_verdict"] = self.positive_verdict
        if self.negative_verdict is not None:
            data["negative_verdict"] = self.negative_verdict
        if self.trigger_verdict is not None:
            data["trigger_verdict"] = self.trigger_verdict
        return data


def _norm_family(value: Any, kind: str | None = None) -> str:
    v = str(value or "").strip().lower()
    mapping = {
        "flow": "flow_missing",
        "missing": "flow_missing",
        "process_missing": "flow_missing",
        "flow_missing": "flow_missing",
        "流程缺失": "flow_missing",
        "knowledge": "knowledge_violation",
        "knowledge_error": "knowledge_violation",
        "knowledge_violation": "knowledge_violation",
        "faq_wrong": "knowledge_violation",
        "fact_wrong": "knowledge_violation",
        "知识错误": "knowledge_violation",
        "semantic_or_context": "semantic_or_context",
        "context": "context_violation",
        "context_violation": "context_violation",
        "constraint": "constraint_violation",
        "boundary": "constraint_violation",
        "boundary_violation": "constraint_violation",
        "constraint_violation": "constraint_violation",
        "限制违规": "constraint_violation",
    }
    if v in mapping:
        return mapping[v]
    k = str(kind or "")
    if "知识" in k:
        return "knowledge_violation"
    if "限制" in k or "边界" in k:
        return "constraint_violation"
    if "requirement" in k or "样本验收" in k:
        return "flow_missing"
    if "上下文" in k:
        return "context_violation"
    return v or "unknown"


def _id_values(*values: Any) -> set[str]:
    out: set[str] = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            out.update(str(x) for x in v if x is not None)
        else:
            out.add(str(v))
    return {x for x in out if x}


def _matches_id(expected: Any, actual: Any, aliases: list[str] | None = None) -> bool:
    if not expected:
        return False
    return str(expected) in _id_values(actual, aliases or [])


def _find_node(result: EvaluationResult, node_id: Any) -> NodeResult | None:
    if not node_id:
        return None
    for node in result.node_results:
        if _matches_id(node_id, node.node_id, getattr(node, "aliases", [])):
            return node
    return None


def _assistant_summary(result: EvaluationResult, limit: int = 1800) -> str:
    lines: list[str] = []
    seen: set[tuple[int, str]] = set()
    for unit in result.evidence_units:
        if unit.speaker != "assistant":
            continue
        key = (unit.turn_index, unit.text)
        if key in seen:
            continue
        seen.add(key)
        text = str(unit.text or "").strip()
        if text:
            lines.append(f"客服第{unit.turn_index}轮：{text}")
    joined = "\n".join(lines)
    return joined[:limit] + ("\n……[已截断]" if len(joined) > limit else "")




def _keep_priority_evidence(items: list[str], limit: int = 6) -> list[str]:
    """Keep compact arbitration evidence without dropping the transcript.

    Local ledger rows can be numerous.  The assistant transcript is generic
    dialogue evidence and is often the only place where an open semantic issue
    can be inspected, so it must survive clipping.
    """
    clean = [str(x or "").strip() for x in items if str(x or "").strip()]
    if not clean:
        return []
    selected = clean[:limit]
    transcript = next((x for x in clean if "客服实际表达摘要" in x), None)
    if transcript and all("客服实际表达摘要" not in x for x in selected):
        selected = selected[: max(0, limit - 1)] + [transcript]
    # Remove accidental duplicates while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:limit]


def _target_schema_summary(result: EvaluationResult, err: dict[str, Any], family: str) -> list[str]:
    """Summarize the bound schema target without using answer-key spans.

    For acceptance arbitration, LLM must know what local schema target the
    sample expects, but it must not receive injected wrong_statement or
    evidence_span as the answer.  This summary is built only from evaluator
    ledgers generated from the graph: target id, name, reason, and current
    verdict.
    """
    out: list[str] = []
    if family == "knowledge_violation":
        kid = err.get("knowledge_id") or err.get("target_knowledge_id")
        for check in result.knowledge_checks:
            if kid and not _matches_id(kid, getattr(check, "knowledge_id", None), getattr(check, "aliases", [])):
                continue
            out.append(
                "目标知识标准："
                f"knowledge_id={getattr(check, 'knowledge_id', '')}；"
                f"名称={getattr(check, 'name', '')}；"
                f"claim_id={getattr(check, 'claim_id', '')}；"
                f"本地verdict={getattr(check, 'verdict', '')}；"
                f"标准说明={getattr(check, 'reason', '')}。"
            )
            break
    elif family == "constraint_violation":
        cid = err.get("constraint_id") or err.get("target_constraint_id")
        for check in result.constraint_checks:
            if cid and not _matches_id(cid, getattr(check, "constraint_id", None), getattr(check, "aliases", [])):
                continue
            out.append(
                "目标限制标准："
                f"constraint_id={getattr(check, 'constraint_id', '')}；"
                f"名称={getattr(check, 'name', '')}；"
                f"本地verdict={getattr(check, 'verdict', '')}；"
                f"说明={getattr(check, 'reason', '')}。"
            )
            break
    return [x for x in out if x]


def _node_evidence(result: EvaluationResult, node_id: Any, requirement_id: Any = None) -> list[str]:
    node = _find_node(result, node_id)
    out: list[str] = []
    if node:
        out.append(f"目标节点：{node.name}；状态={node.status}；分数={round(node.score, 4)}；active={node.active}。")
        if requirement_id:
            for req in node.requirement_results:
                keys = _id_values(req.requirement_id, getattr(req, "aliases", []))
                if str(requirement_id) not in keys:
                    continue
                out.append(f"目标 requirement：{req.text or req.requirement_id}；matched={req.matched}；score={round(req.score, 4)}。")
                hits = [h.text for g in req.group_matches for h in g.hits]
                if hits:
                    out.append("本地已命中的相邻证据：" + " / ".join(hits[:4]))
                break
    else:
        out.append("未能在本次状态图评估结果中定位到目标节点。")
    full = _assistant_summary(result)
    if full:
        out.append("客服实际表达摘要：\n" + full)
    return _keep_priority_evidence(out, limit=4)


def _acceptance_evidence(result: EvaluationResult, err: dict[str, Any], node_id: Any, req_id: Any, family: str) -> list[str]:
    # Do not pass injected evidence_span / wrong_statement to LLM as an
    # answer key.  Arbitration should see evaluator ledgers plus actual
    # assistant utterances from the dialogue.  Earlier versions only passed the
    # local ledger row when it existed; for schema-gap negatives this often meant
    # LLM saw a vague "verdict=证据不足" line but not the real assistant
    # context.  The transcript summary below is generic dialogue evidence, not
    # sample-answer leakage, and lets LLM decide the Chinese semantic issue.
    out: list[str] = []
    if family == "flow_missing":
        out.extend(_node_evidence(result, node_id, req_id))
    elif family == "knowledge_violation":
        out.extend(_target_schema_summary(result, err, family))
        events = []
        kid = err.get("knowledge_id") or err.get("target_knowledge_id")
        for check in result.knowledge_checks:
            if kid and not _matches_id(kid, getattr(check, "knowledge_id", None), getattr(check, "aliases", [])):
                continue
            ev = str(getattr(check, 'evidence', '') or '').strip()
            verdict = str(getattr(check, 'verdict', '') or '')
            # Do not let a local support row dominate a negative acceptance
            # arbitration.  The transcript below contains the real assistant
            # utterances; this row is just schema context.
            events.append(f"本地目标知识核验行：verdict={verdict}；evidence={ev}")
        if events:
            out.extend(events[:2])
        out.append("仲裁提示：本地核验行可能只是目标知识的一条支持句或证据不足句；请在客服实际表达摘要中查找是否存在与目标知识标准相反的客服表达。")
    elif family == "constraint_violation":
        out.extend(_target_schema_summary(result, err, family))
        events = []
        cid = err.get("constraint_id") or err.get("target_constraint_id")
        for check in result.constraint_checks:
            if cid and not _matches_id(cid, getattr(check, "constraint_id", None), getattr(check, "aliases", [])):
                continue
            events.append(f"本地目标限制核验行：verdict={getattr(check, 'verdict', '')}；evidence={getattr(check, 'evidence', '')}")
        if events:
            out.extend(events[:2])
        out.append("仲裁提示：请依据目标限制标准和客服实际表达摘要判断是否出现违规，不要只看本地核验行。")
    full = _assistant_summary(result)
    if full:
        out.append("客服实际表达摘要：\n" + full)
    # Keep the transcript summary in acceptance arbitration.  A schema-gap
    # negative may have only support/insufficient local ledger rows; if the
    # transcript is truncated away, the LLM cannot inspect the actual assistant
    # utterances.  This is generic dialogue evidence, not answer-key leakage.
    return _keep_priority_evidence(out, limit=6)


class OracleRouter:
    """Build the LLM arbitration queue.

    The current method contract keeps node/flow fulfillment fully local: nodes
    are scored by the deterministic state-graph evaluator and are not sent to
    LLM for semantic completion.  LLM only reviews knowledge facts and
    hard-constraint boundary grey zones.  Requirement/context routing is kept as
    an explicit opt-in diagnostic path, disabled by default.
    """

    def __init__(self, runtime: dict[str, Any]) -> None:
        self.config = runtime.get("oracle_budget", {})

    def build_candidates(self, result: EvaluationResult, acceptance: AcceptanceResult | None = None) -> list[OracleCandidate]:
        if not self.config.get("enabled", True):
            return []
        out: list[OracleCandidate] = []
        if bool(self.config.get("route_requirement_candidates", False)):
            out.extend(self._requirement_candidates(result))
        out.extend(self._knowledge_candidates(result))
        out.extend(self._constraint_candidates(result))
        if bool(self.config.get("route_context_candidates", False)):
            out.extend(self._context_candidates(result))
        # Dataset labels (negative ids, injected_errors, wrong_statement, evidence_span)
        # are answer-key metadata. They are report-only and must never become
        # LLM arbitration candidates. Oracle routing is based only on
        # evaluator-produced local review/ambiguous events.
        _ = acceptance
        max_items = int(self.config.get("max_dialogue_candidates", 2))
        by_key: dict[tuple[Any, ...], OracleCandidate] = {}
        for cand in sorted(out, key=lambda x: (x.source == "acceptance", x.need, x.strength), reverse=True):
            key = (cand.kind, cand.node_id, cand.requirement_id, cand.knowledge_id, cand.constraint_id, cand.context_id, cand.source)
            by_key.setdefault(key, cand)
        return list(by_key.values())[:max_items]

    def _requirement_candidates(self, result: EvaluationResult) -> list[OracleCandidate]:
        min_need = float(self.config.get("min_need", 0.70))
        min_strength = float(self.config.get("min_evidence_strength", 0.35))
        out: list[OracleCandidate] = []
        for node in result.node_results:
            if not node.active:
                continue
            for req in node.requirement_results:
                if not req.required or req.matched:
                    continue
                strength = max((g.score for g in req.group_matches), default=0.0)
                need = 1.0 - req.score
                if need < min_need or strength < min_strength:
                    continue
                evidence = [h.text for g in req.group_matches for h in g.hits][:3]
                out.append(
                    OracleCandidate(
                        candidate_id=f"{result.dialogue_id}:req:{node.node_id}:{req.requirement_id}",
                        kind="requirement 覆盖灰区",
                        node_id=node.node_id,
                        requirement_id=req.requirement_id,
                        question=f"这段对话是否已经完成该 requirement：{req.text or req.requirement_id}？",
                        evidence=evidence,
                        need=need,
                        strength=strength,
                        error_family="flow_missing",
                        evaluability="semantic",
                        expected_detector="semantic_node_coverage",
                    )
                )
        return out

    def _knowledge_candidates(self, result: EvaluationResult) -> list[OracleCandidate]:
        out: list[OracleCandidate] = []
        for check in result.knowledge_checks:
            if check.verdict != "证据不足" and not (getattr(check, "requires_arbitration", False) or getattr(check, "positive_verdict", "") == "review" or getattr(check, "negative_verdict", "") == "review"):
                continue
            out.append(
                OracleCandidate(
                    candidate_id=f"{result.dialogue_id}:knowledge:{check.knowledge_id}:{check.claim_id or ''}",
                    kind="知识核验灰区",
                    node_id=check.node_id,
                    knowledge_id=check.knowledge_id,
                    question=f"对话中关于“{check.name}”的事实声明应判为支持、冲突，还是证据不足？",
                    evidence=[x for x in [check.evidence, f"本地原因：{getattr(check, 'reason', '')}；positive_verdict={getattr(check, 'positive_verdict', '')}；negative_verdict={getattr(check, 'negative_verdict', '')}；requires_arbitration={getattr(check, 'requires_arbitration', False)}"] if x],
                    need=0.82,
                    strength=0.60 if check.evidence else 0.35,
                    error_family="knowledge_violation",
                    evaluability="semantic",
                    expected_detector="knowledge_nli",
                    requires_arbitration=bool(getattr(check, "requires_arbitration", False)),
                    positive_verdict=getattr(check, "positive_verdict", None),
                    negative_verdict=getattr(check, "negative_verdict", None),
                )
            )
        return out

    def _constraint_candidates(self, result: EvaluationResult) -> list[OracleCandidate]:
        out: list[OracleCandidate] = []
        for check in result.constraint_checks:
            if check.verdict != "证据不足" and not (getattr(check, "requires_arbitration", False) or getattr(check, "negative_verdict", "") == "review" or getattr(check, "positive_verdict", "") == "review"):
                continue
            out.append(
                OracleCandidate(
                    candidate_id=f"{result.dialogue_id}:constraint:{check.constraint_id}",
                    kind="限制边界灰区",
                    node_id=check.node_id,
                    constraint_id=check.constraint_id,
                    question=f"对话中关于“{check.name}”的表达应判为安全、违规，还是证据不足？",
                    evidence=[x for x in [check.evidence, f"本地原因：{getattr(check, 'reason', '')}；trigger_verdict={getattr(check, 'trigger_verdict', '')}；positive_verdict={getattr(check, 'positive_verdict', '')}；negative_verdict={getattr(check, 'negative_verdict', '')}；requires_arbitration={getattr(check, 'requires_arbitration', False)}"] if x],
                    need=0.86,
                    strength=0.60 if check.evidence else 0.35,
                    error_family="constraint_violation",
                    evaluability="semantic",
                    expected_detector="constraint_nli",
                    requires_arbitration=bool(getattr(check, "requires_arbitration", False)),
                    positive_verdict=getattr(check, "positive_verdict", None),
                    negative_verdict=getattr(check, "negative_verdict", None),
                    trigger_verdict=getattr(check, "trigger_verdict", None),
                )
            )
        return out

    def _context_candidates(self, result: EvaluationResult) -> list[OracleCandidate]:
        out: list[OracleCandidate] = []
        by_turn = {u.turn_index: u for u in result.evidence_units}
        for event in result.context_events:
            if event.status == "已处理":
                continue
            evidence: list[str] = [f"上下文本地核验：状态={event.status}；说明={event.reason}"]
            trigger = by_turn.get(event.trigger_turn) if event.trigger_turn is not None else None
            if trigger:
                evidence.append(f"用户触发句（第{trigger.turn_index}轮）：{trigger.text}")
            if event.handling_turn is not None and event.handling_turn in by_turn:
                handling = by_turn[event.handling_turn]
                evidence.append(f"客服处理句（第{handling.turn_index}轮）：{handling.text}")
            else:
                later = [u for u in result.evidence_units if u.speaker == "assistant" and event.trigger_turn is not None and u.turn_index > event.trigger_turn]
                if later:
                    evidence.append("触发后的客服表达：" + " / ".join(f"第{u.turn_index}轮：{u.text}" for u in later[:3]))
                elif event.trigger_turn is not None:
                    nearby = [u for u in result.evidence_units if u.speaker == "assistant" and abs(u.turn_index - event.trigger_turn) <= 4]
                    if nearby:
                        evidence.append("触发附近客服表达：" + " / ".join(f"第{u.turn_index}轮：{u.text}" for u in nearby[:4]))
            out.append(
                OracleCandidate(
                    candidate_id=f"{result.dialogue_id}:context:{event.policy_id}",
                    kind="上下文转场灰区",
                    node_id=None,
                    context_id=event.policy_id,
                    question=f"对话是否正确处理了上下文转场策略：{event.policy_id}？",
                    evidence=[x for x in evidence if x][:4],
                    need=0.78,
                    strength=0.60 if len(evidence) > 1 else 0.35,
                    error_family="context_violation",
                    evaluability="semantic",
                    expected_detector="context_policy",
                )
            )
        return out

    def _acceptance_candidates(self, result: EvaluationResult, acceptance: AcceptanceResult) -> list[OracleCandidate]:
        out: list[OracleCandidate] = []
        for idx, err in enumerate(acceptance.oracle_expected or []):
            if not isinstance(err, dict):
                continue
            node_id = err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node")
            req_id = err.get("requirement_id") or err.get("target_core") or err.get("target_group_id")
            knowledge_id = err.get("knowledge_id") or err.get("target_knowledge_id")
            constraint_id = err.get("constraint_id") or err.get("target_constraint_id")
            fam = _norm_family(err.get("error_family") or err.get("type"))
            detector = str(err.get("expected_detector") or "").lower()
            if fam == "semantic_or_context" and detector == "semantic_node_coverage":
                evidence_family = "flow_missing"
            elif fam == "semantic_or_context" and (node_id or req_id):
                evidence_family = "flow_missing"
            else:
                evidence_family = fam
            evidence = _acceptance_evidence(result, err, node_id, req_id, evidence_family)
            out.append(
                OracleCandidate(
                    candidate_id=f"{result.dialogue_id}:acceptance:{idx}",
                    kind="样本验收灰区",
                    node_id=str(node_id) if node_id else None,
                    requirement_id=str(req_id) if req_id else None,
                    knowledge_id=str(knowledge_id) if knowledge_id else None,
                    constraint_id=str(constraint_id) if constraint_id else None,
                    question="该样本预设问题是否已被对话证据体现，并应由评估器判为问题？",
                    evidence=evidence,
                    need=0.90,
                    strength=0.60 if evidence else 0.30,
                    source="acceptance",
                    error_family=evidence_family if evidence_family != fam else fam,
                    evaluability=str(err.get("evaluability") or ""),
                    expected_detector=str(err.get("expected_detector") or ""),
                )
            )
        return out
