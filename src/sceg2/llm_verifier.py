from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .longcat_client import LongCatClient, extract_json_object
from .local_second_filter import apply_local_second_filter, local_result_payload


SYSTEM_PROMPT = """你是一个复杂指令对话评估的中文局部仲裁器。
你只能判断输入候选项本身，不要重评整段对话，也不要补充状态图之外的新标准。
请根据 question、evidence、candidate_kind、error_family_cn 和 acceptance_context 做局部判定。

输出必须是严格 JSON，不要 Markdown，不要解释外壳：
{
  "results": [
    {
      "candidate_id": "...",
      "verdict": "问题成立 | 问题不成立 | 证据不足",
      "confidence": 0.0,
      "reason": "不超过60字的中文原因"
    }
  ]
}

中文 verdict 语义：
- 问题成立：候选问题被证据支持。对流程类表示确实未完成；对知识类表示确实存在事实冲突；对限制类表示确实违规；对上下文类表示确实未正确处理。
- 问题不成立：证据足以说明候选问题不成立。对知识类可表示事实表述是支持/正确；对限制类可表示安全表达；对流程类可表示已经完成。
- 证据不足：证据不够、候选绑定不清，或只能判断“提到了相关内容”但无法判定是否成立。

兼容说明：如果你输出 confirmed_issue / no_issue / uncertain，也会分别被视为 问题成立 / 问题不成立 / 证据不足。
"""


def _clip_text(x: Any, limit: int = 900) -> str:
    s = str(x or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "……[已截断]"


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _family_cn(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v == "flow_missing":
        return "流程缺失"
    if v == "knowledge_violation":
        return "知识事实冲突"
    if v == "constraint_violation":
        return "限制或边界违规"
    if v == "context_violation":
        return "上下文转场未妥善处理"
    if v == "semantic_or_context":
        return "语义或上下文灰区"
    return str(value or "未知灰区")


def _extract_results(content: str) -> list[dict[str, Any]]:
    try:
        obj = extract_json_object(content)
    except Exception:
        # LongCat 偶尔会输出数组外壳，做一次轻量兜底。
        s = str(content or "").strip()
        m = re.search(r"\[.*\]", s, flags=re.S)
        if not m:
            raise
        arr = json.loads(m.group(0))
        return arr if isinstance(arr, list) else []
    arr = obj.get("results") if isinstance(obj, dict) else None
    return arr if isinstance(arr, list) else []


def _norm_verdict(x: Any) -> str:
    v_raw = str(x or "").strip()
    v = v_raw.lower()
    yes = {
        "yes", "true", "confirmed", "confirm", "confirmed_issue", "issue", "problem",
        "成立", "确认", "确认成立", "问题成立", "冲突", "事实冲突", "违规", "限制违规", "未完成", "缺失",
    }
    no = {
        "no", "false", "no_issue", "not_issue", "safe", "not_confirmed",
        "不成立", "未成立", "安全", "问题不成立", "支持", "正确", "事实正确", "已完成", "无问题",
    }
    unsure = {"uncertain", "unknown", "insufficient", "证据不足", "不确定", "无法判断"}
    if v_raw in yes or v in yes:
        return "confirmed_issue"
    if v_raw in no or v in no:
        return "no_issue"
    if v_raw in unsure or v in unsure:
        return "uncertain"
    if any(x in v_raw for x in ["问题成立", "事实冲突", "限制违规", "确实违规", "确实缺失"]):
        return "confirmed_issue"
    if any(x in v_raw for x in ["问题不成立", "事实正确", "安全表达", "已经完成", "无违规"]):
        return "no_issue"
    if any(x in v_raw for x in ["证据不足", "无法判断", "不确定"]):
        return "uncertain"
    if "confirm" in v or ("issue" in v and "no" not in v):
        return "confirmed_issue"
    if "no" in v or "safe" in v:
        return "no_issue"
    return "uncertain"


def _payload_evidence(cand: dict[str, Any], limit: int = 6) -> list[str]:
    evidence = [str(x or "").strip() for x in (cand.get("evidence") or []) if str(x or "").strip()]
    if not evidence:
        return []
    selected = evidence[:limit]
    # Acceptance arbitration often appends the full assistant transcript after
    # schema ledger rows.  Keep that transcript even when ledger rows are many,
    # otherwise LongCat sees only local bookkeeping and misses the actual reply.
    transcript = next((x for x in evidence if "客服实际表达摘要" in x), None)
    if transcript and all("客服实际表达摘要" not in x for x in selected):
        if len(selected) >= limit:
            selected = selected[: max(0, limit - 1)] + [transcript]
        else:
            selected.append(transcript)
    return [_clip_text(x, 650) for x in selected[:limit]]


def _candidate_payload(rec: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    acc = rec.get("acceptance") or {}
    return {
        "candidate_id": cand.get("candidate_id"),
        "dialogue_id": rec.get("dialogue_id"),
        "sample_type": rec.get("sample_type"),
        "candidate_kind": cand.get("kind"),
        "source": cand.get("source"),
        "error_family_cn": _family_cn(cand.get("error_family")),
        "judgement_hint": "只判断这个候选项。若是知识灰区，冲突=问题成立，支持/正确=问题不成立，证据不足=证据不足；若是限制灰区，违规=问题成立，安全=问题不成立。",
        "binding": {
            "node_id": cand.get("node_id"),
            "requirement_id": cand.get("requirement_id"),
            "knowledge_id": cand.get("knowledge_id"),
            "constraint_id": cand.get("constraint_id"),
            "context_id": cand.get("context_id"),
        },
        "question": _clip_text(cand.get("question"), 500),
        "evidence": _payload_evidence(cand, limit=6),
        "local_priority": {"need": cand.get("need"), "strength": cand.get("strength")},
        "acceptance_context": {
            "local_result": acc.get("result"),
            "local_reasons": acc.get("reasons") or [],
        },
    }


def _summarize_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    for rec in records:
        total["calls"] += 1
        total["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(rec.get("completion_tokens") or 0)
        total["total_tokens"] += int(rec.get("total_tokens") or 0)
    return {"total": total, "records": records}


def apply_llm_verifier(
    records: list[dict[str, Any]],
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    mode: str | None = "off",
    max_items: int | None = None,
) -> dict[str, Any]:
    """Run optional LongCat arbitration after local second filtering.

    The demo path is intentionally two-stage:
    1. The latest local evaluator creates an oracle queue.
    2. The local second filter keeps strict/noisy items local and only routes
       semantic/open-set candidates with anchored evidence to LongCat.
    """
    mode = (mode or "off").strip().lower()
    if mode in {"", "false", "none", "off", "关闭"}:
        for rec in records:
            rec["llm_verifier"] = {"mode": "off", "items": [], "local_second_filter": []}
        return {
            "mode": "off",
            "enabled": False,
            "items_sent": 0,
            "results": [],
            "local_second_filter": {"total_candidates": 0, "sent_to_llm": 0, "kept_local": 0, "ignored": 0},
            "token_usage": _summarize_usage([]),
        }
    if mode in {"audit", "shadow", "审计", "旁路"}:
        mode = "shadow"
    elif mode in {"assist", "辅助", "arbitrate", "仲裁"}:
        mode = "assist"
    else:
        mode = "shadow"

    unlimited = max_items is not None and int(max_items) < 0
    budget = None if unlimited else int(max_items if max_items is not None else 36)
    gate = apply_local_second_filter(records, max_items=budget, force_route_acceptance=unlimited)
    routed = gate["routed"]
    local_items = gate["local_items"]
    merged_items = gate.get("merged_items") or []

    for rec in records:
        rec["llm_verifier"] = {
            "mode": mode,
            "items": [],
            "local_second_filter": rec.get("local_second_filter") or [],
        }

    attached: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    local_attached: list[dict[str, Any]] = []
    for rec, cand in local_items:
        payload = local_result_payload(rec, cand)
        local_attached.append(payload)
        grouped[str(rec.get("dialogue_id") or "")].append(payload)
        attached.append(payload)

    if routed:
        client = LongCatClient(api_key=api_key, base_url=base_url, model=model, timeout=None)
        if not client.enabled():
            raise RuntimeError("已开启大模型二级判断，但没有 LongCat API Key。")

        payload = [_candidate_payload(rec, cand) for rec, cand in routed]
        user_prompt = "请只判断以下经过本地二次筛选后的局部候选项。不要重评整段对话。\n" + json.dumps({"items": payload}, ensure_ascii=False, indent=2)
        content, usage = client.chat_with_usage(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
            purpose="llm_verifier_batch",
        )
        raw_results = _extract_results(content)
    else:
        payload = []
        usage = {}
        raw_results = []

    by_id: dict[str, dict[str, Any]] = {}
    for obj in raw_results:
        if not isinstance(obj, dict):
            continue
        cid = str(obj.get("candidate_id") or "")
        if not cid:
            continue
        by_id[cid] = {
            "candidate_id": cid,
            "verdict": _norm_verdict(obj.get("verdict")),
            "verdict_raw": _clip_text(obj.get("verdict"), 80),
            "confidence": round(_as_float(obj.get("confidence"), 0.0), 4),
            "reason": _clip_text(obj.get("reason"), 160),
            "local_only": False,
        }

    resolved_by_candidate: dict[str, dict[str, Any]] = {}
    for rec, cand in routed:
        cid = str(cand.get("candidate_id") or "")
        res = by_id.get(cid) or {"candidate_id": cid, "verdict": "uncertain", "confidence": 0.0, "reason": "LLM 未返回该候选项。", "local_only": False}
        enriched = dict(res)
        enriched.update({
            "dialogue_id": rec.get("dialogue_id"),
            "sample_type": rec.get("sample_type"),
            "candidate_kind": cand.get("kind"),
            "source": cand.get("source"),
            "question": cand.get("question"),
            "local_decidability": cand.get("local_decidability"),
            "schema_anchor_score": cand.get("schema_anchor_score"),
            "evidence_anchor_score": cand.get("evidence_anchor_score"),
            "merged_count": cand.get("merged_count"),
        })
        resolved_by_candidate[cid] = enriched
        attached.append(enriched)
        grouped[str(rec.get("dialogue_id") or "")].append(enriched)

    for rec, cand in merged_items:
        cid = str(cand.get("candidate_id") or "")
        rep_id = str(cand.get("merged_into") or "")
        rep = resolved_by_candidate.get(rep_id)
        if rep:
            enriched = dict(rep)
            enriched.update({
                "candidate_id": cid,
                "dialogue_id": rec.get("dialogue_id"),
                "sample_type": rec.get("sample_type"),
                "candidate_kind": cand.get("kind"),
                "source": cand.get("source"),
                "question": cand.get("question"),
                "local_decidability": cand.get("local_decidability"),
                "schema_anchor_score": cand.get("schema_anchor_score"),
                "evidence_anchor_score": cand.get("evidence_anchor_score"),
                "merged_into": rep_id,
                "reason": _clip_text("同类候选合并复用代表候选的 LongCat 判定：" + str(rep.get("reason") or ""), 160),
            })
            attached.append(enriched)
            grouped[str(rec.get("dialogue_id") or "")].append(enriched)

    for rec in records:
        did = str(rec.get("dialogue_id") or "")
        items = grouped.get(did, [])
        rec.setdefault("llm_verifier", {"mode": mode, "items": [], "local_second_filter": rec.get("local_second_filter") or []})
        rec["llm_verifier"]["items"] = items
        exp = rec.get("explanation") or {}
        exp["llm_verifier_summary"] = items
        exp["local_second_filter_summary"] = rec.get("local_second_filter") or []
        rec["explanation"] = exp

    changed = 0
    arbitration_closed = 0
    if mode == "assist":
        for rec in records:
            acc = rec.get("acceptance") or {}
            if acc.get("result") != "待仲裁":
                continue
            items = (rec.get("llm_verifier") or {}).get("items") or []
            confirmed = [
                x for x in items
                if x.get("source") == "acceptance" and x.get("verdict") == "confirmed_issue" and _as_float(x.get("confidence"), 0.0) >= 0.55
            ]
            reasons = list(acc.get("reasons") or [])
            if confirmed:
                acc["result"] = "仲裁通过"
                acc["passed"] = True
                reasons.append("本地二次筛选/LongCat 辅助确认待仲裁预期问题成立，因此该负包按仲裁通过处理。")
                changed += 1
            elif unlimited:
                # In unlimited mode the queue has been fully adjudicated.  If
                # LongCat still does not confirm the issue, the sample should be
                # marked as not passed rather than remaining in a pending state.
                acc["result"] = "不通过"
                acc["passed"] = False
                reasons.append("LLM 判断点选择无限制：待仲裁项已全部送审，但 LongCat 未确认预设问题成立，因此不再保留待仲裁。")
                arbitration_closed += 1
            else:
                continue
            acc["reasons"] = reasons
            rec["acceptance"] = acc
            exp = rec.get("explanation") or {}
            if isinstance(exp.get("acceptance_summary"), dict):
                exp["acceptance_summary"]["验收结果"] = acc["result"]
                exp["acceptance_summary"]["是否通过"] = acc["passed"]
                exp["acceptance_summary"]["说明"] = reasons
            if isinstance((exp.get("plain_summary") or {}).get("样本验收"), dict):
                exp["plain_summary"]["样本验收"]["验收结果"] = acc["result"]
                exp["plain_summary"]["样本验收"]["是否通过"] = acc["passed"]
                exp["plain_summary"]["样本验收"]["说明"] = reasons
            rec["explanation"] = exp

    gate_summary = dict(gate.get("summary") or {})
    gate_summary["decision_counts"] = gate.get("counts") or {}
    return {
        "mode": mode,
        "enabled": True,
        "items_sent": len(payload),
        "assist_changed_dialogues": changed,
        "arbitration_closed_without_pending": arbitration_closed,
        "unlimited": unlimited,
        "results": attached,
        "local_second_filter": gate_summary,
        "local_only_results": local_attached,
        "token_usage": _summarize_usage([usage] if usage else []),
        "note": "shadow/审计模式只记录结果；assist/辅助模式只把本地待仲裁负包在本地二次筛选或 LongCat 确认后改为仲裁通过。",
    }
