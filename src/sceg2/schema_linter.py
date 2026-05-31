from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any


def _compact(value: Any) -> str:
    return "".join(str(value or "").split())


def _walk_patterns(container: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                out.append(item)
    return out


def _pattern_values(patterns: list[dict[str, Any]]) -> set[str]:
    vals: set[str] = set()
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        for key in ("all", "any", "none", "regex_any"):
            for value in pat.get(key) or []:
                t = _compact(value)
                if t:
                    vals.add(t)
    return vals


def _node_text(node: dict[str, Any]) -> str:
    parts: list[str] = [str(node.get("id") or ""), str(node.get("name") or ""), str(node.get("type") or "")]
    parts.extend(str(x or "") for x in node.get("aliases") or [])
    for req in node.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        parts.append(str(req.get("id") or ""))
        parts.append(str(req.get("text") or req.get("description") or req.get("name") or ""))
        for group in req.get("evidence_groups") or []:
            if not isinstance(group, dict):
                continue
            parts.append(str(group.get("id") or ""))
            parts.append(str(group.get("description") or group.get("text") or ""))
    return " ".join(parts)


def _looks_like_followup_handoff_node(node: dict[str, Any]) -> bool:
    """Detect generic follow-up / handoff nodes emitted as hard mainline tasks.

    This does not name a business domain.  It only recognizes structural labels
    that describe out-of-band follow-up/contact work.  Such nodes are often
    optional unless the dialogue or schema explicitly triggers them.
    """
    text = _compact(_node_text(node))
    if not text:
        return False
    markers = ("followup", "follow_up", "handoff", "callback", "contactlater", "recontact")
    return any(m in text for m in markers)




_SOFT_REQUIREMENT_MARKERS = (
    "鼓励", "建议", "尽量", "减少", "避免", "少", "不要", "简短",
    "自然结束", "礼貌", "重复", "30字", "换种方式", "风格", "话术",
)


def _requirement_text(req: dict[str, Any]) -> str:
    parts: list[str] = [str(req.get("id") or ""), str(req.get("text") or req.get("description") or req.get("name") or "")]
    for group in req.get("evidence_groups") or []:
        if isinstance(group, dict):
            parts.append(str(group.get("id") or ""))
            parts.append(str(group.get("description") or group.get("text") or ""))
            for pat in group.get("patterns") or []:
                if isinstance(pat, dict):
                    for key in ("any", "all", "regex_any"):
                        parts.extend(str(x or "") for x in pat.get(key) or [])
    return " ".join(parts)


def _is_soft_requirement(req: dict[str, Any]) -> bool:
    """Detect soft/style/exhortation requirements without task vocabulary.

    This repairs a common LongCat graph issue: secondary advice or style
    requirements are emitted as hard `always` tasks.  The detector only uses
    generic speech-act markers (encourage, avoid, be brief, repeat, style) and
    never names a business domain or product.
    """
    text = _requirement_text(req)
    return any(marker in text for marker in _SOFT_REQUIREMENT_MARKERS)


def _requirement_has_required_evidence(req: dict[str, Any]) -> bool:
    groups = req.get("evidence_groups") or []
    return any(isinstance(g, dict) and (g.get("required", True) is not False) for g in groups)

def _looks_like_answer_key_pattern(pat: dict[str, Any]) -> bool:
    """Detect schema rules copied from dataset answer metadata.

    This is domain-neutral: it only checks provenance markers and overly long
    one-off sentence patterns.  It does not know business vocabulary.
    """
    reason = str(pat.get("reason") or pat.get("source") or "")
    provenance_markers = ("负包注入", "数据包注", "answer_key", "injected_error")
    if any(x in reason for x in provenance_markers):
        return True
    values: list[str] = []
    for key in ("any", "all"):
        values.extend(str(x or "").strip() for x in pat.get(key) or [])
    # A single long full sentence with terminal punctuation is more likely to be
    # copied from an answer key than generated as a reusable schema rule.
    if len(values) == 1:
        v = values[0]
        if len(_compact(v)) >= 16 and any(v.endswith(ch) for ch in ("。", ".", "！", "?", "？")):
            return True
    return False


def _remove_answer_key_patterns(patterns: list[dict[str, Any]], issues: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    removed = 0
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        if _looks_like_answer_key_pattern(pat):
            removed += 1
            continue
        kept.append(pat)
    if removed:
        issues.append({"level": "warning", "type": "answer_key_pattern_removed", "path": path, "message": f"移除疑似来自样本答案的精确规则 {removed} 条，避免负包答案泄漏进 schema。"})
    return kept


def _pattern_signature(pat: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(pat.get("speaker") or ""),
        tuple(sorted(_compact(x) for x in pat.get("all") or [] if _compact(x))),
        tuple(sorted(_compact(x) for x in pat.get("any") or [] if _compact(x))),
        tuple(sorted(_compact(x) for x in pat.get("regex_any") or [] if _compact(x))),
        tuple(sorted(_compact(x) for x in pat.get("none") or [] if _compact(x))),
    )


def _dedupe_patterns(patterns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    removed = 0
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        sig = _pattern_signature(pat)
        if sig in seen:
            removed += 1
            continue
        seen.add(sig)
        out.append(pat)
    return out, removed


def _remove_overlapping_refutes(block: dict[str, Any], issues: list[dict[str, Any]], path: str) -> int:
    support = _walk_patterns(block.get("support_patterns"))
    refute_key = "refute_patterns" if "refute_patterns" in block else "conflict_patterns" if "conflict_patterns" in block else None
    if not refute_key:
        return 0
    refutes = _remove_answer_key_patterns(_walk_patterns(block.get(refute_key)), issues, path + "." + refute_key)
    support_sigs = {_pattern_signature(p) for p in support}
    support_vals = _pattern_values(support)
    kept: list[dict[str, Any]] = []
    removed = 0
    for pat in refutes:
        sig = _pattern_signature(pat)
        vals = _pattern_values([pat])
        exact_overlap = sig in support_sigs
        value_overlap = bool(vals and vals <= support_vals)
        if exact_overlap or value_overlap:
            removed += 1
            issues.append({
                "level": "warning",
                "type": "support_refute_overlap_repaired",
                "path": path,
                "message": "发现同一证据同时出现在支持证据和反驳证据中，已从反驳侧移除。",
                "pattern": pat,
            })
            continue
        kept.append(pat)
    deduped, dup_removed = _dedupe_patterns(kept)
    if dup_removed:
        issues.append({
            "level": "info",
            "type": "duplicate_refute_removed",
            "path": path,
            "message": f"移除重复反驳证据 {dup_removed} 条。",
        })
    block[refute_key] = deduped
    return removed + dup_removed


def _lint_nodes(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    for node_idx, node in enumerate(data.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        activation = node.get("activation") or {}
        mode = str(activation.get("mode") or "always")
        if mode not in {"always", "user_triggered", "condition", "optional"}:
            issues.append({"level": "warning", "type": "unknown_activation_mode", "path": f"nodes[{node_idx}].activation", "message": f"未知 activation.mode={mode}，评估时可能按默认逻辑处理。"})
        if mode in {"user_triggered", "condition"} and not activation.get("patterns"):
            node["required"] = False
            issues.append({"level": "warning", "type": "conditional_without_trigger_repaired", "path": f"nodes[{node_idx}]", "message": "条件/用户触发节点缺少触发证据，已降级为非必需，避免未触发分支误扣。"})
        if str(node.get("type") or "") == "terminal" and not (node.get("requirements") or node.get("evidence_groups")):
            node["required"] = False
            activation["mode"] = "optional"
            node["activation"] = activation
            issues.append({"level": "warning", "type": "empty_terminal_made_optional", "path": f"nodes[{node_idx}]", "message": "terminal 节点没有可执行证据组，已改为 optional，避免把自然结束误当成核心流程缺失。"})
        if mode == "always" and node.get("required", True) is not False and _looks_like_followup_handoff_node(node):
            node["required"] = False
            activation["mode"] = "optional"
            node["activation"] = activation
            for req in node.get("requirements") or []:
                if isinstance(req, dict):
                    req["required"] = False
                    for group in req.get("evidence_groups") or []:
                        if isinstance(group, dict):
                            group["required"] = False
            issues.append({"level": "warning", "type": "followup_handoff_made_optional", "path": f"nodes[{node_idx}]", "message": "检测到后续联系/交接类节点被 LongCat 放入 always 主线，已降为 optional；若样本显式触发该目标，仍可通过 coverage/negative target 验收。"})

        reqs = node.get("requirements") or []
        hard_req_count = sum(1 for r in reqs if isinstance(r, dict) and (r.get("required", True) is not False) and not _is_soft_requirement(r) and _requirement_has_required_evidence(r))
        for req_idx, req in enumerate(reqs):
            if isinstance(req, dict) and hard_req_count >= 1 and req.get("required", True) is not False and _is_soft_requirement(req):
                req["required"] = False
                req.setdefault("runtime_notes", [])
                req["runtime_notes"] = [*list(req.get("runtime_notes") or []), "schema_linter_soft_requirement_optional"]
                for g in req.get("evidence_groups") or []:
                    if isinstance(g, dict):
                        g["required"] = False
                issues.append({"level": "warning", "type": "soft_requirement_made_optional", "path": f"nodes[{node_idx}].requirements[{req_idx}]", "message": "检测到鼓励/建议/风格类小任务与同节点核心任务并存，已降为 optional；负包若显式绑定该 requirement 仍会被验收层检查。"})
            if not isinstance(req, dict):
                continue
            groups = req.get("evidence_groups") or []
            if not groups:
                issues.append({"level": "warning", "type": "requirement_without_group", "path": f"nodes[{node_idx}].requirements[{req_idx}]", "message": "小任务没有证据组，可能无法被本地评估器命中。"})
            for group_idx, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                patterns = _walk_patterns(group.get("patterns"))
                group["patterns"], removed = _dedupe_patterns(patterns)
                if removed:
                    issues.append({"level": "info", "type": "duplicate_group_pattern_removed", "path": f"nodes[{node_idx}].requirements[{req_idx}].evidence_groups[{group_idx}]", "message": f"移除重复证据表达 {removed} 条。"})
                for pat_idx, pat in enumerate(group.get("patterns") or []):
                    any_vals = [str(x).strip() for x in pat.get("any") or [] if str(x).strip()]
                    if pat.get("speaker") in {None, "", "assistant"} and len(any_vals) >= 4 and not pat.get("min_any_hits"):
                        pat["min_any_hits"] = 2
                        pat.setdefault("compiler_notes", [])
                        pat["compiler_notes"] = [*list(pat.get("compiler_notes") or []), "schema_linter_broad_any_min2"]
                        issues.append({"level": "info", "type": "broad_any_strengthened", "path": f"nodes[{node_idx}].requirements[{req_idx}].evidence_groups[{group_idx}].patterns[{pat_idx}]", "message": "单个 evidence any 候选过多，已要求至少命中 2 个证据，避免只靠话题词完成节点。"})


def _lint_relations(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    nodes = {str(n.get("id")): n for n in data.get("nodes") or [] if isinstance(n, dict)}
    for edge_idx, edge in enumerate(data.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        rel = str(edge.get("relation") or "soft_order")
        tgt = nodes.get(str(edge.get("target") or ""))
        if rel == "branch" and tgt:
            activation = tgt.get("activation") or {}
            if str(activation.get("mode") or "always") == "always":
                activation["mode"] = "user_triggered"
                tgt["activation"] = activation
                tgt["required"] = False
                issues.append({"level": "warning", "type": "branch_target_activation_repaired", "path": f"edges[{edge_idx}].target", "message": "branch 目标节点原本像必跑节点，已改为用户触发/非必需，避免未触发分支误扣。"})


def _lint_knowledge(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    for item_idx, item in enumerate(data.get("knowledge_table") or []):
        if not isinstance(item, dict):
            continue
        _remove_overlapping_refutes(item, issues, f"knowledge_table[{item_idx}]")
        claims = item.get("claims") or []
        for claim_idx, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            _remove_overlapping_refutes(claim, issues, f"knowledge_table[{item_idx}].claims[{claim_idx}]")
            if claim.get("refute_patterns") and not claim.get("claim_patterns"):
                issues.append({"level": "warning", "type": "refute_without_claim_anchor", "path": f"knowledge_table[{item_idx}].claims[{claim_idx}]", "message": "反驳证据缺少 claim_patterns 作为对象锚点，建议 LongCat 在知识声明里补对象/属性锚点。"})



def _ensure_constraint_violation_scope(rule: dict[str, Any]) -> None:
    scope = dict(rule.get("violation_scope") or {})
    scope.setdefault("protected_objects", _derive_constraint_scope_objects(rule))
    scope.setdefault("forbidden_actions", list(rule.get("prohibited") or []))
    scope.setdefault("safe_actions", list(rule.get("safe_context") or []))
    scope.setdefault("ambiguous_zone", list(rule.get("unresolved") or rule.get("grey_zone") or []))
    rule["violation_scope"] = scope


def _derive_constraint_scope_objects(rule: dict[str, Any]) -> list[dict[str, Any]]:
    texts: list[str] = [str(rule.get("id") or ""), str(rule.get("name") or ""), str(rule.get("description") or "")]
    texts.extend(str(x or "") for x in rule.get("aliases") or [])
    for key in ("prohibited", "safe_context"):
        for pat in rule.get(key) or []:
            if isinstance(pat, dict):
                for field in ("any", "all", "regex_any"):
                    texts.extend(str(x or "") for x in pat.get(field) or [])
                if pat.get("reason"):
                    texts.append(str(pat.get("reason")))
    stop = {"禁止", "不能", "不得", "不应", "承诺", "保证", "确保", "人工", "系统", "继续", "必须", "强制", "私下", "安全", "回复", "说明", "规则", "按规则", "无法", "不能保证", "不承诺", "我帮你", "帮你", "帮您", "处理", "操作", "可以"}
    candidates: list[str] = []
    for text in texts:
        for token in re.split(r"[，。；;、/\s]+", text):
            token = str(token or "").strip()
            token = re.sub(r"^(禁止|不能|不得|不应|承诺|保证|确保)", "", token)
            token = re.sub(r"(要求|证据|违规|安全|处理|操作|回复|说明)$", "", token)
            if len(token) >= 2 and token not in stop:
                candidates.append(token)
        for m in re.findall(r'[《“"]([^《》“”"]{2,20})[》”"]', text):
            if m and m not in stop:
                candidates.append(m)
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        key = _compact(c)
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return [{"any": out[:12]}] if out else []

def _lint_constraints(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    for idx, rule in enumerate(data.get("constraint_table") or []):
        if not isinstance(rule, dict):
            continue
        for key in ("prohibited", "safe_context", "trigger", "unresolved"):
            raw_pats = _walk_patterns(rule.get(key))
            if key in {"prohibited", "unresolved"}:
                raw_pats = _remove_answer_key_patterns(raw_pats, issues, f"constraint_table[{idx}].{key}")
            empty_removed = 0
            if key in {"prohibited", "unresolved"}:
                cleaned = []
                for pat in raw_pats:
                    vals = _pattern_values([pat])
                    if not vals and not pat.get("regex_any"):
                        empty_removed += 1
                        continue
                    cleaned.append(pat)
                raw_pats = cleaned
                if empty_removed:
                    issues.append({"level": "warning", "type": "empty_constraint_pattern_removed", "path": f"constraint_table[{idx}].{key}", "message": f"移除空限制规则 {empty_removed} 条，避免空 any 被误当作任意违规。"})
            pats, removed = _dedupe_patterns(raw_pats)
            if removed:
                issues.append({"level": "info", "type": "duplicate_constraint_pattern_removed", "path": f"constraint_table[{idx}].{key}", "message": f"移除重复限制证据 {removed} 条。"})
            rule[key] = pats
        _ensure_constraint_violation_scope(rule)
        scope = rule.get("violation_scope") or {}
        if rule.get("severity") in {"high", "critical"} and not scope.get("protected_objects") and not rule.get("prohibited"):
            issues.append({"level": "warning", "type": "constraint_scope_incomplete", "path": f"constraint_table[{idx}].violation_scope", "message": "高风险限制缺少 protected_objects/prohibited，建议 LongCat 补全违例范围；本地仅进入灰区，不编造业务对象。"})



def _lint_relation_groups(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    nodes = {str(n.get("id") or ""): n for n in data.get("nodes") or [] if isinstance(n, dict)}
    for idx, group in enumerate(data.get("relation_groups") or []):
        if not isinstance(group, dict):
            continue
        group_type = str(group.get("type") or group.get("group_type") or "all_of")
        if group_type not in {"all_of", "ordered", "strict_order"}:
            continue
        node_ids = [str(x) for x in group.get("nodes") or []]
        required_nodes = [nid for nid in node_ids if (nodes.get(nid) or {}).get("required", True) is not False and str(((nodes.get(nid) or {}).get("activation") or {}).get("mode") or "always") != "optional"]
        old_min = group.get("min_completed")
        if old_min is None:
            continue
        try:
            old_value = int(old_min)
        except Exception:
            continue
        new_value = min(old_value, len(required_nodes))
        if new_value < old_value:
            group["min_completed"] = new_value
            issues.append({"level": "warning", "type": "relation_group_optional_nodes_repaired", "path": f"relation_groups[{idx}].min_completed", "message": f"关系组包含 optional/非必需节点，min_completed 已从 {old_value} 下调到 {new_value}，避免可选后续事项压低主流程。"})

def lint_and_repair_schema(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    repaired = copy.deepcopy(data)
    issues: list[dict[str, Any]] = []
    _lint_nodes(repaired, issues)
    _lint_relations(repaired, issues)
    _lint_relation_groups(repaired, issues)
    _lint_knowledge(repaired, issues)
    _lint_constraints(repaired, issues)
    counts = Counter(str(x.get("type")) for x in issues)
    report = {
        "issue_count": len(issues),
        "counts": dict(counts),
        "issues": issues,
        "summary": "schema_linter 已完成：检查条件分支、宽泛证据组、支持/反驳污染、重复规则和知识对象锚点。",
    }
    repaired.setdefault("metadata", {})["schema_linter"] = {"issue_count": len(issues), "counts": dict(counts)}
    return repaired, report
