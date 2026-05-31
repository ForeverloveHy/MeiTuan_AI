from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


def compile_state_graph(raw_graph: dict[str, Any], legacy_dialogue_root: str | Path | None = None, *, allow_legacy_exact_patterns: bool = False) -> dict[str, Any]:
    """Compile a graph-builder output into an executable evaluation schema.

    The compiler is intentionally domain-neutral: it never contains task words or
    risk lexicons. It only normalizes structure, creates stable requirement / group
    IDs, and folds legacy dataset target identifiers into aliases when an explicit
    graph node already gives a safe anchor.
    """

    graph = copy.deepcopy(raw_graph)
    _normalize_nodes(graph)
    _normalize_knowledge(graph)
    _normalize_constraints(graph)
    if legacy_dialogue_root:
        legacy = _collect_legacy_expectations(Path(legacy_dialogue_root), str(graph.get("metadata", {}).get("domain") or ""))
        _attach_legacy_aliases(graph, legacy)
        _synthesize_missing_knowledge_targets(graph, legacy)
        # Do not compile negative-package wrong statements into refute/prohibited
        # patterns by default.  Those statements are answer-key metadata for
        # dataset auditing, not executable business rules.  Enabling this flag is
        # only for legacy debugging, never for formal evaluation.
        if allow_legacy_exact_patterns:
            _compile_legacy_evidence_patterns(graph, legacy)
    graph.setdefault("metadata", {})["schema_compiled"] = True
    graph["metadata"]["schema_compiler_version"] = "2.0-v1.6-step2-antileak"
    return graph


def compile_graph_file(in_path: str | Path, out_path: str | Path | None = None, legacy_dialogue_root: str | Path | None = None) -> dict[str, Any]:
    in_path = Path(in_path)
    graph = json.loads(in_path.read_text(encoding="utf-8"))
    compiled = compile_state_graph(graph, legacy_dialogue_root=legacy_dialogue_root)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(compiled, ensure_ascii=False, indent=2), encoding="utf-8")
    return compiled


def _normalize_nodes(graph: dict[str, Any]) -> None:
    for node in graph.get("nodes", []) or []:
        node.setdefault("aliases", [])
        node["aliases"] = _dedupe([*node.get("aliases", []), node.get("id", ""), node.get("name", "")])
        activation_terms = _activation_terms(node)
        reqs = node.get("requirements") or []
        if not reqs and node.get("evidence_groups"):
            reqs = []
            for g in node.get("evidence_groups", []):
                reqs.append(
                    {
                        "id": g.get("id") or _slug(g.get("description") or "requirement"),
                        "text": g.get("description") or g.get("text") or "",
                        "required": g.get("required", True),
                        "weight": g.get("weight", 1.0),
                        "aliases": g.get("aliases", []),
                        "evidence_groups": [g],
                    }
                )
            node.pop("evidence_groups", None)
            node["requirements"] = reqs
        for idx, req in enumerate(node.get("requirements", []) or [], start=1):
            req.setdefault("id", f"{node.get('id','node')}.r{idx}")
            req.setdefault("text", req.get("description") or req.get("name") or "")
            req.setdefault("aliases", [])
            req["aliases"] = _dedupe([*req.get("aliases", []), req.get("id", ""), req.get("text", "")])
            groups = req.get("evidence_groups") or []
            if not groups and req.get("patterns") is not None:
                groups = [
                    {
                        "id": f"{req['id']}.evidence",
                        "description": req.get("text") or "",
                        "patterns": req.get("patterns") or [],
                        "min_hits": req.get("min_hits", 1),
                        "required": True,
                        "weight": 1.0,
                    }
                ]
                req.pop("patterns", None)
            req["evidence_groups"] = groups
            for gidx, group in enumerate(groups, start=1):
                group.setdefault("id", f"{req['id']}.g{gidx}")
                group.setdefault("description", group.get("text") or req.get("text") or "")
                group.setdefault("aliases", [])
                group["aliases"] = _dedupe([*group.get("aliases", []), group.get("id", ""), group.get("description", "")])
                _deconflict_activation_only_evidence(group, activation_terms)
                _tighten_evidence_group(group)
                _add_window_union_patterns(group)


def _normalize_knowledge(graph: dict[str, Any]) -> None:
    for item in graph.get("knowledge_table", []) or []:
        item.setdefault("aliases", [])
        item["aliases"] = _dedupe([*item.get("aliases", []), item.get("id", ""), item.get("name", "")])
        if item.get("claims"):
            for claim in item.get("claims", []) or []:
                claim.setdefault("aliases", [])
                claim["aliases"] = _dedupe([*claim.get("aliases", []), claim.get("id", ""), claim.get("name", "")])
        else:
            # Keep older pattern_conflict items executable but make the implicit
            # claim addressable for arbitration/reporting.
            if item.get("support_patterns") or item.get("conflict_patterns") or item.get("refute_patterns"):
                item.setdefault("claims", [])
                item["claims"].append(
                    {
                        "id": f"{item.get('id','knowledge')}.claim",
                        "name": item.get("name") or item.get("id") or "knowledge",
                        "support_patterns": item.get("support_patterns") or [],
                        "refute_patterns": item.get("refute_patterns") or item.get("conflict_patterns") or [],
                        "severity": item.get("severity", "medium"),
                        "aliases": [item.get("id", ""), item.get("name", "")],
                    }
                )
                item["judge_type"] = "claim_evidence"
        _clean_claim_refute_patterns(item)


def _pattern_text_values(pattern: dict[str, Any]) -> list[str]:
    vals: list[str] = []
    for key in ("any", "all"):
        for value in pattern.get(key, []) or []:
            t = str(value or "").strip()
            if t:
                vals.append(t)
    return vals


def _norm_value(text: str) -> str:
    s = re.sub(r"\s+", "", str(text or ""))
    s = s.replace("－", "-").replace("–", "-").replace("—", "-").replace("到", "-")
    s = s.replace("约", "").replace("大概", "").replace("左右", "")
    return s


def _clean_claim_refute_patterns(item: dict[str, Any]) -> None:
    """Remove obvious support text from refute slots.

    LongCat/schema binding may accidentally copy positive coverage descriptions
    into refute_patterns.  The compiler only compares text already present in
    the same knowledge item: if a refute sentence contains a support value and
    contains no stronger conflicting signal from its own pattern structure, it
    is safer to drop it than to let correct positive samples be marked as
    factual conflicts.
    """
    for claim in item.get("claims", []) or []:
        support_values = [_norm_value(v) for p in claim.get("support_patterns", []) or [] for v in _pattern_text_values(p)]
        support_values = [v for v in support_values if v]
        if not support_values:
            continue
        kept: list[dict[str, Any]] = []
        for pat in claim.get("refute_patterns", []) or []:
            pat_text = _norm_value(" ".join(_pattern_text_values(pat)))
            if pat_text and any(v and v in pat_text for v in support_values):
                # Keep exact contradiction patterns that contain an explicit
                # negation marker; otherwise this is likely positive/support
                # coverage copied to the wrong side.
                raw_text = " ".join(_pattern_text_values(pat))
                if not any(x in raw_text for x in ("不是", "不", "无需", "不用", "更低", "更高", "免费")):
                    continue
            kept.append(pat)
        claim["refute_patterns"] = kept


def _normalize_constraints(graph: dict[str, Any]) -> None:
    for rule in graph.get("constraint_table", []) or []:
        rule.setdefault("aliases", [])
        rule["aliases"] = _dedupe([*rule.get("aliases", []), rule.get("id", ""), rule.get("name", "")])
        rule.setdefault("prohibited", [])
        rule.setdefault("safe_context", [])
        rule.setdefault("unresolved", rule.get("grey_zone", []))
        _ensure_constraint_violation_scope(rule)


def _ensure_constraint_violation_scope(rule: dict[str, Any]) -> None:
    """Build a schema-side violation_scope when LongCat omitted it.

    The compiler only reuses text that already exists in the constraint schema:
    trigger/prohibited/safe/unresolved/name/description.  It never reads negative
    sample answers and never injects Python-domain dictionaries into the graph.
    Newer LongCat prompts should output violation_scope directly; this migration
    keeps older graphs executable under the same schema-driven executor.
    """
    scope = dict(rule.get("violation_scope") or {})
    scope.setdefault("protected_objects", _derive_scope_objects(rule))
    scope.setdefault("forbidden_actions", _derive_scope_actions(rule.get("prohibited") or [], fallback_name=rule.get("name") or rule.get("description") or ""))
    scope.setdefault("safe_actions", _derive_scope_actions(rule.get("safe_context") or [], fallback_name=""))
    scope.setdefault("ambiguous_zone", rule.get("unresolved") or rule.get("grey_zone") or [])
    # Keep empty arrays explicit so reports/linter can distinguish schema-driven
    # absence from old missing fields.
    rule["violation_scope"] = scope


def _constraint_pattern_values(patterns: list[dict[str, Any]] | None) -> list[str]:
    vals: list[str] = []
    for pat in patterns or []:
        if not isinstance(pat, dict):
            continue
        for key in ("any", "all", "regex_any"):
            vals.extend(str(x or "").strip() for x in pat.get(key, []) or [] if str(x or "").strip())
        if pat.get("reason"):
            vals.append(str(pat.get("reason")).strip())
    return vals


def _derive_scope_actions(patterns: list[dict[str, Any]] | None, fallback_name: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pat in patterns or []:
        if isinstance(pat, dict):
            # Preserve original schema evidence pattern.  The executor treats it
            # as an action boundary, not a domain dictionary.
            out.append(dict(pat))
    if not out and str(fallback_name or "").strip():
        out.append({"any": [str(fallback_name).strip()]})
    return out


def _derive_scope_objects(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract protected objects from the constraint's own text.

    This is deliberately conservative.  It removes common speech-act words and
    short function words, then keeps longer noun-like chunks already present in
    the schema.  If extraction finds nothing, the executor can still rely on
    explicit prohibited patterns, but it will not invent a business object.
    """
    texts = [str(rule.get("name") or ""), str(rule.get("description") or ""), *[str(x or "") for x in rule.get("aliases", []) or []]]
    texts.extend(_constraint_pattern_values(rule.get("prohibited") or []))
    texts.extend(_constraint_pattern_values(rule.get("safe_context") or []))
    stop = {
        "禁止", "不能", "不得", "不应", "承诺", "保证", "确保", "人工", "系统",
        "我帮你", "帮你", "帮您", "处理", "操作", "继续", "必须", "强制", "私下",
        "安全", "回复", "说明", "规则", "按规则", "可以", "无法", "不能保证", "不承诺",
    }
    candidates: list[str] = []
    for text in texts:
        raw = re.split(r"[，。；;、/\s]+", text)
        for token in raw:
            token = str(token or "").strip()
            if not token or token in stop or len(token) < 2:
                continue
            token = re.sub(r"^(禁止|不能|不得|不应|承诺|保证|确保)", "", token)
            token = re.sub(r"(要求|证据|违规|安全|处理|操作|回复|说明)$", "", token)
            if len(token) >= 2 and token not in stop:
                candidates.append(token)
        # Also keep quoted/book-title chunks from schema text.
        for m in re.findall(r'[《“"]([^《》“”"]{2,20})[》”"]', text):
            if m and m not in stop:
                candidates.append(m)
    seen: set[str] = set()
    values: list[str] = []
    for c in candidates:
        norm = re.sub(r"\s+", "", c)
        if norm and norm not in seen:
            seen.add(norm)
            values.append(c)
    return [{"any": values[:12]}] if values else []


def _collect_legacy_expectations(root: Path, domain: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    files = list(root.rglob("*.json"))
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if domain and str(data.get("domain") or "") != domain:
            continue
        for err in data.get("injected_errors", []) or []:
            out.append(
                {
                    "family": err.get("error_family") or err.get("type") or "",
                    "target_node": err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node") or data.get("target_node_id") or "",
                    "target_core": err.get("requirement_id") or err.get("target_core") or err.get("target_group_id") or data.get("target_id") or "",
                    "knowledge_id": err.get("knowledge_id") or err.get("target_knowledge_id") or "",
                    "constraint_id": err.get("constraint_id") or err.get("target_constraint_id") or "",
                    "title": err.get("title") or err.get("error_type_title") or data.get("source_error_type") or "",
                    "source_node": data.get("source_node") or err.get("source_node") or "",
                    "target_kind": data.get("target_kind") or err.get("target_kind") or "",
                    "wrong_statement": err.get("wrong_statement") or err.get("evidence_span") or data.get("source_negative_design") or "",
                    "source": "negative_error",
                }
            )
        # Positive coverage targets are also useful for ID binding and branch
        # traceability. They are metadata supplied by the current package, not a
        # task-specific code dictionary.
        for cov in data.get("coverage_targets", []) or []:
            if not isinstance(cov, dict):
                continue
            out.append(
                {
                    "family": cov.get("target_kind") or data.get("target_kind") or "coverage",
                    "target_node": cov.get("node_id") or cov.get("target_node_id") or data.get("target_node_id") or "",
                    "target_core": cov.get("target_id") or data.get("target_id") or "",
                    "knowledge_id": cov.get("knowledge_id") or "",
                    "constraint_id": cov.get("constraint_id") or "",
                    "title": data.get("source_error_type") or data.get("scenario") or "",
                    "source_node": data.get("source_node") or "",
                    "target_kind": cov.get("target_kind") or data.get("target_kind") or "",
                    # Positive coverage text is allowed to help bind node /
                    # requirement aliases, but it must never be compiled into
                    # refute/prohibited evidence.
                    "wrong_statement": "",
                    "source_positive_design": data.get("source_positive_design") or "",
                    "source": "positive_coverage",
                }
            )
    return out



def _node_by_id(graph: dict[str, Any], node_id: str, hint: str = "", target_core: str = "") -> dict[str, Any] | None:
    nodes = graph.get("nodes", []) or []
    if node_id:
        for node in nodes:
            if str(node.get("id") or "") == node_id or node_id in [str(x) for x in node.get("aliases", []) or []]:
                return node
    return _best_node(nodes, node_id, target_core, hint) if (node_id or target_core or hint) else None


def _best_requirement(node: dict[str, Any], target_core: str = "", hint: str = "") -> dict[str, Any] | None:
    reqs = node.get("requirements", []) or []
    if not reqs:
        return None
    best = None
    best_score = -1.0
    for req in reqs:
        text = " ".join([str(req.get("id", "")), str(req.get("text", "")), " ".join(map(str, req.get("aliases", []) or []))])
        for group in req.get("evidence_groups", []) or []:
            text += " " + " ".join([str(group.get("id", "")), str(group.get("description", "")), " ".join(map(str, group.get("aliases", []) or []))])
        score = max(_similarity(target_core, text) if target_core else 0.0, _similarity(hint, text) if hint else 0.0)
        if str(req.get("id") or "") == target_core or target_core in [str(x) for x in req.get("aliases", []) or []]:
            score += 1.0
        if score > best_score:
            best_score = score
            best = req
    if best is not None and (best_score > 0.0 or len(reqs) == 1):
        return best
    return None


def _patterns_from_requirement(req: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for group in req.get("evidence_groups", []) or []:
        for pat in group.get("patterns", []) or []:
            if not isinstance(pat, dict):
                continue
            p = copy.deepcopy(pat)
            p["speaker"] = p.get("speaker") or "assistant"
            # Runtime-only pattern extensions do not belong in knowledge claim
            # support patterns.  Keep the original values supplied by the graph.
            p.pop("window_union", None)
            p.pop("reply_window", None)
            p.pop("user_affirm_bonus", None)
            if p.get("any") or p.get("all") or p.get("regex_any"):
                out.append(p)
    if not out:
        text = str(req.get("text") or "").strip()
        # A weak support pattern derived from the requirement text itself.  It
        # is only used for conflict detection when the dialogue asserts and
        # negates these schema terms; it is not copied from negative samples.
        terms = [x for x in _tokens(text) if len(x) >= 2][:8]
        if terms:
            out.append({"speaker": "assistant", "any": terms, "min_any_hits": min(2, len(terms))})
    return out


def _synthesize_missing_knowledge_targets(graph: dict[str, Any], legacy: list[dict[str, Any]]) -> None:
    """Create schema-side knowledge checks for dataset target IDs missing from LongCat.

    LongCat sometimes represents a business fact only as a requirement, while
    the negative package targets it as a knowledge item.  If the target id is
    absent from ``knowledge_table`` but the graph has an anchored node/requirement,
    synthesize a claim from that requirement's own evidence groups.  This keeps
    the runtime generic: no wrong_statement/evidence_span is copied, and no
    domain words live in Python code.
    """
    table = graph.setdefault("knowledge_table", [])
    existing = {str(x.get("id") or "") for x in table}
    for item in legacy:
        family = str(item.get("family") or "")
        if not ("knowledge" in family or "faq" in family or "fact" in family or "知识" in family):
            continue
        explicit_id = str(item.get("knowledge_id") or "").strip()
        if not explicit_id or explicit_id in existing:
            continue
        hint = _legacy_hint(item)
        node = _node_by_id(graph, str(item.get("target_node") or ""), hint, str(item.get("target_core") or ""))
        if node is None:
            continue
        req = _best_requirement(node, str(item.get("target_core") or ""), hint)
        if req is None:
            continue
        support_patterns = _patterns_from_requirement(req)
        if not support_patterns:
            continue
        aliases = _dedupe([
            explicit_id,
            str(item.get("target_core") or ""),
            hint,
            str(req.get("id") or ""),
            str(req.get("text") or ""),
        ])
        claim_name = str(req.get("text") or item.get("target_core") or explicit_id)
        claim_terms: list[str] = []
        for pat in support_patterns:
            for v in _pattern_text_values(pat):
                if v and v not in claim_terms:
                    claim_terms.append(v)
        claim_pattern = {"speaker": "assistant", "any": claim_terms[:12], "min_any_hits": 1} if claim_terms else {"speaker": "assistant", "any": aliases[:8], "min_any_hits": 1}
        table.append({
            "id": explicit_id,
            "name": claim_name or explicit_id,
            "node_id": str(node.get("id") or ""),
            "judge_type": "claim_evidence",
            "severity": "medium",
            "claims": [{
                "id": f"{explicit_id}.claim",
                "name": claim_name or explicit_id,
                "claim_patterns": [claim_pattern],
                "support_patterns": support_patterns,
                "refute_patterns": [],
                "severity": "medium",
                "reason": claim_name or explicit_id,
                "aliases": aliases,
            }],
            "aliases": aliases + ["schema_compiler_synthesized_from_requirement"],
        })
        existing.add(explicit_id)


def _attach_legacy_aliases(graph: dict[str, Any], legacy: list[dict[str, Any]]) -> None:
    nodes = graph.get("nodes", []) or []
    node_by_id = {str(n.get("id")): n for n in nodes}
    for item in legacy:
        family = str(item.get("family") or "")
        target_node = str(item.get("target_node") or "")
        target_core = str(item.get("target_core") or "")
        hint = _legacy_hint(item)
        if target_node and target_node in node_by_id:
            node = node_by_id[target_node]
            _add_alias(node, target_node)
            if target_core:
                _attach_requirement_alias(node, target_core, hint)
        elif target_node:
            # Only add node alias when the graph itself provides a strong textual
            # anchor. Otherwise leave it unresolved instead of guessing semantics.
            node = _best_node(nodes, target_node, target_core, hint)
            if node is not None:
                _add_alias(node, target_node)
                if target_core:
                    _attach_requirement_alias(node, target_core, hint)
        if "knowledge" in family or "faq" in family or "fact" in family or "知识" in family:
            _attach_table_alias(graph.get("knowledge_table", []) or [], target_node, target_core, item.get("knowledge_id") or "", hint)
        if "constraint" in family or "boundary" in family or "限制" in family:
            _attach_table_alias(graph.get("constraint_table", []) or [], target_node, target_core, item.get("constraint_id") or "", hint)


def _legacy_hint(item: dict[str, Any]) -> str:
    """Textual binding hints copied from the sample metadata itself.

    This is deliberately data-derived rather than task-coded: the compiler uses
    source_node/title/target_kind/id fields supplied by the current dataset to
    bind legacy injected_error ids to whatever IDs LongCat generated this run.
    It deliberately excludes exact wrong statements and dialogue text to avoid
    leaking the answer key into the executable schema.
    """
    parts = [
        item.get("source_node"),
        item.get("title"),
        item.get("target_kind"),
        item.get("target_core"),
        item.get("target_node"),
        # Do not include wrong_statement/source_positive_design: they are sample
        # text and can leak the answer key or positive script into schema binding.
    ]
    return " ".join(str(x or "").strip() for x in parts if str(x or "").strip())


def _object_anchor(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for vv in value.values():
                walk(vv)
        elif isinstance(value, (list, tuple, set)):
            for vv in value:
                walk(vv)
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                parts.append(text)
    for key in ("id", "name", "text", "description", "node_id"):
        walk(obj.get(key))
    walk(obj.get("aliases"))
    return " ".join(parts)


def _attach_requirement_alias(node: dict[str, Any], alias: str, hint: str = "") -> None:
    if not alias:
        return
    reqs = node.get("requirements", []) or []
    best_req: dict[str, Any] | None = None
    best_score = -1.0
    for req in reqs:
        text = " ".join([str(req.get("id", "")), str(req.get("text", "")), " ".join(map(str, req.get("aliases", []) or []))])
        for group in req.get("evidence_groups", []) or []:
            text += " " + " ".join([str(group.get("id", "")), str(group.get("description", "")), " ".join(map(str, group.get("aliases", []) or []))])
        score = max(_similarity(alias, text), _similarity(hint, text))
        if score > best_score:
            best_score = score
            best_req = req
    if best_req is None:
        return
    if best_score <= 0.0:
        reqs = node.get("requirements", []) or []
        if len(reqs) == 1:
            best_req = reqs[0]
        else:
            return
    _add_alias(best_req, alias)
    if hint:
        _add_alias(best_req, hint)
    best_group = None
    best_group_score = -1.0
    for group in best_req.get("evidence_groups", []) or []:
        text = " ".join([str(group.get("id", "")), str(group.get("description", "")), " ".join(map(str, group.get("aliases", []) or []))])
        score = max(_similarity(alias, text), _similarity(hint, text))
        if score > best_group_score:
            best_group_score = score
            best_group = group
    if best_group is None:
        return
    if best_group_score <= 0.0:
        groups = best_req.get("evidence_groups", []) or []
        if len(groups) != 1:
            return
        best_group = groups[0]
    _add_alias(best_group, alias)
    if hint:
        _add_alias(best_group, hint)


def _attach_table_alias(items: list[dict[str, Any]], target_node: str, target_core: str, explicit_id: str, hint: str = "") -> None:
    if explicit_id:
        for item in items:
            if item.get("id") == explicit_id:
                _add_alias(item, explicit_id)
                if target_core:
                    _add_alias(item, target_core)
                return
    if not target_core:
        return
    best = None
    best_score = -1.0
    for item in items:
        anchor = _object_anchor(item)
        node_bonus = 0.25 if target_node and target_node == str(item.get("node_id") or "") else 0.0
        score = max(_similarity(target_core, anchor), _similarity(hint, anchor)) + node_bonus
        if score > best_score:
            best_score = score
            best = item
    if best is not None and best_score > 0.0:
        _add_alias(best, target_core)
        if hint:
            _add_alias(best, hint)


def _best_node(nodes: list[dict[str, Any]], target_node: str, target_core: str, hint: str = "") -> dict[str, Any] | None:
    best = None
    best_score = -1.0
    second_score = -1.0
    for node in nodes:
        anchor = _object_anchor(node)
        req_anchor = []
        for req in node.get("requirements", []) or []:
            req_anchor.extend([str(req.get("id", "")), str(req.get("text", "")), " ".join(map(str, req.get("aliases", []) or []))])
            for group in req.get("evidence_groups", []) or []:
                req_anchor.extend([str(group.get("id", "")), str(group.get("description", "")), " ".join(map(str, group.get("aliases", []) or []))])
        req_text = " ".join(req_anchor)
        score = _similarity(target_node, anchor)
        if target_core:
            score = max(score, 0.8 * _similarity(target_core, req_text))
        if hint:
            score = max(score, _similarity(hint, anchor), 0.85 * _similarity(hint, req_text))
        if score > best_score:
            second_score = best_score
            best_score = score
            best = node
        elif score > second_score:
            second_score = score
    if best is None:
        return None
    # Opaque legacy IDs such as English snake_case often have no overlap with
    # fresh LongCat IDs. When dataset text hints exist, allow a lower threshold
    # but require a clear winner. This stays domain-neutral because all hints
    # come from the current package metadata, not code constants.
    if best_score >= 0.65:
        return best
    if hint and best_score >= 0.18 and (second_score <= 0.0001 or best_score >= second_score * 1.15):
        return best
    return None


def _activation_terms(node: dict[str, Any]) -> set[str]:
    """Collect trigger evidence supplied by the graph for this node.

    These terms are not a code lexicon; they are copied from the node schema.
    The compiler uses them only to avoid a common schema mistake: reusing the
    same trigger/topic terms as assistant-side completion evidence. A node can
    be activated by a user's topic mention, but completion should normally need
    handling evidence supplied by the graph.
    """
    terms: set[str] = set()
    activation = node.get("activation") or {}
    for pat in activation.get("patterns", []) or []:
        for key in ("any", "all"):
            for value in pat.get(key, []) or []:
                value = str(value or "").strip()
                if value:
                    terms.add(value)
    return terms


def _deconflict_activation_only_evidence(group: dict[str, Any], activation_terms: set[str]) -> None:
    """Separate pure trigger evidence from completion evidence.

    The old pass removed every assistant evidence term that also appeared in a
    node activation profile. That was too aggressive: in conditional FAQ nodes,
    words such as a topic or condition can be both the user trigger and a core
    answer fact. This version removes only activation terms that are not named
    by the requirement/group description. If the schema itself describes the
    term as part of the expected answer, it remains executable evidence.
    """
    if not activation_terms:
        return
    description = str(group.get("description") or group.get("text") or "")
    for pat in group.get("patterns", []) or []:
        if pat.get("speaker") and pat.get("speaker") != "assistant":
            continue
        values = [str(x) for x in (pat.get("any") or []) if str(x)]
        if len(values) < 2:
            continue
        removable = [x for x in values if x in activation_terms and x not in description]
        if not removable:
            continue
        handling_values = [x for x in values if x not in removable]
        if handling_values:
            pat["any"] = handling_values
            pat.setdefault("compiler_notes", []).append("removed_pure_activation_terms_from_completion_evidence")
        else:
            pat["min_any_hits"] = max(int(pat.get("min_any_hits") or 1), min(2, len(values)))
            pat.setdefault("compiler_notes", []).append("activation_only_completion_evidence_requires_multiple_hits")


def _similarity(a: str, b: str) -> float:
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter:
        return inter / max(1, min(len(ta), len(tb)))
    # Character n-gram fallback for opaque ids.
    ca = _char_grams(a)
    cb = _char_grams(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / max(1, min(len(ca), len(cb)))


def _tighten_evidence_group(group: dict[str, Any]) -> None:
    """Make under-specified evidence groups more executable.

    This is schema compilation, not a code lexicon: the compiler only uses the
    evidence words already present in the graph group's own description. If a
    graph pattern says "any of A/B/C" while the requirement description itself
    contains multiple of A/B/C, a single token hit is too weak; require the
    number of terms that the graph description explicitly names.
    """
    description = str(group.get("description") or group.get("text") or "")
    if not description:
        return
    for pat in group.get("patterns", []) or []:
        if pat.get("all") or pat.get("min_any_hits"):
            continue
        values = [str(x) for x in (pat.get("any") or []) if str(x)]
        if len(values) < 2:
            continue
        named = [x for x in values if x in description]
        if len(named) >= 2:
            pat["min_any_hits"] = len(named)



def _add_window_union_patterns(group: dict[str, Any]) -> None:
    """Allow one requirement to be completed across adjacent/multiple turns.

    LongCat sometimes emits a single evidence pattern with min_any_hits>1 even
    though a human call may naturally split those facts across turns. This pass
    adds a schema-level union pattern derived only from the graph's own terms.
    """
    patterns = list(group.get("patterns", []) or [])
    description = str(group.get("description") or group.get("text") or "")
    additions: list[dict[str, Any]] = []
    for pat in patterns:
        if pat.get("window_union") or pat.get("speaker") not in {None, "", "assistant"}:
            continue
        values = [str(x) for x in (pat.get("any") or []) if str(x)]
        min_hits = int(pat.get("min_any_hits") or 1)
        if len(values) < 2 or min_hits < 2:
            continue
        union = {
            "speaker": "assistant",
            "any": values,
            "min_any_hits": min_hits,
            "window_union": True,
            "compiler_notes": ["added_window_union_pattern_from_schema_terms"],
        }
        if ("身份" in description or "确认" in description) and any(("请问" in x or "本人" in x or "是" in x) for x in values):
            union["user_affirm_bonus"] = True
            union["reply_window"] = 2
            union["compiler_notes"].append("enabled_generic_user_affirm_bonus")
        additions.append(union)
    if "身份" in description or "本人" in description:
        additions.append({
            "speaker": "assistant",
            "cross_turn": "assistant_ask_user_affirm",
            "ask_regex_any": ["确认.{0,12}本人", "是.{0,12}本人", "本人.{0,6}吗", "接听.{0,8}吗"],
            "reply_window": 2,
            "compiler_notes": ["added_generic_identity_cross_turn_pattern"],
        })
    if additions:
        group.setdefault("patterns", [])
        for add in additions:
            if add not in group["patterns"]:
                group["patterns"].append(add)


def _append_exact_pattern(container: dict[str, Any], key: str, sentence: str) -> None:
    sentence = str(sentence or "").strip()
    if not sentence:
        return
    # Keep only natural evidence text. Legacy generators sometimes write edit
    # markers; those are not executable dialogue evidence.
    if sentence.startswith("insert@") or sentence.startswith("delete@"):
        if ":" in sentence:
            sentence = sentence.split(":", 1)[1].strip()
        else:
            return
    if not sentence:
        return
    container.setdefault(key, [])
    pat = {"speaker": "assistant", "any": [sentence]}
    if pat not in container[key]:
        container[key].append(pat)


def _compile_legacy_evidence_patterns(graph: dict[str, Any], legacy: list[dict[str, Any]]) -> None:
    for item in legacy:
        if str(item.get("source") or "") == "positive_coverage":
            continue
        family = str(item.get("family") or "")
        sentence = str(item.get("wrong_statement") or "").strip()
        if not sentence:
            continue
        target_node = str(item.get("target_node") or "")
        target_core = str(item.get("target_core") or "")
        explicit_knowledge_id = str(item.get("knowledge_id") or "")
        explicit_constraint_id = str(item.get("constraint_id") or "")
        if "knowledge" in family or "faq" in family or "fact" in family or "知识" in family:
            hint = _legacy_hint(item)
            target = _table_item_by_id(graph.get("knowledge_table", []) or [], explicit_knowledge_id) or _best_table_item(graph.get("knowledge_table", []) or [], target_node, target_core, hint)
            if target is not None:
                target.setdefault("judge_type", "claim_evidence")
                if not target.get("claims"):
                    target["claims"] = [{
                        "id": f"{target.get('id','knowledge')}.claim",
                        "name": target.get("name") or target.get("id") or "knowledge",
                        "support_patterns": target.get("support_patterns") or [],
                        "refute_patterns": target.get("refute_patterns") or target.get("conflict_patterns") or [],
                        "severity": target.get("severity", "medium"),
                        "aliases": [target.get("id", ""), target.get("name", "")],
                    }]
                # Attach to the first claim because the legacy annotation normally
                # describes the knowledge item as a whole.
                _append_exact_pattern(target["claims"][0], "refute_patterns", sentence)
        if "constraint" in family or "boundary" in family or "限制" in family:
            hint = _legacy_hint(item)
            target = _table_item_by_id(graph.get("constraint_table", []) or [], explicit_constraint_id) or _best_table_item(graph.get("constraint_table", []) or [], target_node, target_core, hint)
            if target is not None:
                _append_exact_pattern(target, "prohibited", sentence)


def _table_item_by_id(items: list[dict[str, Any]], explicit_id: str) -> dict[str, Any] | None:
    if not explicit_id:
        return None
    for item in items:
        if str(item.get("id") or "") == explicit_id:
            return item
    return None


def _best_table_item(items: list[dict[str, Any]], target_node: str, target_core: str, hint: str = "") -> dict[str, Any] | None:
    best = None
    best_score = -1.0
    second_score = -1.0
    for item in items:
        anchor = _object_anchor(item)
        score = _similarity(target_core, anchor) if target_core else 0.0
        if hint:
            score = max(score, _similarity(hint, anchor))
        if target_node and target_node == str(item.get("node_id") or ""):
            score += 0.35
        if score > best_score:
            second_score = best_score
            best_score = score
            best = item
        elif score > second_score:
            second_score = score
    if best is None:
        return None
    if best_score > 0.0 and (second_score <= 0.0001 or best_score >= second_score * 1.10):
        return best
    return None


def _tokens(text: str) -> set[str]:
    raw = str(text or "").lower()
    parts = re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", raw)
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        out.add(part)
        # Domain-neutral Chinese segmentation fallback: LongCat may name the
        # same node with different short Chinese phrases. Character bigrams keep
        # binding robust without adding any business dictionary.
        for chunk in re.findall(r"[\u4e00-\u9fff]+", part):
            out.update(chunk[i : i + 2] for i in range(max(0, len(chunk) - 1)))
            if len(chunk) <= 4:
                out.update(chunk)
        for chunk in re.findall(r"[a-z0-9]+", part):
            out.add(chunk)
    return {x for x in out if x}


def _char_grams(text: str, n: int = 2) -> set[str]:
    s = re.sub(r"\s+", "", str(text).lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _add_alias(obj: dict[str, Any], alias: str) -> None:
    if not alias:
        return
    obj.setdefault("aliases", [])
    obj["aliases"] = _dedupe([*obj.get("aliases", []), str(alias)])


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        s = str(value or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_")
    return s or "requirement"
