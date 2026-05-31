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
    conflict/violation, sending it to LongCat is usually audit noise.  If the
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
    """Whether a candidate has enough concrete material for LongCat.

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
    """Gate oracle candidates before sending them to LongCat.

    This is the latest-kernel counterpart of the older demo's local second
    filter. It is schema/evidence driven: local strict items stay local;
    noisy or unsupported items are ignored; only semantic/open-set candidates
    with enough anchored evidence are deferred to LongCat.
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

            if promoted:
                decision = "local_strict"
                reason = promote_reason
                route = False
                verdict = "confirmed_issue" if cand.get("source") == "acceptance" else "no_llm_needed"
            elif force_route_acceptance and cand.get("source") == "acceptance":
                decision = "force_defer"
                reason = "用户选择 LLM 判断点无限制：待仲裁验收候选送 LongCat；但仍不绕过本地证据门控。"
                route = True
                verdict = "defer_to_llm"
            elif cand.get("source") == "local" and fam in {"knowledge_violation", "constraint_violation"} and ledger_relation == "same_evidence_support":
                decision = "merged_local_no_issue"
                reason = "同一证据在本地账本中已有支持/安全结论，属于低价值重复仲裁点，合并为本地无问题审计项。"
                route = False
                verdict = "no_llm_needed"
            elif cand.get("source") == "local" and fam in {"knowledge_violation", "constraint_violation"} and ledger_relation == "same_evidence_insufficient_only":
                decision = "merged_low_value"
                reason = "同一证据没有产生冲突/违规，只是兄弟 claim 证据不足；为避免过量仲裁，合并为低价值审计项。"
                route = False
                verdict = "ignored"
            elif schema_score < 0.30 and evidence_score < 0.25:
                decision = "ignore_noise"
                reason = "schema 与候选证据锚定都不足，不进入 LongCat。"
                route = False
                verdict = "ignored"
            elif (not cand.get("evidence")) and fam != "flow_missing":
                decision = "ignore_noise"
                reason = "缺少可回查证据，不进入 LongCat。"
                route = False
                verdict = "ignored"
            elif evidence_score < 0.35 and fam != "flow_missing":
                decision = "ignore_noise"
                reason = "证据锚点过低，不进入 LongCat。"
                route = False
                verdict = "ignored"
            else:
                ev = str(cand.get("evaluability") or "semantic").strip().lower()
                has_focus = _has_traceable_evidence(cand, fam, schema_score, evidence_score)
                if not has_focus:
                    decision = "ignore_noise"
                    reason = "没有形成 schema 绑定 + 可回查证据的组合，不进入 LongCat。"
                    route = False
                    verdict = "ignored"
                elif ev in {"open_set", "unsupported"}:
                    if evidence_score < 0.60:
                        decision = "ignore_noise"
                        reason = "开放集候选证据强度不足，先保留本地灰区，不进入 LongCat。"
                        route = False
                        verdict = "ignored"
                    else:
                        decision = "open_set_defer"
                        reason = "存在聚焦证据但 schema 不足，进入开放集/审计判断。"
                        route = True
                        verdict = "defer_to_llm"
                elif schema_score < 0.55 or evidence_score < 0.45:
                    decision = "ignore_noise"
                    reason = "schema 或证据锚点不够，避免把低质量候选送入 LongCat。"
                    route = False
                    verdict = "ignored"
                else:
                    decision = "semantic_defer"
                    reason = "schema 可定位且证据足够，但本地语义关系不稳，送 LongCat 二级判断。"
                    route = True
                    verdict = "defer_to_llm"

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

    routed.sort(key=lambda pair: (_as_float(pair[1].get("need")), _as_float(pair[1].get("evidence_anchor_score")), _as_float(pair[1].get("schema_anchor_score"))), reverse=True)

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
            cand["defer_reason"] = "与其他候选属于同类同证据仲裁点，合并到代表候选，避免重复调用 LongCat。"
            rep_cand["merged_count"] = int(rep_cand.get("merged_count") or 1) + 1
            merged_items.append((rec, cand))
    routed = deduped

    if max_items is not None and max_items >= 0:
        overflow = routed[int(max_items):]
        routed = routed[: int(max_items)]
        for rec, cand in overflow:
            cand["local_decidability"] = "budget_deferred"
            cand["route_to_llm"] = False
            cand["defer_reason"] = "超过本轮 LongCat 判断点预算，保留为本地待审计项。"
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
        "sample_type": rec.get("sample_type"),
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
