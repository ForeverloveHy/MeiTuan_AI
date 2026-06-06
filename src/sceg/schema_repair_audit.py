from __future__ import annotations

import json
import re
from typing import Any


def _collect_strings(value: Any, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _collect_strings(v, out)
    elif isinstance(value, (str, int, float)):
        s = str(value).strip()
        if s:
            out.append(s)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def schema_text(graph: dict[str, Any]) -> str:
    parts: list[str] = []
    _collect_strings(graph, parts)
    return "\n".join(parts)


def parse_binding_hints(binding_hints: str | None) -> list[dict[str, Any]]:
    text = str(binding_hints or "")
    if not text.strip():
        return []
    # Prefer the last JSON object in the prompt tail because the prose before it
    # is instructional text.  This parser is deliberately generic and ignores
    # dialogue turns, evidence spans and wrong statements.
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for start in reversed(starts):
        candidate = text[start:].strip()
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        rows = obj.get("binding_hints") if isinstance(obj, dict) else None
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def _all_schema_ids(graph: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for key, val in v.items():
                if key in {"id", "node_id", "source", "target"} or key.endswith("_id"):
                    if isinstance(val, (str, int, float)) and str(val).strip():
                        ids.add(str(val).strip())
                if key == "aliases" and isinstance(val, list):
                    for x in val:
                        if isinstance(x, (str, int, float)) and str(x).strip():
                            ids.add(str(x).strip())
                walk(val)
        elif isinstance(v, list):
            for item in v:
                walk(item)
    walk(graph)
    return ids




def _pattern_values(patterns: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        for key in ("any", "all", "regex_any"):
            for value in pat.get(key) or []:
                t = str(value or "").strip()
                if t:
                    out.append(t)
    return out


def _has_contrastive_operator(text: str) -> bool:
    t = _norm(text)
    groups = (("前", "后"), ("上", "下"), ("高", "低"), ("多", "少"), ("早", "晚"), ("内", "外"), ("已", "未"), ("能", "不能"), ("会", "不会"))
    return any(any(op in t for op in group) for group in groups)




def _has_comparative_direction(text: str) -> bool:
    t = _norm(text)
    low = ("更低", "较低", "偏低", "便宜", "低一些", "少", "减少")
    high = ("更高", "较高", "偏高", "更贵", "高一些", "多", "增加")
    return any(x in t for x in low + high)


def _pattern_shape_risky(patterns: list[dict[str, Any]] | None) -> bool:
    for p in patterns or []:
        if not isinstance(p, dict):
            continue
        values = _pattern_values([p])
        if p.get("any") and not p.get("all") and not p.get("regex_any"):
            if any(len(_norm(v)) <= 4 or _has_contrastive_operator(v) or _has_comparative_direction(v) for v in values):
                return True
    return False

def _schema_quality_warnings(graph: dict[str, Any], max_items: int = 40) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for item in graph.get("knowledge_table") or []:
        if not isinstance(item, dict):
            continue
        for claim in item.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            refute_values = _pattern_values(claim.get("refute_patterns") or claim.get("conflict_patterns") or [])
            support_values = _pattern_values(claim.get("support_patterns") or [])
            if any(_has_comparative_direction(v) for v in support_values) and not refute_values:
                warnings.append({
                    "type": "comparative_support_missing_refute",
                    "knowledge_id": item.get("id") or "",
                    "claim_id": claim.get("id") or "",
                    "message": "support patterns contain a comparative direction but refute_patterns are empty; element细化 should add object-gated opposite-direction refutes, using complete attribute-direction phrases rather than bare direction characters.",
                })
            if _pattern_shape_risky(claim.get("refute_patterns") or claim.get("conflict_patterns") or []):
                warnings.append({
                    "type": "risky_short_or_any_only_refute",
                    "knowledge_id": item.get("id") or "",
                    "claim_id": claim.get("id") or "",
                    "message": "refute pattern is short or any-only; element细化 should add explicit claim/object anchors and preserve decisive operators so sibling facts do not contaminate each other.",
                })
            if any(_has_contrastive_operator(v) for v in refute_values + support_values):
                risky_any = [p for p in claim.get("refute_patterns") or [] if isinstance(p, dict) and p.get("any") and not p.get("all") and not p.get("regex_any")]
                if risky_any:
                    warnings.append({
                        "type": "contrastive_refute_needs_operator_preservation",
                        "knowledge_id": item.get("id") or "",
                        "claim_id": claim.get("id") or "",
                        "message": "support/refute contains contrastive time/direction/polarity operators; element细化 should keep the decisive operator inside exact, all-gated or regex patterns so opposite forms are not collapsed.",
                    })
            if claim.get("refute_patterns") and not claim.get("claim_patterns"):
                warnings.append({
                    "type": "refute_without_claim_anchor",
                    "knowledge_id": item.get("id") or "",
                    "claim_id": claim.get("id") or "",
                    "message": "refute patterns exist without a claim/object anchor; element细化 should add object and attribute anchors instead of broad any-only refutes.",
                })
            if len(warnings) >= max_items:
                return warnings
    for rule in graph.get("constraint_table") or []:
        if not isinstance(rule, dict):
            continue
        scope = rule.get("violation_scope") or {}
        protected_values = _pattern_values(scope.get("protected_objects") or [])
        forbidden_values = _pattern_values(scope.get("forbidden_actions") or [])
        safe_values = _pattern_values(scope.get("safe_actions") or [])
        prohibited = rule.get("prohibited") or []
        if not protected_values or not forbidden_values:
            warnings.append({
                "type": "constraint_scope_incomplete",
                "constraint_id": rule.get("id") or "",
                "message": "constraint lacks protected_objects or forbidden_actions; element细化 should extract controlled objects/results and prohibited speech acts from the instruction into violation_scope.",
            })
        if prohibited and not any(isinstance(p, dict) and (p.get("self_sufficient") or p.get("requires_trigger")) for p in prohibited):
            warnings.append({
                "type": "constraint_trigger_mode_unclear",
                "constraint_id": rule.get("id") or "",
                "message": "prohibited patterns do not state whether they are self_sufficient or require a user trigger; element细化 should mark self-contained violations with self_sufficient=true and contextual violations with requires_trigger=true.",
            })
        if protected_values and forbidden_values and len(protected_values) <= 2 and len(forbidden_values) <= 2:
            warnings.append({
                "type": "constraint_paraphrase_gap",
                "constraint_id": rule.get("id") or "",
                "message": "constraint scope has very few protected/action paraphrases; element细化 should add instruction-derived object aliases, result aliases, action aliases, and safe-action aliases without using sample wrong statements.",
            })
        if len(warnings) >= max_items:
            return warnings

    for idx, policy in enumerate(graph.get("terminal_policies") or []):
        if not isinstance(policy, dict):
            continue
        trigger_values = _pattern_values(policy.get("trigger") or [])
        has_regex = any(isinstance(p, dict) and p.get("regex_any") for p in policy.get("trigger") or [])
        if policy.get("suppress_nodes_after_safe_response") and len(trigger_values) < 8 and not has_regex:
            warnings.append({
                "type": "terminal_trigger_paraphrase_gap",
                "policy_id": policy.get("id") or f"terminal_policy_{idx}",
                "message": "terminal policy suppresses later nodes but trigger coverage is mostly literal; element细化 should infer broader user-condition paraphrases from the instruction and current policy description.",
            })
        if len(warnings) >= max_items:
            return warnings
    return warnings

def audit_schema_repair_need(
    graph: dict[str, Any],
    binding_hints: str | None,
    max_items: int = 80,
    repair_mode: str = "quality",
) -> dict[str, Any]:
    """Build a small LLM-repair audit for the generated graph.

    The audit never patches the graph.  It only summarizes schema gaps so the
    next LLM call can refine the schema.  All signals come from LLM's
    current schema plus package metadata IDs/coverage intent, not from answer-key
    evidence spans or task-specific Python dictionaries.

    第二阶段 element细化是必要建图阶段，不能由审计结果或用户选项跳过。
    本函数只负责提供细化重点与质量提示；`needs_repair` 在新语义下表示
    “必须进入第二阶段 element细化”。
    """
    mode = "required"
    rows = parse_binding_hints(binding_hints)
    ids = _all_schema_ids(graph)
    full_text_n = _norm(schema_text(graph))
    missing_targets: list[dict[str, Any]] = []
    positive_design_rows = 0
    by_kind: dict[str, int] = {}
    for row in rows:
        sample_types_raw = row.get("sample_types") if isinstance(row.get("sample_types"), list) else None
        sample_types = [str(x or "") for x in (sample_types_raw or [row.get("sample_type") or ""]) if str(x or "").strip()]
        sample_type_label = ",".join(sample_types)
        positive_design_value = row.get("positive_designs") or row.get("source_positive_design")
        if "positive" in sample_types and positive_design_value:
            positive_design_rows += 1
        target_node = str(row.get("target_node_id") or "").strip()
        target_id = str(row.get("target_id") or "").strip()
        kind = str(row.get("target_kind") or row.get("error_family") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + int(row.get("count") or 1)
        node_ok = (not target_node) or target_node in ids or _norm(target_node) in full_text_n
        target_ok = (not target_id) or target_id in ids or _norm(target_id) in full_text_n
        if (not node_ok) or (not target_ok):
            missing_targets.append({
                "target_node_id": target_node,
                "target_id": target_id,
                "target_kind": kind,
                "sample_type": sample_type_label,
                "source_node": row.get("source_node") or row.get("source_nodes") or "",
                "error_family": row.get("error_family") or row.get("error_families") or "",
                "has_positive_design": bool(positive_design_value),
                "node_missing": not node_ok,
                "target_missing": not target_ok,
            })
        if len(missing_targets) >= max_items:
            break
    nodes = graph.get("nodes") or []
    knowledge = graph.get("knowledge_table") or []
    constraints = graph.get("constraint_table") or []
    structural_warnings: list[str] = []
    if not nodes:
        structural_warnings.append("nodes_empty")
    if not knowledge:
        structural_warnings.append("knowledge_table_empty")
    if not constraints:
        structural_warnings.append("constraint_table_empty")
    for rule in constraints:
        if isinstance(rule, dict) and not rule.get("violation_scope"):
            structural_warnings.append("constraint_missing_violation_scope")
            break
    quality_warnings = _schema_quality_warnings(graph)
    blocking_gaps = bool(missing_targets or structural_warnings)
    # 新机制下第二阶段 element细化必跑；审计只决定提示重点，不决定是否触发。
    needs = True
    skipped_advisory = False
    return {
        "needs_repair": needs,
        "refine_mode": mode,
        "blocking_repair_needed": blocking_gaps,
        "quality_repair_needed": bool(quality_warnings),
        "quality_warnings_kept_as_advisory": skipped_advisory,
        "binding_hint_count": len(rows),
        "positive_design_hint_count": positive_design_rows,
        "schema_counts": {"nodes": len(nodes), "knowledge_table": len(knowledge), "constraint_table": len(constraints)},
        "binding_hint_kinds": by_kind,
        "missing_or_unbound_targets": missing_targets,
        "structural_warnings": structural_warnings,
        "quality_warnings": quality_warnings,
        "refine_policy": "Second-stage element refinement is mandatory. Ask LLM to return a complete refined schema JSON; local code must not add task facts.",
    }


def build_repair_instruction(original_instruction: str, graph: dict[str, Any], audit: dict[str, Any], binding_hints: str | None) -> str:
    payload = {
        "original_complex_instruction": original_instruction,
        "current_schema_json": graph,
        "local_schema_audit": audit,
        "binding_hints_tail": parse_binding_hints(binding_hints),
    }
    # Compact JSON substantially reduces the second-call prompt size while
    # preserving every schema field needed by LLM.  This is not a semantic
    # shortcut and does not hide any repair signal.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
