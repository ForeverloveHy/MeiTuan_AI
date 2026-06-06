from __future__ import annotations

"""One-graph/two-table schema assembly and atom-level element passes.

This module keeps the new SCEG build contract explicit:

1. graph_core pass: only nodes / atoms / edges / relation groups.
2. knowledge_table pass: only fact rows bound to graph atoms.
3. constraint_tables pass: hard and soft tables stay separate.
4. local atom transport: all executable objects become atom packets with stable atom_id.
5. element passes: LLM sees only atom_id + atom_text + atom_source, then returns
   primary elements and secondary pools keyed by atom_id.

The module is schema-only.  It does not inspect dialogue labels, negative-pack
answers, evidence spans, wrong statements or sample metadata.
"""

import copy
import os
import hashlib
import json
import re
from typing import Any

from .role_aware_element_hints import build_role_aware_element_hints

from .hard_constraint_backfill import ensure_hard_constraints_when_required


GRAPH_CORE_ALLOWED = {"graph_id", "name", "metadata", "nodes", "edges", "relation_groups", "terminal_policies"}
FORBIDDEN_CORE_KEYS = {"knowledge_table", "constraint_table", "hard_constraint_table", "soft_constraint_table", "evidence_groups"}


def stable_anchor(kind: str, *parts: Any) -> str:
    # Internal compatibility key.  Do not expose this concept in prompts.
    raw = "|".join(str(p or "") for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    safe_parts = [re.sub(r"[^A-Za-z0-9_\-:.]+", "_", str(p or "")).strip("_")[:48] for p in parts if str(p or "").strip()]
    stem = ":".join([kind] + safe_parts) if safe_parts else kind
    return f"{stem}:{digest}"


def stable_atom_id(atom_source: str, *parts: Any) -> str:
    """Public atom id used by LLM element passes."""
    safe_parts = [re.sub(r"[^A-Za-z0-9_\-:.]+", "_", str(p or "")).strip("_")[:64] for p in parts if str(p or "").strip()]
    if not safe_parts:
        safe_parts = [hashlib.sha1(atom_source.encode("utf-8")).hexdigest()[:8]]
    return ":".join([str(atom_source or "atom")] + safe_parts)


def ensure_metadata(graph: dict[str, Any]) -> dict[str, Any]:
    meta = graph.setdefault("metadata", {})
    if not isinstance(meta, dict):
        graph["metadata"] = meta = {}
    return meta


def _graph_node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "").strip()


def strip_graph_core(raw: dict[str, Any]) -> dict[str, Any]:
    """Return graph-core-only schema and remove table fields if LLM leaked them."""
    if not isinstance(raw, dict):
        raise ValueError("graph_core output must be a JSON object")
    core = {k: copy.deepcopy(v) for k, v in raw.items() if k in GRAPH_CORE_ALLOWED}
    core.setdefault("graph_id", raw.get("graph_id") or "sceg_graph_core")
    core.setdefault("name", raw.get("name") or "SCEG 主状态图")
    core.setdefault("nodes", [])
    core.setdefault("edges", [])
    core.setdefault("relation_groups", [])
    core.setdefault("terminal_policies", {})
    meta = ensure_metadata(core)
    meta["schema_generation_mode"] = "one_graph_two_tables_atom_element_only"
    meta["phase1"] = "graph_core_only"
    leaked = [k for k in FORBIDDEN_CORE_KEYS if k in raw]
    if leaked:
        meta.setdefault("core_generation_warnings", []).append({
            "type": "forbidden_table_keys_removed",
            "keys": leaked,
            "message": "graph_core pass must not output knowledge/constraint/evidence fields; local assembler removed them.",
        })
    for idx, node in enumerate(core.get("nodes") or [], start=1):
        if not isinstance(node, dict):
            continue
        nid = _graph_node_id(node) or f"node_{idx}"
        node.setdefault("id", nid)
        node.setdefault("node_id", nid)
        node.pop("evidence_groups", None)
        # Old requirements are compatibility-only.  Keep absent by default in new core.
        for atom_idx, atom in enumerate(node.get("atoms") or [], start=1):
            if not isinstance(atom, dict):
                continue
            aid = str(atom.get("id") or atom.get("atom_id") or f"{nid}_atom_{atom_idx:02d}")
            atom.setdefault("id", aid)
            atom.setdefault("atom_id", aid)
        if not node.get("requirements"):
            node["requirements"] = []
    return core


def extract_table(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    val = raw.get(key)
    if isinstance(val, list):
        return [copy.deepcopy(x) for x in val if isinstance(x, dict)]
    if isinstance(raw.get("tables"), dict) and isinstance(raw["tables"].get(key), list):
        return [copy.deepcopy(x) for x in raw["tables"][key] if isinstance(x, dict)]
    return []


def _flatten_parent_atoms(items: list[dict[str, Any]], *, parent_key: str, row_prefix: str) -> list[dict[str, Any]]:
    """Flatten prompt-level parent -> atoms rows into executable runtime rows."""
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        atoms = item.get("atoms")
        if not isinstance(atoms, list):
            row = copy.deepcopy(item)
            row.setdefault("id", row.get(parent_key) or f"{row_prefix}_{idx}")
            rows.append(row)
            continue
        parent = {k: copy.deepcopy(v) for k, v in item.items() if k != "atoms"}
        parent_id = str(parent.get(parent_key) or parent.get("id") or f"{row_prefix}_{idx}")
        for j, atom in enumerate(atoms, start=1):
            if not isinstance(atom, dict):
                continue
            row = copy.deepcopy(parent)
            row.update(copy.deepcopy(atom))
            row.setdefault(parent_key, parent_id)
            row["id"] = str(atom.get("id") or atom.get("atom_id") or f"{parent_id}_atom_{j:02d}")
            row.setdefault("name", atom.get("name") or parent.get("name") or row["id"])
            if "severity" not in row and parent.get("severity"):
                row["severity"] = parent.get("severity")
            rows.append(row)
    return rows


def _node_ids(graph: dict[str, Any]) -> set[str]:
    return {_graph_node_id(n) for n in graph.get("nodes") or [] if isinstance(n, dict) and _graph_node_id(n)}


def _atom_ids_by_node(graph: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = _graph_node_id(node)
        if not nid:
            continue
        out[nid] = {str(a.get("id") or a.get("atom_id")) for a in node.get("atoms") or [] if isinstance(a, dict) and (a.get("id") or a.get("atom_id"))}
    return out


def merge_knowledge_table(graph: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(graph)
    table = _flatten_parent_atoms(extract_table(raw, "knowledge_table"), parent_key="knowledge_id", row_prefix="k")
    node_ids = _node_ids(merged)
    atom_by_node = _atom_ids_by_node(merged)
    clean: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, item in enumerate(table, start=1):
        item = copy.deepcopy(item)
        item.setdefault("id", item.get("atom_id") or item.get("knowledge_id") or f"k_{idx}")
        nid = str(item.get("node_id") or "")
        if nid and nid not in node_ids:
            warnings.append({"type": "knowledge_bad_node_binding", "id": item.get("id"), "node_id": nid})
            item.pop("node_id", None)
        raw_atom_ids = item.get("atom_ids") or []
        atom_ids = [str(x) for x in raw_atom_ids if str(x or "").strip()]
        if nid and atom_ids:
            allowed = atom_by_node.get(nid, set())
            atom_ids = [x for x in atom_ids if x in allowed]
            if atom_ids:
                item["atom_ids"] = atom_ids
            else:
                item.pop("atom_ids", None)
        item.setdefault("judge_type", "element_fact_verification")
        item.setdefault("severity", "medium")
        clean.append(item)
    merged["knowledge_table"] = clean
    merged = normalize_executable_groups(merged)
    meta = ensure_metadata(merged)
    meta["phase2"] = "knowledge_table_only"
    if warnings:
        meta.setdefault("table_binding_warnings", []).extend(warnings)
    return merged


def merge_constraint_tables(graph: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(graph)
    hard = _flatten_parent_atoms(extract_table(raw, "hard_constraint_table"), parent_key="constraint_id", row_prefix="hc")
    soft = _flatten_parent_atoms(extract_table(raw, "soft_constraint_table"), parent_key="constraint_id", row_prefix="sc")
    # If model leaked a merged constraint_table, split by enforcement but do not
    # store a merged table.  Hard/soft tables are the authoritative schema.
    leaked = extract_table(raw, "constraint_table")
    for item in leaked:
        if str(item.get("enforcement") or "").lower() == "soft" or str(item.get("constraint_kind") or "").lower() == "fuzzy_quality":
            soft.append(item)
        else:
            hard.append(item)
    def dedupe(items: list[dict[str, Any]], default_enforcement: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx, item in enumerate(items, start=1):
            item = copy.deepcopy(item)
            item.setdefault("id", f"{default_enforcement[:1]}c_{idx}")
            item["enforcement"] = default_enforcement
            key = str(item.get("id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out
    merged["hard_constraint_table"] = dedupe(hard, "hard")
    merged["soft_constraint_table"] = dedupe(soft, "soft")
    merged.pop("constraint_table", None)
    merged = normalize_executable_groups(merged)
    meta = ensure_metadata(merged)
    meta["phase3"] = "hard_soft_constraint_tables_only"
    meta["constraint_table_policy"] = "hard_constraint_table_and_soft_constraint_table_are_authoritative; merged constraint_table is not written"
    return merged


def _copy_element_list(value: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(x) for x in value if isinstance(x, dict)] if isinstance(value, list) else []



# Generic constraint-table sanity limits.  They are intentionally domain-neutral:
# the goal is to prevent a supplement prompt from exploding a concise negative
# object table into dozens of invented or soft-quality rows.
_DEFAULT_MAX_HARD_CONSTRAINTS = 10
_DEFAULT_MAX_SOFT_CONSTRAINTS = 8
_HARD_ACTION_RE = re.compile(r"(禁止|不能|不得|不允许|不要|不可|承诺|保证|确保|代为|代替|帮.*操作|人工.*(改|调|干预)|干预|越权|赠送|发放|减免|返现|强迫|继续推进)")
_SOFT_SIGNAL_RE = re.compile(r"(自然|口语|礼貌|简洁|冗长|重复|发言机会|沟通风格|语气|清晰|流畅|正式|书面)")
_BOUNDARY_RE = re.compile(r"(系统|平台|规则|结果|权限|配置|页面|金额|收费|奖励|补偿|账号|号码|入口|记录|安全|驾驶|自助|人工|折扣|优惠|券|准入|排序|资源)")
_GENERIC_STOP_RE = re.compile(r"(客服|用户|客户|问题|电话|通话|当前|稍后|可以|需要|进行|说明|告知|提醒|确认|询问|回复|回答|处理)")
_LOW_VALUE_PROMISE_RE = re.compile(r"(通话时长|通话结束条件|拨打电话|知情状态|转达结果|反馈结果|查看消息)")


def _env_int_local(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        val = int(str(os.getenv(name, default)).strip())
    except Exception:
        val = default
    return max(minimum, val)


def _walk_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_walk_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_walk_text(v) for v in value)
    return ""


def _simple_tokens(text: str) -> set[str]:
    # Chinese first: keep 2-8 char chunks after removing generic stop words;
    # also keep ASCII tokens for App/Web/SaaS-like fixed identifiers.
    text = _GENERIC_STOP_RE.sub(" ", str(text or ""))
    toks = set(re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,20}", text))
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
        seg = m.group(0)
        for n in range(2, min(6, len(seg)) + 1):
            for i in range(0, len(seg) - n + 1):
                piece = seg[i:i+n]
                if not _GENERIC_STOP_RE.fullmatch(piece):
                    toks.add(piece)
    return toks


def _looks_soft_quality_rule(item: dict[str, Any]) -> bool:
    blob = _walk_text({k: item.get(k) for k in ("id", "constraint_id", "name", "text", "description", "quality_dimension", "metric", "score_effect", "constraint_kind")})
    kind = str(item.get("constraint_kind") or "").lower()
    has_quality_fields = any(item.get(k) not in (None, "", [], {}) for k in ("quality_dimension", "metric", "score_effect"))
    if kind == "fuzzy_quality" and has_quality_fields:
        # Explicit lexical bans can still be hard if the instruction really says
        # not to say a phrase.  General style/length/naturalness rules belong to soft.
        if re.search(r"(禁用|禁止|不能说|不得说|不说|不要说)", blob) and not _SOFT_SIGNAL_RE.search(blob.replace("语气词", "")):
            return False
        return True
    return False


def _has_executable_negative(item: dict[str, Any]) -> bool:
    neg = item.get("negative_groups") or item.get("negative_element_groups") or item.get("negative_elements") or item.get("negative_object")
    return bool(_walk_text(neg).strip())


def _constraint_priority(item: dict[str, Any], instruction_tokens: set[str]) -> tuple[int, int, str]:
    blob = _walk_text(item)
    score = 0
    if _has_executable_negative(item):
        score += 5
    if _HARD_ACTION_RE.search(blob):
        score += 4
    if _BOUNDARY_RE.search(blob):
        score += 2
    item_tokens = _simple_tokens(blob)
    overlap = len(item_tokens & instruction_tokens)
    score += min(overlap, 6)
    if _looks_soft_quality_rule(item):
        score -= 8
    if str(item.get("severity") or "").lower() == "low":
        score -= 2
    if len(blob) > 1500:
        score -= 1
    return (score, overlap, str(item.get("id") or item.get("constraint_id") or item.get("name") or ""))


def _dedupe_by_semantics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        clone = copy.deepcopy(item)
        cid = str(clone.get("id") or clone.get("constraint_id") or f"constraint_{idx}")
        name = str(clone.get("name") or "")
        neg = _walk_text(clone.get("negative_groups") or clone.get("negative_elements") or clone.get("negative_object"))
        key_text = name + " " + neg
        key = "|".join(sorted(list(_simple_tokens(key_text)))[:16]) or cid
        if key in seen:
            continue
        seen.add(key)
        clone.setdefault("id", cid)
        clone.setdefault("constraint_id", cid)
        out.append(clone)
    return out




def _dedupe_hard_clusters(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_cluster: set[str] = set()
    for item in items:
        # Use executable semantics only.  source_quote may contain adjacent
        # unrelated boundary clauses and should not merge independent hard rows.
        blob = _walk_text({k: v for k, v in item.items() if k not in {"source_quote"}})
        cluster = ""
        if re.search(r"(好的|哈哈|嘿嘿|嘻嘻|禁用表达|禁用语气词|禁止使用明确禁用)", blob):
            cluster = "explicit_banned_phrase"
        elif re.search(r"(开车|驾驶|骑行|安全状态|不方便接听|不方便接电话).*(继续|推进|说明|追问|施压|稍后|挂断)", blob):
            cluster = "safety_stop"
        elif re.search(r"(职责范围|权限范围|职责范围外|超出职责).*(擅自|编造|越权|同事确认|确认后.*回)", blob):
            cluster = "out_of_scope_boundary"
        elif re.search(r"承诺.*(操作|配置|设置|保存|开通|勾选|编辑|启用|验证|添加|联系).*(成功|完成|生效|通过|可用)", blob):
            cluster = "promise_operation_result"
        elif re.search(r"(承诺|保证|确保|一定|肯定|绝对).*(金额|收费|优惠|折扣|减免|返现|券|权益|福利)", blob) or re.search(r"(金额|收费|优惠|折扣|减免|返现|券|权益|福利).*(承诺|保证|确保|一定|肯定|绝对)", blob):
            cluster = "promise_benefit"
        elif re.search(r"承诺.*(后台|权限|代为|代替|帮.*配置|帮.*操作)", blob):
            cluster = "promise_manual_authority"
        if cluster and cluster in seen_cluster:
            dropped.append({"id": item.get("id") or item.get("constraint_id"), "reason": "duplicate_hard_cluster:" + cluster})
            continue
        if cluster:
            seen_cluster.add(cluster)
        out.append(item)
    return out, dropped

def sanitize_constraint_tables(raw: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    """Domain-neutral postprocess for LLM constraint outputs.

    This only synthesizes hard rows from explicit negative/boundary language in
    the original instruction or from soft rows that clearly contain lexical bans.
    It also prevents common LLM failure modes: soft-quality rows leaking into
    hard_constraint_table, supplement-stage explosion, duplicate rows, and hard
    rows without executable negative objects.
    """
    if not isinstance(raw, dict):
        return {"hard_constraint_table": [], "soft_constraint_table": []}
    max_hard = _env_int_local("SCEG_MAX_HARD_CONSTRAINTS", _DEFAULT_MAX_HARD_CONSTRAINTS, minimum=1)
    max_soft = _env_int_local("SCEG_MAX_SOFT_CONSTRAINTS", _DEFAULT_MAX_SOFT_CONSTRAINTS, minimum=0)
    instruction_tokens = _simple_tokens(instruction or raw.get("original_complex_instruction") or "")
    hard = [copy.deepcopy(x) for x in extract_table(raw, "hard_constraint_table") if isinstance(x, dict)]
    soft = [copy.deepcopy(x) for x in extract_table(raw, "soft_constraint_table") if isinstance(x, dict)]
    for item in extract_table(raw, "constraint_table"):
        if not isinstance(item, dict):
            continue
        if str(item.get("enforcement") or "").lower() == "soft" or _looks_soft_quality_rule(item):
            soft.append(copy.deepcopy(item))
        else:
            hard.append(copy.deepcopy(item))

    # Third/fourth repair path: explicit hard candidates are first extracted as
    # a tiny candidate table, then deterministically converted to formal hard
    # rows.  This also promotes soft rows that contain concrete lexical bans.
    repaired_raw = {"hard_constraint_table": hard, "soft_constraint_table": soft, "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}}
    repaired_raw = ensure_hard_constraints_when_required(repaired_raw, instruction or "", max_new=max_hard)
    hard = _flatten_parent_atoms(
        [copy.deepcopy(x) for x in repaired_raw.get("hard_constraint_table") or [] if isinstance(x, dict)],
        parent_key="constraint_id",
        row_prefix="hc",
    )
    soft = [copy.deepcopy(x) for x in repaired_raw.get("soft_constraint_table") or [] if isinstance(x, dict)]

    kept_hard: list[dict[str, Any]] = []
    moved_soft: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in hard:
        blob = _walk_text(item)
        if _looks_soft_quality_rule(item):
            clone = copy.deepcopy(item)
            clone["enforcement"] = "soft"
            clone["constraint_kind"] = "fuzzy_quality"
            moved_soft.append(clone)
            continue
        if _LOW_VALUE_PROMISE_RE.search(blob) and not _LOW_VALUE_PROMISE_RE.search(str(instruction or "")):
            dropped.append({"id": item.get("id") or item.get("constraint_id"), "reason": "generic_process_promise_not_in_instruction"})
            continue
        if not _has_executable_negative(item):
            dropped.append({"id": item.get("id") or item.get("constraint_id"), "reason": "missing_negative_groups"})
            continue
        item.setdefault("enforcement", "hard")
        item["constraint_kind"] = "semantic_object"
        kept_hard.append(item)

    kept_hard = _dedupe_by_semantics(kept_hard)
    kept_hard, cluster_dropped = _dedupe_hard_clusters(kept_hard)
    dropped.extend(cluster_dropped)
    if len(kept_hard) > max_hard:
        # Preserve model order after removing soft-quality leaks and duplicates.
        # In practice LLM places the most instruction-salient hard rules
        # earlier, while later rows are often generic “不能承诺 X 成功” expansion.
        dropped.extend({"id": kept_hard[i].get("id") or kept_hard[i].get("constraint_id"), "reason": "over_limit"} for i in range(max_hard, len(kept_hard)))
        kept_hard = kept_hard[:max_hard]

    soft = _dedupe_by_semantics(soft + moved_soft)
    for item in soft:
        item.setdefault("enforcement", "soft")
        item["constraint_kind"] = "fuzzy_quality"
        # soft table is not a negative object table.
        item.pop("negative_groups", None)
        item.pop("safe_groups", None)
        item.pop("trigger_groups", None)
    if max_soft and len(soft) > max_soft:
        soft = soft[:max_soft]

    out = copy.deepcopy(raw)
    out["hard_constraint_table"] = kept_hard
    out["soft_constraint_table"] = soft
    out.pop("constraint_table", None)
    meta = out.setdefault("metadata", {}) if isinstance(out.get("metadata"), dict) else {}
    out["metadata"] = meta
    meta["constraint_sanitize"] = {
        "max_hard": max_hard,
        "max_soft": max_soft,
        "hard_before": len(hard),
        "hard_after": len(kept_hard),
        "soft_after": len(soft),
        "moved_hard_to_soft": len(moved_soft),
        "dropped_count": len(dropped),
        "dropped_sample": dropped[:20],
    }
    if isinstance(repaired_raw.get("metadata"), dict) and repaired_raw["metadata"].get("hard_candidate_backfill"):
        meta["hard_candidate_backfill"] = repaired_raw["metadata"]["hard_candidate_backfill"]
    return out


def merge_constraint_supplement(base: dict[str, Any], supplement: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    """Merge a second-pass constraint supplement without letting it replace the
    whole table with an exploded output.  Supports both patch-style and old full
    table outputs.
    """
    if not isinstance(base, dict):
        base = {"hard_constraint_table": [], "soft_constraint_table": []}
    if not isinstance(supplement, dict):
        return sanitize_constraint_tables(base, instruction)
    if any(k in supplement for k in ("add_hard_constraint_table", "add_soft_constraint_table", "add_hard_constraints", "add_soft_constraints")):
        merged = copy.deepcopy(base)
        merged.setdefault("hard_constraint_table", [])
        merged.setdefault("soft_constraint_table", [])
        merged["hard_constraint_table"] = list(merged.get("hard_constraint_table") or []) + list(supplement.get("add_hard_constraint_table") or supplement.get("add_hard_constraints") or [])
        merged["soft_constraint_table"] = list(merged.get("soft_constraint_table") or []) + list(supplement.get("add_soft_constraint_table") or supplement.get("add_soft_constraints") or [])
    else:
        # Old prompt variants may return full corrected tables.  Treat them as a
        # candidate pool, not as an authoritative replacement.
        merged = {
            "hard_constraint_table": list(base.get("hard_constraint_table") or []) + list(supplement.get("hard_constraint_table") or []),
            "soft_constraint_table": list(base.get("soft_constraint_table") or []) + list(supplement.get("soft_constraint_table") or []),
        }
    return sanitize_constraint_tables(merged, instruction)


def assign_element_anchor_ids(graph: dict[str, Any]) -> dict[str, Any]:
    """Add stable element_anchor_id fields without changing public ids."""
    merged = copy.deepcopy(graph)
    for node in merged.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = _graph_node_id(node) or "node"
        node.setdefault("id", nid)
        node.setdefault("node_id", nid)
        if isinstance(node.get("activation"), dict):
            node["activation"].setdefault("element_anchor_id", stable_anchor("activation", nid))
        for atom in node.get("atoms") or []:
            if isinstance(atom, dict):
                atom.setdefault("element_anchor_id", stable_anchor("node_atom", nid, atom.get("id") or atom.get("atom_id") or atom.get("name") or atom.get("text")))
                atom.setdefault("source_kind", "node_atom")
    for table_name, kind in (("knowledge_table", "knowledge"), ("hard_constraint_table", "hard_constraint"), ("soft_constraint_table", "soft_constraint")):
        for item in merged.get(table_name) or []:
            if isinstance(item, dict):
                item.setdefault("element_anchor_id", stable_anchor(kind, item.get("id") or item.get("name")))
                item.setdefault("source_kind", kind)
    return merged


def _short_atom_text(*values: Any, limit: int = 96) -> str:
    text = " ".join(str(v or "").strip() for v in values if str(v or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def build_atom_transport(graph: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    """Build the local atom transport layer used by element passes.

    The transport layer is intentionally tiny.  It exposes no graph/table body
    and no internal anchor concept.  LLM receives only atom_id, atom_source,
    parent_id, atom_name, atom_text, requested output slots, and a compact
    role-aware derivation mode.  User utterance variation is generated in the
    secondary expansion stage, not inside transport.
    """
    graph = assign_element_anchor_ids(graph)
    entries: list[dict[str, Any]] = []

    def add_entry(*, atom_source: str, atom_id: str, parent_id: Any = "", atom_name: Any = "", atom_text: Any = "", requested_slots: list[str] | None = None, extra: dict[str, Any] | None = None) -> None:
        atom_text_short = _short_atom_text(atom_text, atom_name)
        slots = requested_slots or ["element_groups"]
        payload = {
            "atom_id": atom_id,
            "atom_source": atom_source,
            "source_kind": atom_source,
            "parent_id": str(parent_id or ""),
            "atom_name": str(atom_name or ""),
            "atom_text": atom_text_short,
            "requested_slots": slots,
            "role_aware_element_hints": build_role_aware_element_hints(atom_source, atom_text_short, slots),
        }
        if extra:
            payload.update({k: copy.deepcopy(v) for k, v in extra.items() if v not in (None, [], {})})
        entries.append(payload)

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = _graph_node_id(node)
        activation = node.get("activation") if isinstance(node.get("activation"), dict) else {}
        mode = str(activation.get("mode") or "always")
        if mode in {"condition", "user_triggered"}:
            add_entry(
                atom_source="activation",
                atom_id=stable_atom_id("activation", nid),
                parent_id=nid,
                atom_name=str(node.get("name") or nid) + " 触发条件",
                atom_text=activation.get("trigger_hint") or activation.get("description") or activation.get("hint") or node.get("name"),
                requested_slots=["trigger_groups"],
                extra={"mode": mode, "trigger_groups": copy.deepcopy(activation.get("trigger_groups") or activation.get("element_groups") or [])},
            )
        for atom in node.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            local_id = str(atom.get("id") or atom.get("atom_id") or atom.get("name") or "atom")
            add_entry(
                atom_source="node_atom",
                atom_id=stable_atom_id("node_atom", nid, local_id),
                parent_id=nid,
                atom_name=atom.get("name"),
                atom_text=atom.get("text") or atom.get("name"),
                requested_slots=["element_groups"],
                extra={"required": atom.get("required", True), "element_groups": copy.deepcopy(atom.get("element_groups") or [])},
            )
    for item in graph.get("knowledge_table") or []:
        if not isinstance(item, dict):
            continue
        local_id = str(item.get("id") or item.get("atom_id") or item.get("knowledge_id") or item.get("name") or "knowledge")
        add_entry(
            atom_source="knowledge",
            atom_id=stable_atom_id("knowledge", local_id),
            parent_id=item.get("knowledge_id") or item.get("id"),
            atom_name=item.get("name"),
            atom_text=item.get("text") or item.get("description") or item.get("name"),
            requested_slots=["selector_groups", "correct_groups"],
            extra={
                "value_check": copy.deepcopy(item.get("value_check") or {}),
                "selector_groups": copy.deepcopy(item.get("selector_groups") or item.get("selector_element_groups") or []),
                "correct_groups": copy.deepcopy(item.get("correct_groups") or item.get("correct_element_groups") or item.get("element_groups") or []),
                "wrong_groups": copy.deepcopy(item.get("wrong_groups") or item.get("wrong_element_groups") or []),
            },
        )
    for item in graph.get("hard_constraint_table") or []:
        if not isinstance(item, dict):
            continue
        local_id = str(item.get("id") or item.get("atom_id") or item.get("constraint_id") or item.get("name") or "hard_constraint")
        add_entry(
            atom_source="hard_constraint",
            atom_id=stable_atom_id("hard_constraint", local_id),
            parent_id=item.get("constraint_id") or item.get("id"),
            atom_name=item.get("name"),
            atom_text=item.get("text") or item.get("description") or item.get("name"),
            requested_slots=["trigger_groups", "negative_groups", "safe_groups"],
            extra={
                "trigger_groups": copy.deepcopy(item.get("trigger_groups") or []),
                "negative_groups": copy.deepcopy(item.get("negative_groups") or item.get("negative_element_groups") or []),
                "safe_groups": copy.deepcopy(item.get("safe_groups") or item.get("positive_element_groups") or []),
            },
        )
    for item in graph.get("soft_constraint_table") or []:
        if not isinstance(item, dict):
            continue
        local_id = str(item.get("id") or item.get("atom_id") or item.get("constraint_id") or item.get("name") or "soft_constraint")
        add_entry(
            atom_source="soft_constraint",
            atom_id=stable_atom_id("soft_constraint", local_id),
            parent_id=item.get("constraint_id") or item.get("id"),
            atom_name=item.get("name"),
            atom_text=item.get("text") or item.get("description") or item.get("name"),
            requested_slots=["element_groups"],
            extra={"element_groups": copy.deepcopy(item.get("element_groups") or [])},
        )
    return {
        "schema_mode": "one_graph_two_tables_atom_element_only",
        "id_policy": {
            "atom_id": "global stable id used by element passes",
            "atom_sources": ["activation", "node_atom", "knowledge", "hard_constraint", "soft_constraint"],
            "no_anchor_concept_in_prompt": True,
            "role_aware_element_derivation": "assistant side expands element pools; user triggers expand likely user texts then elementize each text as an OR trigger group",
        },
        "instruction_digest": hashlib.sha1((instruction or "").encode("utf-8")).hexdigest()[:12],
        "entry_count": len(entries),
        "entries": entries,
    }


def build_atom_registry(graph: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    """Backward-compatible alias for the atom transport layer."""
    return build_atom_transport(graph, instruction)

def _find_anchor_targets(graph: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    targets: dict[str, tuple[str, dict[str, Any]]] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = _graph_node_id(node)
        if isinstance(node.get("activation"), dict):
            activation = node["activation"]
            if activation.get("element_anchor_id"):
                targets[str(activation["element_anchor_id"])] = ("activation", activation)
            targets[stable_atom_id("activation", nid)] = ("activation", activation)
        for atom in node.get("atoms") or []:
            if isinstance(atom, dict):
                local_id = str(atom.get("id") or atom.get("atom_id") or atom.get("name") or "atom")
                if atom.get("element_anchor_id"):
                    targets[str(atom["element_anchor_id"])] = ("node_atom", atom)
                targets[stable_atom_id("node_atom", nid, local_id)] = ("node_atom", atom)
    for table_name, kind in (("knowledge_table", "knowledge"), ("hard_constraint_table", "hard_constraint"), ("soft_constraint_table", "soft_constraint")):
        for item in graph.get(table_name) or []:
            if isinstance(item, dict):
                local_id = str(item.get("id") or item.get("atom_id") or item.get("knowledge_id") or item.get("constraint_id") or item.get("name") or kind)
                if item.get("element_anchor_id"):
                    targets[str(item["element_anchor_id"])] = (kind, item)
                targets[stable_atom_id(kind, local_id)] = (kind, item)
    return targets


def _normalize_element_group_list(value: Any) -> list[dict[str, Any]]:
    """Normalize LLM element group output to [{"elements":[...]}].

    Accepts canonical groups, mistaken flat element arrays, and common loose
    rows such as {"element":"对象", "value":"相反值", "fact":true}.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("element_groups", "groups", "selector_groups", "correct_groups", "wrong_groups", "trigger_groups", "negative_groups", "safe_groups"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value]
    if not isinstance(value, list):
        return []
    groups: list[dict[str, Any]] = []
    flat_elements: list[dict[str, Any]] = []

    def norm_elem(raw: Any, *, main: bool | None = None, fact: bool | None = None) -> dict[str, Any] | None:
        if isinstance(raw, str):
            text = raw.strip()
            return {"value": text, "main": bool(main), "fact": bool(fact), "pool": []} if text else None
        if not isinstance(raw, dict):
            return None
        val = str(raw.get("value") or raw.get("v") or raw.get("text") or raw.get("name") or raw.get("description") or "").strip()
        if not val:
            return None
        return {"value": val, "main": bool(raw.get("main") if main is None else main), "fact": bool(raw.get("fact") if fact is None else fact), "pool": list(raw.get("pool") or raw.get("secondary_pool") or raw.get("variants") or [])}

    for item in value:
        if isinstance(item, str):
            e = norm_elem(item)
            if e:
                flat_elements.append(e)
            continue
        if not isinstance(item, dict):
            continue
        elems: list[dict[str, Any]] = []
        if isinstance(item.get("elements"), list):
            elems = [e for e in (norm_elem(x) for x in item.get("elements") or []) if e]
        elif "element" in item and "value" in item:
            obj = norm_elem(item.get("element"), main=True, fact=False)
            val = norm_elem({"value": item.get("value"), "pool": item.get("pool") or []}, main=bool(item.get("main", False)), fact=bool(item.get("fact", True)))
            elems = [x for x in (obj, val) if x]
        else:
            e = norm_elem(item)
            if e:
                flat_elements.append(e)
                continue
        if elems:
            g = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"elements", "element", "value", "v", "text", "name", "description", "main", "fact", "pool", "secondary_pool", "variants"}}
            g["elements"] = elems
            groups.append(g)
    if groups:
        return groups
    if flat_elements:
        return [{"elements": flat_elements}]
    return []


def _ensure_group_main(groups: list[dict[str, Any]], *, fact_allowed: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in groups or []:
        g = copy.deepcopy(g)
        elems = [dict(e) for e in g.get("elements") or [] if isinstance(e, dict) and str(e.get("value") or "").strip()]
        if not elems:
            continue
        if not fact_allowed:
            for e in elems:
                e["fact"] = False
        if not any(e.get("main") is True for e in elems):
            elems[0]["main"] = True
        g["elements"] = elems
        out.append(g)
    return out


def _bind_fact_groups(groups: list[dict[str, Any]], selector_groups: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    selector_main = None
    for sg in selector_groups or []:
        for e in sg.get("elements") or []:
            if isinstance(e, dict) and e.get("main") is True and e.get("fact") is not True:
                selector_main = {"value": e.get("value"), "main": True, "fact": False, "pool": list(e.get("pool") or [])}
                break
        if selector_main:
            break
    out: list[dict[str, Any]] = []
    for g in groups or []:
        g = copy.deepcopy(g)
        elems = [dict(e) for e in g.get("elements") or [] if isinstance(e, dict) and str(e.get("value") or "").strip()]
        if not elems:
            continue
        for e in elems:
            if e.get("fact") is True:
                e["main"] = False
        if any(e.get("fact") is True for e in elems):
            has_non_fact_main = any(e.get("main") is True and e.get("fact") is not True for e in elems)
            if not has_non_fact_main and selector_main:
                if not any(str(x.get("value")) == str(selector_main.get("value")) for x in elems):
                    elems.insert(0, dict(selector_main))
                else:
                    for x in elems:
                        if str(x.get("value")) == str(selector_main.get("value")):
                            x["main"] = True
                            x["fact"] = False
                            break
        if not any(e.get("main") is True for e in elems):
            for e in elems:
                if e.get("fact") is not True:
                    e["main"] = True
                    break
        g["elements"] = elems
        out.append(g)
    return out


def _value_check_has_comparable(vc: Any) -> bool:
    if not isinstance(vc, dict) or not vc:
        return False
    candidates = [vc.get("expected_value"), vc.get("expected"), vc.get("normalized_expected")]
    checks = vc.get("checks") or vc.get("value_checks") or []
    if isinstance(checks, dict):
        checks = [checks]
    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, dict):
                candidates.extend([c.get("expected_value"), c.get("expected"), c.get("normalized_expected")])
    return any(str(x or "").strip() for x in candidates)

def _norm_elem_text(v: Any) -> str:
    return re.sub(r"[\s，。,.、：:；;（）()【】\[\]\-~—_]+", "", str(v or "").lower())

def _groups_signature(groups: list[dict[str, Any]]) -> set[str]:
    sig: set[str] = set()
    for g in groups or []:
        for e in g.get("elements") or []:
            if isinstance(e, dict):
                val = _norm_elem_text(e.get("value"))
                if val:
                    sig.add(val)
    return sig

def _drop_non_executable_wrong_groups(wrong_groups: list[dict[str, Any]], correct_groups: list[dict[str, Any]], value_check: Any) -> list[dict[str, Any]]:
    if _value_check_has_comparable(value_check):
        return []
    w_sig = _groups_signature(wrong_groups)
    c_sig = _groups_signature(correct_groups)
    if w_sig and c_sig and w_sig <= c_sig:
        return []
    return wrong_groups

def _normalize_node_atom_element_groups(groups: Any) -> list[dict[str, Any]]:
    out = _normalize_element_group_list(groups)
    for g in out:
        for e in g.get("elements") or []:
            if isinstance(e, dict):
                # Node atoms judge action completion only.  Comparable facts are
                # judged by knowledge/value_check, so do not let node elements
                # become fact gates.
                e["fact"] = False
    return out


def normalize_executable_groups(graph: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(graph)
    for node in out.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for atom in node.get("atoms") or []:
            if isinstance(atom, dict) and atom.get("element_groups") is not None:
                atom["element_groups"] = _normalize_node_atom_element_groups(atom.get("element_groups"))
    for item in out.get("knowledge_table") or []:
        if not isinstance(item, dict):
            continue
        selector = _ensure_group_main(_normalize_element_group_list(item.get("selector_groups") or item.get("selector_element_groups") or []), fact_allowed=False)
        item["selector_groups"] = selector
        item["correct_groups"] = _bind_fact_groups(_normalize_element_group_list(item.get("correct_groups") or item.get("correct_element_groups") or item.get("positive_element_groups") or []), selector)
        wrong_groups = _bind_fact_groups(_normalize_element_group_list(item.get("wrong_groups") or item.get("wrong_element_groups") or item.get("negative_element_groups") or []), selector)
        item["wrong_groups"] = _drop_non_executable_wrong_groups(wrong_groups, item["correct_groups"], item.get("value_check"))
        if item.get("negative_groups") and _value_check_has_comparable(item.get("value_check")):
            item["negative_groups"] = []
        if item.get("element_groups") is not None:
            item["element_groups"] = _normalize_element_group_list(item.get("element_groups"))
    for item in out.get("hard_constraint_table") or []:
        if not isinstance(item, dict):
            continue
        item["negative_groups"] = _ensure_group_main(_normalize_element_group_list(item.get("negative_groups") or item.get("negative_element_groups") or []), fact_allowed=True)
        item["safe_groups"] = _bind_fact_groups(_normalize_element_group_list(item.get("safe_groups") or item.get("positive_element_groups") or []), item.get("negative_groups") or [])
        item["trigger_groups"] = _ensure_group_main(_normalize_element_group_list(item.get("trigger_groups") or item.get("trigger_element_groups") or []), fact_allowed=False)
        if item.get("element_groups") is not None:
            item["element_groups"] = _normalize_element_group_list(item.get("element_groups"))
    for item in out.get("soft_constraint_table") or []:
        if isinstance(item, dict) and item.get("element_groups") is not None:
            item["element_groups"] = _normalize_element_group_list(item.get("element_groups"))
    return out

def _merge_element_groups(base: Any, delta: Any, *, secondary_only: bool = False, allow_new_groups: bool = False) -> list[dict[str, Any]]:
    base_groups = [copy.deepcopy(x) for x in base if isinstance(x, dict)] if isinstance(base, list) else []
    delta_groups = [copy.deepcopy(x) for x in delta if isinstance(x, dict)] if isinstance(delta, list) else []
    if not secondary_only:
        return delta_groups or base_groups
    if not base_groups:
        return delta_groups
    by_id = {str(g.get("group_id") or g.get("id") or i): g for i, g in enumerate(base_groups)}
    existing_sigs = {_norm_elem_text("|".join(str(e.get("value") or "") for e in (g.get("elements") or []) if isinstance(e, dict))) for g in base_groups}
    for dg_idx, dg in enumerate(delta_groups):
        gid = str(dg.get("group_id") or dg.get("id") or dg_idx)
        if gid not in by_id:
            # Secondary expansion prompts often return groups without ids.
            # For user trigger text expansion, new OR groups are intended and
            # should be appended. For assistant element pool expansion, unknown
            # groups are ignored to avoid schema drift.
            if allow_new_groups:
                sig = _norm_elem_text("|".join(str(e.get("value") or "") for e in (dg.get("elements") or []) if isinstance(e, dict)))
                if sig and sig not in existing_sigs:
                    base_groups.append(copy.deepcopy(dg))
                    existing_sigs.add(sig)
            continue
        bg = by_id[gid]
        base_elements = bg.get("elements") if isinstance(bg.get("elements"), list) else []
        delta_elements = dg.get("elements") if isinstance(dg.get("elements"), list) else []
        pool_written = False
        def _ekey(e: dict[str, Any], i: int | None = None) -> str:
            explicit = str(e.get("element_id") or e.get("id") or "").strip()
            if explicit:
                return "id:" + explicit
            value = str(e.get("value") or e.get("v") or e.get("text") or "").strip()
            if value:
                return "value:" + value
            return "idx:" + str(i if i is not None else "")

        e_index = {_ekey(e, i): e for i, e in enumerate(base_elements) if isinstance(e, dict)}
        # Also allow secondary expansion rows to reference an element by value only,
        # because the simplified prompt contract no longer requires element_id/type.
        for i, e in enumerate(base_elements):
            if isinstance(e, dict) and (e.get("value") or e.get("v")):
                e_index.setdefault("value:" + str(e.get("value") or e.get("v")), e)
        for de in delta_elements:
            if not isinstance(de, dict):
                continue
            target = e_index.get(_ekey(de))
            if target is None and (de.get("value") or de.get("v")):
                target = e_index.get("value:" + str(de.get("value") or de.get("v")))
            if target is None:
                continue
            for key in ("pool", "secondary_pool", "secondary_pool_terms", "aliases", "surface_forms", "semantic_equivalents"):
                if key in de and de.get(key) is not None:
                    vals = copy.deepcopy(de[key])
                    target[key] = vals
                    if key != "pool" and isinstance(vals, list):
                        # Public executable schema reads element.pool.  Keep it
                        # synchronized with compatibility pool fields.
                        existing = [x for x in (target.get("pool") or []) if isinstance(x, str)]
                        for x in vals:
                            if isinstance(x, str) and x not in existing and x != target.get("value"):
                                existing.append(x)
                        target["pool"] = existing
                        pool_written = True
                    elif key == "pool":
                        pool_written = True
        if allow_new_groups and not pool_written:
            # If this secondary trigger group represents a freshly elementized
            # user utterance rather than a pool update to an existing group, keep
            # it as an extra OR trigger group.
            sig = _norm_elem_text("|".join(str(e.get("value") or "") for e in (dg.get("elements") or []) if isinstance(e, dict)))
            if sig and sig not in existing_sigs:
                base_groups.append(copy.deepcopy(dg))
                existing_sigs.add(sig)
        if isinstance(dg.get("secondary_elements"), dict):
            bg.setdefault("secondary_elements", {}).update(copy.deepcopy(dg["secondary_elements"]))
    return base_groups

def _delta_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    for key in ("element_refinements", "secondary_expansions", "atom_element_refinements", "atom_elements", "entries"):
        if isinstance(raw.get(key), list):
            return [x for x in raw[key] if isinstance(x, dict)]
    return []


def merge_element_anchor_delta(graph: dict[str, Any], raw: dict[str, Any], *, secondary_only: bool = False) -> dict[str, Any]:
    merged = assign_element_anchor_ids(graph)
    targets = _find_anchor_targets(merged)
    ignored: list[str] = []
    for entry in _delta_entries(raw):
        atom_key = str(entry.get("atom_id") or entry.get("anchor_id") or entry.get("element_anchor_id") or "")
        if atom_key not in targets:
            ignored.append(atom_key or str(entry.get("source_id") or "unknown"))
            continue
        kind, target = targets[atom_key]
        if secondary_only:
            for key in ("element_groups", "positive_element_groups", "negative_element_groups", "trigger_element_groups", "trigger_groups", "selector_groups", "correct_groups", "wrong_groups", "negative_groups", "safe_groups"):
                if isinstance(entry.get(key), list):
                    target[key] = _merge_element_groups(target.get(key) or [], entry[key], secondary_only=True, allow_new_groups=(key == "trigger_groups"))
            for key in ("secondary_elements", "secondary_pools"):
                if isinstance(entry.get(key), dict):
                    target[key] = copy.deepcopy(entry[key])
            # hard constraint nested object pools are safe secondary-only fields.
            if kind == "hard_constraint" and isinstance(target.get("negative_object"), dict) and isinstance(entry.get("negative_object"), dict):
                no = entry["negative_object"]
                if isinstance(no.get("secondary_pools"), dict):
                    target["negative_object"].setdefault("secondary_pools", {}).update(copy.deepcopy(no["secondary_pools"]))
            continue
        allowed = {
            "primary_elements", "positive_elements", "negative_elements", "global_elements",
            "element_groups", "positive_element_groups", "negative_element_groups", "trigger_element_groups",
            "trigger_groups", "selector_groups", "correct_groups", "wrong_groups", "negative_groups", "safe_groups",
            "negation_rule", "verdict_logic",
            "secondary_elements", "secondary_pools",
        }
        group_keys = {"element_groups", "positive_element_groups", "negative_element_groups", "trigger_element_groups", "trigger_groups", "selector_groups", "correct_groups", "wrong_groups", "negative_groups", "safe_groups"}
        for key in allowed:
            if key in entry and entry.get(key) is not None:
                if key in group_keys:
                    target[key] = _normalize_element_group_list(entry[key])
                else:
                    target[key] = copy.deepcopy(entry[key])
        if kind == "hard_constraint" and isinstance(entry.get("negative_object"), dict):
            target.setdefault("negative_object", {})
            for key in ("primary_elements", "secondary_pools", "description"):
                if key in entry["negative_object"]:
                    target["negative_object"][key] = copy.deepcopy(entry["negative_object"][key])
    merged = normalize_executable_groups(merged)
    meta = ensure_metadata(merged)
    if ignored:
        meta.setdefault("element_delta_warnings", []).append({"type": "unknown_atom_id_ignored", "atom_ids": ignored[:30]})
    return merged


def remove_old_runtime_tables(graph: dict[str, Any]) -> dict[str, Any]:
    """Keep hard/soft authoritative and remove legacy merged table."""
    out = copy.deepcopy(graph)
    out.pop("constraint_table", None)
    for node in out.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        # Keep requirements as empty compatibility field if absent; do not carry
        # evidence_groups forward into atom-only schema.
        node.pop("evidence_groups", None)
        for req in node.get("requirements") or []:
            if isinstance(req, dict):
                req.pop("evidence_groups", None)
    return out
