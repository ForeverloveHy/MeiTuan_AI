from __future__ import annotations

from collections import Counter
import re
from typing import Any


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clip(x: Any, limit: int = 500) -> str:
    s = str(x or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 20)] + "……[已截断]"


def _norm_family(value: Any, kind: Any = None) -> str:
    v = str(value or "").strip().lower()
    mapping = {
        "flow": "flow_missing",
        "missing": "flow_missing",
        "process_missing": "flow_missing",
        "flow_missing": "flow_missing",
        "knowledge": "knowledge_violation",
        "knowledge_error": "knowledge_violation",
        "knowledge_violation": "knowledge_violation",
        "faq_wrong": "knowledge_violation",
        "fact_wrong": "knowledge_violation",
        "constraint": "constraint_violation",
        "boundary": "constraint_violation",
        "boundary_violation": "constraint_violation",
        "constraint_violation": "constraint_violation",
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



def _norm_text(value: Any) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\s+", "", s)
    return s


def _candidate_evidence_texts(cand: dict[str, Any]) -> list[str]:
    return [_norm_text(x) for x in (cand.get("evidence") or []) if _norm_text(x)]


def _ledger_rows(rec: dict[str, Any], fam: str) -> list[dict[str, Any]]:
    ev = rec.get("evaluation") or {}
    if fam == "knowledge_violation":
        return [x for x in (ev.get("knowledge_checks") or []) if isinstance(x, dict)]
    if fam == "constraint_violation":
        return [x for x in (ev.get("constraint_checks") or []) if isinstance(x, dict)]
    return []


def _same_evidence_ledger_relation(rec: dict[str, Any], cand: dict[str, Any], fam: str) -> str:
    """Classify a semantic candidate by same-evidence ledger signals.

    This is intentionally schema/evidence driven.  It does not know any domain
    words.  If a grey candidate only repeats evidence that the local ledger has
    already treated as support/insufficient and there is no same-evidence
    conflict/violation, sending it to LLM is usually audit noise.  If the
    same evidence has already created a conflict/violation somewhere in the
    structured ledger, it is a valuable semantic arbitration point.
    """
    rows = _ledger_rows(rec, fam)
    if not rows:
        return "unknown"
    evs = set(_candidate_evidence_texts(cand))
    if not evs:
        return "unknown"
    same: list[dict[str, Any]] = []
    for row in rows:
        txt = _norm_text(row.get("evidence"))
        if txt and txt in evs:
            same.append(row)
    if not same:
        return "unknown"
    verdicts = {str(x.get("verdict") or "").strip() for x in same}
    if "冲突" in verdicts or "违规" in verdicts:
        return "same_evidence_issue"
    if "支持" in verdicts or "安全" in verdicts or "已处理" in verdicts:
        return "same_evidence_support"
    if verdicts and verdicts <= {"证据不足", "未提及", ""}:
        return "same_evidence_insufficient_only"
    return "same_evidence_neutral"


def _route_key(rec: dict[str, Any], cand: dict[str, Any]) -> tuple[Any, ...]:
    """A conservative merge key for duplicate arbitration candidates."""
    cid = str(cand.get("candidate_id") or "")
    claim = cid.rsplit(":", 1)[-1] if ":" in cid else ""
    evidence_sig = "|".join(_candidate_evidence_texts(cand))[:240]
    return (
        cand.get("source") or "",
        cand.get("error_family") or "",
        cand.get("kind") or "",
        cand.get("node_id") or "",
        cand.get("requirement_id") or "",
        cand.get("knowledge_id") or "",
        cand.get("constraint_id") or "",
        cand.get("context_id") or "",
        claim,
        evidence_sig,
    )


def _has_binding(cand: dict[str, Any]) -> bool:
    return any(cand.get(k) for k in ["node_id", "requirement_id", "knowledge_id", "constraint_id", "context_id"])


def _schema_anchor_score(cand: dict[str, Any]) -> float:
    score = 0.0
    if cand.get("node_id"):
        score = max(score, 0.62)
    if cand.get("requirement_id"):
        score = max(score, 0.74)
    if cand.get("knowledge_id") or cand.get("constraint_id") or cand.get("context_id"):
        score = max(score, 0.82)
    if cand.get("source") == "acceptance" and _has_binding(cand):
        score = max(score, 0.78)
    if not _has_binding(cand) and cand.get("source") == "acceptance":
        score = max(score, 0.35)
    return min(1.0, score)


def _evidence_anchor_score(cand: dict[str, Any]) -> float:
    evidence = [str(x or "").strip() for x in (cand.get("evidence") or []) if str(x or "").strip()]
    if not evidence:
        return 0.0
    joined = "\n".join(evidence)
    base = min(0.86, 0.25 + len(joined) / 1800.0)
    strength = _as_float(cand.get("strength"), 0.0)
    return min(1.0, max(base, strength))




def _has_substantive_evidence(cand: dict[str, Any]) -> bool:
    evidence = [str(x or "").strip() for x in (cand.get("evidence") or []) if str(x or "").strip()]
    if not evidence:
        return False
    joined = "\n".join(evidence)
    strong_markers = ("样本预设证据", "verdict=", "状态=", "matched=", "本地已命中", "目标节点", "目标 requirement")
    if any(m in joined for m in strong_markers):
        # A pure assistant transcript is context, not a focused arbitration anchor.
        if joined.strip().startswith("客服实际表达摘要") and not any(m in joined for m in strong_markers[:-1]):
            return False
        return True
    return False


def _has_traceable_evidence(cand: dict[str, Any], fam: str, schema_score: float, evidence_score: float) -> bool:
    """Whether a candidate has enough concrete material for LLM.

    The previous gate treated only ledger strings such as ``verdict=`` or
    ``目标节点`` as substantive.  That accidentally suppressed genuine local
    grey-zone NLI candidates, because a knowledge/constraint checker often
    contributes exactly one focused assistant utterance as evidence.  Here the
    evidence is still required to be schema-bound and non-empty, but it no
    longer has to contain report-specific marker words.
    """
    evidence = [str(x or "").strip() for x in (cand.get("evidence") or []) if str(x or "").strip()]
    if not evidence:
        return False
    if _has_substantive_evidence(cand):
        return True
    if fam == "flow_missing":
        return _has_binding(cand) and evidence_score >= 0.25
    if fam in {"knowledge_violation", "constraint_violation", "context_violation", "semantic_or_context"}:
        return _has_binding(cand) and schema_score >= 0.55 and evidence_score >= 0.35
    return _has_binding(cand) and schema_score >= 0.55 and evidence_score >= 0.45

def _decision_margin(cand: dict[str, Any]) -> float:
    need = _as_float(cand.get("need"), 0.0)
    strength = _as_float(cand.get("strength"), 0.0)
    return min(1.0, max(0.0, 0.55 * need + 0.45 * strength))



def _element_review_force(cand: dict[str, Any]) -> tuple[bool, str]:
    """Route only structured high-value element review candidates.

    Zero-level elements have been removed.  Do not infer arbitration from free
    text such as “送审” in a reason string; rely only on structured fields.
    """
    flags = [
        cand.get("requires_arbitration") or "requires_arbitration=True" in "\n".join(str(x or "") for x in cand.get("evidence") or []),
        cand.get("negative_verdict") == "review",
        cand.get("positive_verdict") == "hit" and cand.get("negative_verdict") == "hit",
    ]
    if any(flags):
        return True, "元素层结构化 review：负向对象不稳或正负两侧同时命中。"
    return False, ""





def _routing_priority(cand: dict[str, Any]) -> float:
    """Rank routed candidates by local ambiguity value before budget cutting.

    This stays label-free.  It prefers cases where the element engine sees a
    real positive/negative collision over cases where only one side is a weak
    review.  The aim is not to increase calls blindly, but to spend the small
    budget on locally hard semantic boundary points.
    """
    p = 0.0
    pos = str(cand.get("positive_verdict") or "").lower()
    neg = str(cand.get("negative_verdict") or "").lower()
    if pos == "hit" and neg == "hit":
        p += 0.22
    elif neg == "review" and pos in {"hit", "review"}:
        p += 0.12
    elif neg == "review":
        p += 0.06
    if pos == "review" and neg in {"miss", ""}:
        p -= 0.10
    if str(cand.get("source") or "") == "local":
        p += 0.02
    return p


def _budget_primary_key(cand: dict[str, Any]) -> tuple[str, str]:
    anchor = cand.get("knowledge_id") or cand.get("constraint_id") or cand.get("context_id") or cand.get("node_id") or cand.get("kind") or "unknown"
    return (str(cand.get("error_family") or "unknown"), str(anchor))


def _apply_budget_with_diversity(
    routed: list[tuple[dict[str, Any], dict[str, Any]]],
    max_items: int,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Cut the LLM budget while preserving schema-anchor diversity.

    The purpose of the local second filter is visible only when it both keeps
    enough candidates for arbitration and shows that many near-duplicates were
    merged or deferred locally.  We therefore avoid letting a single hard atom
    consume the whole budget: candidates are grouped by ``error_family + schema
    anchor`` and selected in a round-robin order after global priority sorting.
    This remains label-free and uses only schema/evidence anchors.
    """
    budget = max(0, int(max_items))
    if budget <= 0 or len(routed) <= budget:
        return routed[:budget], routed[budget:]

    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    order: list[tuple[str, str]] = []
    for pair in routed:
        key = _budget_primary_key(pair[1])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(pair)

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    chosen_ids: set[int] = set()
    cursor = 0
    while len(selected) < budget and any(grouped.get(k) for k in order):
        key = order[cursor % len(order)]
        cursor += 1
        bucket = grouped.get(key) or []
        if not bucket:
            continue
        pair = bucket.pop(0)
        selected.append(pair)
        chosen_ids.add(id(pair[1]))

    overflow = [pair for pair in routed if id(pair[1]) not in chosen_ids]
    return selected, overflow

def _high_value_llm_eligible(cand: dict[str, Any], fam: str, schema_score: float, evidence_score: float, margin: float, ledger_relation: str = "unknown") -> tuple[bool, str]:
    """Strict gate for expensive LLM arbitration.

    The second-level model should not review every local ``review`` row.  It is
    reserved for cases where all of these are true:
    1) the local evaluator explicitly marked the element/fact/boundary as
       requiring arbitration;
    2) the candidate is bound to a concrete graph object and concrete dialogue
       evidence;
    3) the local ledger does not already show same-evidence support/safety or
       a low-value sibling insufficient row;
    4) the local need/strength margin is high enough that an LLM decision can
       change a sample-level result, rather than merely decorate the report.
    """
    if fam not in {"knowledge_violation", "constraint_violation", "context_violation", "semantic_or_context"}:
        return False, "流程覆盖和普通节点履约保持本地判定，不送大模型。"
    if not bool(cand.get("requires_arbitration")):
        return False, "未被本地元素/事实/边界执行器标记为必须仲裁，仅作为本地灰区审计保留。"
    if cand.get("source") not in {"local", "acceptance"}:
        return False, "候选来源不属于可送审的本地局部候选。"
    if ledger_relation == "same_evidence_support":
        return False, "同一证据已有明确支持/安全结论，避免重复送审。"
    if schema_score < 0.80:
        return False, "schema 锚点不足够稳定，不送大模型。"
    if evidence_score < 0.58:
        return False, "证据锚点不足够稳定，不送大模型。"
    if margin < 0.70:
        return False, "候选优先级不足，保留为本地审计项。"
    if not _has_binding(cand):
        return False, "缺少节点/知识/限制/上下文绑定，不送大模型。"
    return True, "高价值局部灰区：schema、证据、优先级均满足预算内送审条件。"

def _local_promoted(cand: dict[str, Any], fam: str, schema_score: float, evidence_score: float) -> tuple[bool, str]:
    if cand.get("source") != "acceptance":
        return False, ""
    ev = str(cand.get("evaluability") or "").strip().lower()
    detector = str(cand.get("expected_detector") or "").strip().lower()
    if ev not in {"semantic", "open_set", "unsupported", ""} and detector not in {"knowledge_nli", "constraint_nli", "semantic_node_coverage", "audit_only"}:
        return True, "样本预设项属于本地严格验收范围，不应进入 LLM。"
    evidence_text = "\n".join(str(x or "") for x in cand.get("evidence") or [])
    if fam == "flow_missing" and schema_score >= 0.70:
        low_markers = ["状态=缺失", "matched=False", "active=False"]
        if any(x in evidence_text for x in low_markers):
            return True, "本地二次筛选确认：目标流程/requirement 仍未覆盖，可本地严格升级。"
    if fam == "knowledge_violation" and schema_score >= 0.78 and "verdict=冲突" in evidence_text:
        return True, "本地二次筛选确认：知识核验已经给出冲突事件。"
    if fam == "constraint_violation" and schema_score >= 0.78 and "verdict=违规" in evidence_text:
        return True, "本地二次筛选确认：限制核验已经给出违规事件。"
    return False, ""


def apply_local_second_filter(records: list[dict[str, Any]], max_items: int | None = None, force_route_acceptance: bool = False) -> dict[str, Any]:
    """Gate oracle candidates before sending them to LLM.

    This is the latest-kernel counterpart of the older demo's local second
    filter. It is schema/evidence driven: local strict items stay local;
    noisy or unsupported items are ignored; only semantic/open-set candidates
    with enough anchored evidence are deferred to LLM.
    """
    routed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    local_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ignored: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for rec in records:
        rec_decisions: list[dict[str, Any]] = []
        for raw in rec.get("oracle_candidates") or []:
            if not isinstance(raw, dict):
                continue
            cand = dict(raw)
            fam = _norm_family(cand.get("error_family"), cand.get("kind"))
            schema_score = _schema_anchor_score(cand)
            evidence_score = _evidence_anchor_score(cand)
            margin = _decision_margin(cand)
            promoted, promote_reason = _local_promoted(cand, fam, schema_score, evidence_score)

            ledger_relation = _same_evidence_ledger_relation(rec, cand, fam)
            force_review, force_review_reason = _element_review_force(cand)
            # The second filter is intentionally blind to sample_type / positive-vs-negative
            # labels.  It may look only at schema anchors, evaluator ledgers, local
            # review flags, and concrete evidence.

            if promoted:
                decision = "local_strict"
                reason = promote_reason
                route = False
                verdict = "confirmed_issue" if cand.get("source") == "acceptance" else "no_llm_needed"
            elif force_review:
                eligible, eligible_reason = _high_value_llm_eligible(cand, fam, schema_score, evidence_score, margin, ledger_relation)
                if eligible and _has_traceable_evidence(cand, fam, schema_score, evidence_score):
                    decision = "route_to_oracle_high_value"
                    reason = force_review_reason + "；" + eligible_reason
                    route = True
                    verdict = "needs_semantic_arbitration"
                else:
                    decision = "local_review_kept"
                    reason = eligible_reason or "元素层 review 未达到高价值送审门槛，保留本地审计。"
                    route = False
                    verdict = "needs_local_audit"
            elif force_route_acceptance and cand.get("source") == "acceptance":
                eligible, eligible_reason = _high_value_llm_eligible(cand, fam, schema_score, evidence_score, margin, ledger_relation)
                if eligible:
                    decision = "force_defer_high_value"
                    reason = "用户请求扩大仲裁，但仍只送高价值候选：" + eligible_reason
                    route = True
                    verdict = "defer_to_llm"
                else:
                    decision = "force_defer_blocked_by_gate"
                    reason = "用户请求扩大仲裁，但该候选未达高价值门槛：" + eligible_reason
                    route = False
                    verdict = "needs_local_audit"
            elif cand.get("source") == "local" and fam in {"knowledge_violation", "constraint_violation"} and ledger_relation == "same_evidence_support":
                decision = "merged_local_no_issue"
                reason = "同一证据在本地账本中已有支持/安全结论，属于低价值重复仲裁点，合并为本地无问题审计项。"
                route = False
                verdict = "no_llm_needed"
            elif (
                cand.get("source") == "local"
                and fam in {"knowledge_violation", "constraint_violation"}
                and ledger_relation == "same_evidence_insufficient_only"
                and not bool(cand.get("requires_arbitration"))
            ):
                decision = "merged_low_value"
                reason = "同一证据没有产生冲突/违规，且本地未标记必须仲裁；合并为低价值审计项。"
                route = False
                verdict = "ignored"
            elif schema_score < 0.30 and evidence_score < 0.25:
                decision = "ignore_noise"
                reason = "schema 与候选证据锚定都不足，不进入 LLM。"
                route = False
                verdict = "ignored"
            elif (not cand.get("evidence")) and fam != "flow_missing":
                decision = "ignore_noise"
                reason = "缺少可回查证据，不进入 LLM。"
                route = False
                verdict = "ignored"
            elif evidence_score < 0.35 and fam != "flow_missing":
                decision = "ignore_noise"
                reason = "证据锚点过低，不进入 LLM。"
                route = False
                verdict = "ignored"
            else:
                ev = str(cand.get("evaluability") or "semantic").strip().lower()
                has_focus = _has_traceable_evidence(cand, fam, schema_score, evidence_score)
                if not has_focus:
                    decision = "ignore_noise"
                    reason = "没有形成 schema 绑定 + 可回查证据的组合，不进入 LLM。"
                    route = False
                    verdict = "ignored"
                elif ev in {"open_set", "unsupported"}:
                    if evidence_score < 0.60:
                        decision = "ignore_noise"
                        reason = "开放集候选证据强度不足，先保留本地灰区，不进入 LLM。"
                        route = False
                        verdict = "ignored"
                    else:
                        decision = "open_set_kept_local"
                        reason = "开放集候选不再默认送大模型；仅保留本地灰区审计，避免二级判断过量介入。"
                        route = False
                        verdict = "needs_local_audit"
                elif schema_score < 0.55 or evidence_score < 0.45:
                    decision = "ignore_noise"
                    reason = "schema 或证据锚点不够，避免把低质量候选送入 LLM。"
                    route = False
                    verdict = "ignored"
                else:
                    eligible, eligible_reason = _high_value_llm_eligible(cand, fam, schema_score, evidence_score, margin, ledger_relation)
                    if eligible:
                        decision = "semantic_defer_high_value"
                        reason = eligible_reason
                        route = True
                        verdict = "defer_to_llm"
                    else:
                        decision = "semantic_review_kept_local"
                        reason = eligible_reason or "语义灰区未达到高价值送审门槛，保留为本地审计项。"
                        route = False
                        verdict = "needs_local_audit"

            cand.update(
                {
                    "error_family": fam,
                    "local_decidability": decision,
                    "defer_reason": reason,
                    "schema_anchor_score": round(schema_score, 4),
                    "evidence_anchor_score": round(evidence_score, 4),
                    "decision_margin": round(margin, 4),
                    "route_to_llm": route,
                    "local_second_filter_verdict": verdict,
                }
            )
            rec_decisions.append(cand)
            if decision == "local_strict":
                local_items.append((rec, cand))
            elif route:
                routed.append((rec, cand))
            else:
                ignored.append((rec, cand))
        rec["local_second_filter"] = rec_decisions

    routed.sort(
        key=lambda pair: (
            _routing_priority(pair[1]),
            _as_float(pair[1].get("need")),
            _as_float(pair[1].get("evidence_anchor_score")),
            _as_float(pair[1].get("schema_anchor_score")),
        ),
        reverse=True,
    )

    merged_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    representatives: dict[tuple[Any, ...], tuple[dict[str, Any], dict[str, Any]]] = {}
    deduped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rec, cand in routed:
        key = _route_key(rec, cand)
        rep = representatives.get(key)
        if rep is None:
            representatives[key] = (rec, cand)
            deduped.append((rec, cand))
            cand["merged_count"] = 1
        else:
            _, rep_cand = rep
            cand["local_decidability"] = "merged_duplicate"
            cand["route_to_llm"] = False
            cand["merged_into"] = rep_cand.get("candidate_id")
            cand["defer_reason"] = "与其他候选属于同类同证据仲裁点，合并到代表候选，避免重复调用 LLM。"
            rep_cand["merged_count"] = int(rep_cand.get("merged_count") or 1) + 1
            merged_items.append((rec, cand))
    routed = deduped

    if max_items is not None and max_items >= 0:
        routed, overflow = _apply_budget_with_diversity(routed, int(max_items))
        for rec, cand in overflow:
            cand["local_decidability"] = "budget_deferred"
            cand["route_to_llm"] = False
            cand["defer_reason"] = "超过本轮 LLM 判断点预算，保留为本地待审计项。"
        merged_items.extend(overflow)

    counts = Counter()
    for _, cand in local_items + routed + ignored + merged_items:
        counts[str(cand.get("local_decidability") or "unknown")] += 1
    return {
        "routed": routed,
        "local_items": local_items,
        "ignored": ignored,
        "merged_items": merged_items,
        "counts": dict(counts),
        "summary": {
            "total_candidates": len(local_items) + len(routed) + len(ignored) + len(merged_items),
            "sent_to_llm": len(routed),
            "kept_local": len(local_items),
            "ignored": len(ignored),
            "merged": len(merged_items),
        },
    }


def local_result_payload(rec: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    verdict = "confirmed_issue" if cand.get("local_second_filter_verdict") == "confirmed_issue" else "no_issue"
    return {
        "candidate_id": cand.get("candidate_id"),
        "dialogue_id": rec.get("dialogue_id"),
        "candidate_kind": cand.get("kind"),
        "source": cand.get("source"),
        "verdict": verdict,
        "confidence": 0.92 if verdict == "confirmed_issue" else 0.70,
        "reason": _clip(cand.get("defer_reason"), 160),
        "local_only": True,
        "local_decidability": cand.get("local_decidability"),
        "schema_anchor_score": cand.get("schema_anchor_score"),
        "evidence_anchor_score": cand.get("evidence_anchor_score"),
    }
