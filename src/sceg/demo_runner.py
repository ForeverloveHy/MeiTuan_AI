from __future__ import annotations
from .graph_language import assert_chinese_context

import copy
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from .dataset_interface import DatasetInterface
from .dialogue_loader import load_dialogues
from .evidence_extractor import EvidenceExtractor
from .graph_evaluator import GraphEvaluator
from .io_utils import read_json, write_json, write_text
from .llm_client import LLMClient
from .llm_verifier import apply_llm_verifier
from .oracle_router import OracleRouter
from .report_explainer import ReportExplainer
from .report_html import render_case_html, render_html
from .schema import StateGraph
from .version import CORE_VERSION, runtime_version_info
from .score_adjuster import apply_dataset_score_adjustments
from .schema_compiler import compile_state_graph
from .schema_linter import lint_and_repair_schema
from .schema_repair_audit import audit_schema_repair_need, parse_binding_hints
from .schema_supplement_hints import (
    build_core_supplement_hints,
    build_knowledge_supplement_hints,
    build_constraint_supplement_hints,
    instruction_hard_constraint_requirement,
)
from .schema_atomic_pipeline import (
    build_atom_registry,
    build_atom_transport,
    merge_constraint_tables,
    merge_element_anchor_delta,
    merge_knowledge_table,
    remove_old_runtime_tables,
    strip_graph_core,
    assign_element_anchor_ids,
    normalize_executable_groups,
    sanitize_constraint_tables,
    merge_constraint_supplement,
)


def _now_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _ensure(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(value: Any, fallback: str = "graph") -> str:
    import re

    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value or "")).strip("_-")
    return s[:80] or fallback


def _stable_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _graph_cache_path(root: Path, key_data: dict[str, Any]) -> Path:
    cache_dir = root / "runs" / "graphs_llm" / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / ("graph_" + _stable_hash(key_data) + ".json")


def _unwrap_stage_output(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    for key in keys:
        val = raw.get(key)
        if isinstance(val, dict):
            return val
    return raw


def _table_output_nonempty(raw: dict[str, Any], key: str) -> bool:
    if not isinstance(raw, dict):
        return False
    val = raw.get(key)
    return isinstance(val, list) and any(isinstance(x, dict) for x in val)




def _safe_llm_stage_json(client: LLMClient, user_payload: str, prompt_text: str, *, purpose: str, default: dict[str, Any], emit_phase=None, phase: str = "llm", label: str = "") -> dict[str, Any]:
    """Call LLM JSON stage without letting optional stages kill the build.

    Used for second-pass supplements and element batches.  Core graph/table
    first-pass stages still call client.generate_json directly and fail fast.
    This keeps the pipeline reproducible while preventing one malformed optional
    batch from discarding an otherwise usable graph.
    """
    try:
        return client.generate_json(user_payload, prompt_text, purpose=purpose)
    except Exception as exc:
        if emit_phase is not None:
            try:
                emit_phase(phase, "warning", "%s JSON 解析失败，已跳过该可选阶段/批次：%s" % (label or purpose, str(exc).split("\n")[0][:260]))
            except Exception:
                pass
        out = copy.deepcopy(default)
        out.setdefault("_stage_skipped_due_to_invalid_json", True)
        out.setdefault("_stage_purpose", purpose)
        out.setdefault("_stage_error", str(exc)[:800])
        return out

def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(minimum, value)


def _split_atom_transport(registry: dict[str, Any], *, batch_size: int | None = None) -> list[dict[str, Any]]:
    """Split the atom transport into deterministic LLM batches.

    This is schema-generic and prevents the element stages from asking the
    model to emit a single very long JSON object.  Batches are grouped by
    source_kind first, then chunked by a small size.
    """
    entries = [x for x in (registry.get("entries") or []) if isinstance(x, dict)]
    if not entries:
        return [copy.deepcopy(registry)]
    batch_size = batch_size or len(entries)
    order = ["activation", "node_atom", "knowledge", "hard_constraint", "soft_constraint"]
    grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in order}
    extra: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        kind = str(e.get("source_kind") or "unknown")
        (grouped[kind] if kind in grouped else extra.setdefault(kind, [])).append(e)
    batches: list[dict[str, Any]] = []
    all_groups = [(k, grouped[k]) for k in order if grouped.get(k)] + [(k, v) for k, v in sorted(extra.items()) if v]
    for kind, vals in all_groups:
        for start in range(0, len(vals), batch_size):
            chunk = vals[start:start + batch_size]
            b = copy.deepcopy(registry)
            b["entries"] = copy.deepcopy(chunk)
            b["entry_count"] = len(chunk)
            b["batch"] = {
                "source_kind": kind,
                "start": start,
                "end": start + len(chunk),
                "total_for_source_kind": len(vals),
            }
            batches.append(b)
    total = len(batches)
    for idx, b in enumerate(batches, start=1):
        b.setdefault("batch", {})["batch_index"] = idx
        b.setdefault("batch", {})["batch_total"] = total
    return batches


def _split_atom_registry(registry: dict[str, Any], *, batch_size: int | None = None) -> list[dict[str, Any]]:
    # Backward-compatible internal alias.
    return _split_atom_transport(registry, batch_size=batch_size)



_GENERIC_FALLBACK_STOPWORDS = {
    "请问", "您好", "你好", "谢谢", "麻烦", "稍后", "好的", "进入", "点击", "选择", "保存",
    "说明", "告知", "询问", "确认", "提醒", "引导", "回复", "结束语", "处理", "分支",
}
_GENERIC_UNIT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:单|天|元|秒|点|分钟|小时|次|%)")
_GENERIC_BRACKET_RE = re.compile(r"[【\[（(\"“]([^【】\[\]（）()\"“”]{1,16})[】\]）)\"”]")
_GENERIC_PHRASE_SPLIT_RE = re.compile(r"[，,。；;：:\n\r\t、/]|然后|并且|以及|或者|或|和|与")


def _fallback_clean_phrase(value: Any) -> str:
    s = str(value or "").strip()
    s = re.sub(r"[*#`]+", "", s)
    s = re.sub(r"^[\-\d\.\s]+", "", s)
    s = re.sub(r"[。！？!?；;，,：:\s]+$", "", s)
    s = s.replace("【", "").replace("】", "").replace("“", "").replace("”", "").replace('"', "")
    return s.strip()


def _fallback_terms(*values: Any, max_terms: int = 3) -> list[str]:
    """Generic last-resort element extraction from existing schema text.

    This is intentionally task-agnostic: it never injects business words from
    code; it only shortens phrases already present in LLM's own schema.
    It is used when a batched element call skips an atom, so the local
    executor still has a minimal recall object instead of an empty atom.
    """
    out: list[str] = []
    def add(term: Any) -> None:
        t = _fallback_clean_phrase(term)
        if not t or t in _GENERIC_FALLBACK_STOPWORDS:
            return
        if len(t) < 2 or len(t) > 14:
            return
        if t not in out:
            out.append(t)
    joined = " ".join(str(v or "") for v in values)
    for m in _GENERIC_BRACKET_RE.findall(joined):
        add(m)
    for m in _GENERIC_UNIT_RE.findall(joined):
        add(m.replace(" ", ""))
    for raw in values:
        for part in _GENERIC_PHRASE_SPLIT_RE.split(str(raw or "")):
            part = _fallback_clean_phrase(part)
            if not part:
                continue
            # Prefer the compact action/object phrase from names, but avoid
            # entire long explanatory sentences.
            if len(part) <= 14:
                add(part)
            else:
                # Keep a short head phrase if it is the only material.
                add(part[:10])
            if len(out) >= max_terms:
                return out[:max_terms]
    return out[:max_terms]


def _fallback_group(terms: list[str], *, fact_values: set[str] | None = None, max_main: int = 2) -> list[dict[str, Any]]:
    elems: list[dict[str, Any]] = []
    fact_values = fact_values or set()
    for i, t in enumerate(terms):
        is_fact = t in fact_values
        elems.append({"value": t, "main": (i < max_main and not is_fact), "fact": bool(is_fact), "pool": []})
    return [{"elements": elems}] if elems else []


def _has_group_material(obj: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, list) and val:
            return True
        if isinstance(val, dict) and val:
            return True
    return False


def _fill_minimal_element_fallbacks(graph: dict[str, Any]) -> dict[str, Any]:
    """Fill skipped atom elements with minimal generic elements.

    Long outputs can cause the model to skip a few anchors even when JSON is
    valid.  The fallback is schema-derived and does not add facts or business
    rules; it only turns existing name/text/trigger_hint into short recall
    elements.  LLM output still wins whenever it provided executable groups.
    """
    changed: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or node.get("node_id") or node.get("name") or "node")
        activation = node.get("activation") if isinstance(node.get("activation"), dict) else {}
        mode = str(activation.get("mode") or "always")
        if mode in {"condition", "user_triggered"} and not _has_group_material(activation, "trigger_groups", "element_groups", "primary_elements", "trigger_object"):
            terms = _fallback_terms(activation.get("trigger_hint"), node.get("name"), max_terms=2)
            if terms:
                activation["trigger_groups"] = _fallback_group(terms, max_main=2)
                changed.append(f"activation:{nid}")
        for atom in node.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            if atom.get("required", True) is False:
                continue
            if not _has_group_material(atom, "element_groups", "primary_elements", "positive_elements", "positive_object", "element_rule"):
                terms = _fallback_terms(atom.get("name"), atom.get("text"), max_terms=3)
                if terms:
                    atom["element_groups"] = _fallback_group(terms, max_main=2)
                    changed.append(str(atom.get("id") or atom.get("atom_id") or nid))
    def iter_atoms(table: Any, parent_key: str):
        for item in table or []:
            if not isinstance(item, dict):
                continue
            atoms = item.get("atoms")
            if isinstance(atoms, list) and atoms:
                for atom in atoms:
                    if isinstance(atom, dict):
                        yield item, atom
            else:
                yield item, item
    for parent, atom in iter_atoms(graph.get("knowledge_table") or [], "knowledge_id"):
        aid = str(atom.get("id") or atom.get("atom_id") or parent.get("id") or parent.get("knowledge_id") or "knowledge")
        if not _has_group_material(atom, "selector_groups", "primary_elements", "element_groups"):
            terms = _fallback_terms(atom.get("name"), atom.get("text"), max_terms=2)
            if terms:
                atom["selector_groups"] = _fallback_group(terms, max_main=2)
                changed.append(f"knowledge:{aid}:selector")
        if not _has_group_material(atom, "correct_groups", "positive_elements"):
            vc = atom.get("value_check") if isinstance(atom.get("value_check"), dict) else {}
            expected = _fallback_clean_phrase(vc.get("expected_value") or vc.get("expected"))
            terms = _fallback_terms(atom.get("name"), atom.get("text"), max_terms=2)
            if expected and terms:
                all_terms = terms[:1] + [expected]
                atom["correct_groups"] = _fallback_group(all_terms, fact_values={expected}, max_main=1)
                changed.append(f"knowledge:{aid}:correct")
    for parent, atom in iter_atoms(graph.get("hard_constraint_table") or graph.get("constraint_table") or [], "constraint_id"):
        if not isinstance(atom, dict):
            continue
        aid = str(atom.get("id") or atom.get("atom_id") or parent.get("id") or parent.get("constraint_id") or "constraint")
        if not _has_group_material(atom, "negative_groups", "negative_elements", "negative_object", "primary_elements"):
            terms = _fallback_terms(atom.get("name"), atom.get("text"), parent.get("name"), max_terms=2)
            if terms:
                atom["negative_groups"] = _fallback_group(terms, max_main=2)
                changed.append(f"hard:{aid}:negative")
    if changed:
        meta = graph.setdefault("metadata", {})
        meta.setdefault("element_fallback_repair", {})["filled_atoms"] = changed[:80]
        meta["element_fallback_repair"]["count"] = len(changed)

    return graph


# Hard constraints are intentionally not synthesized locally.
# Empty hard tables must be fixed by prompt/model iteration so the graph remains reproducible.

def _zip_dir(src_dir: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p == out_path:
                continue
            if p.is_file():
                zf.write(p, p.relative_to(src_dir))


def _pattern_from_texts(texts: list[Any], speaker: str = "assistant") -> list[dict[str, Any]]:
    vals = []
    seen = set()
    for x in texts:
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            vals.append(s)
    return [{"speaker": speaker, "any": vals, "min_any_hits": 1}] if vals else []


def _legacy_to_latest(raw: dict[str, Any]) -> dict[str, Any]:
    """Best-effort compatibility for older graph-builder output.

    This adapter is structural only: it does not infer task-specific evidence.
    If the model already outputs the latest schema, the adapter is bypassed.
    """
    if "nodes" in raw:
        return raw
    flow = raw.get("flow_graph") or {}
    nodes = []
    for node in flow.get("nodes", []) or []:
        activation = node.get("activation") or {}
        if not isinstance(activation, dict):
            activation = {"mode": str(activation or "start"), "seed_intents": []}
        mode = str(activation.get("mode") or "start")
        latest_mode = "always" if mode in {"start", "always"} else "user_triggered"
        trigger_texts = []
        for key in ("seed_intents", "positive_examples"):
            trigger_values = activation.get(key) or []
            if isinstance(trigger_values, str):
                trigger_values = [trigger_values]
            trigger_texts.extend(trigger_values)
        reqs = []
        for ridx, req in enumerate(node.get("requirements", []) or [], start=1):
            text = req.get("text") or req.get("expected") or req.get("description") or ""
            mp = req.get("match_profile") or {}
            evidence_texts = []
            evidence_texts.extend(mp.get("positive_examples") or [])
            evidence_texts.append(text)
            req_id = str(req.get("id") or f"{node.get('id','node')}.r{ridx}")
            reqs.append(
                {
                    "id": req_id,
                    "text": text,
                    "required": req.get("required", True),
                    "weight": float(req.get("weight") or 1.0),
                    "evidence_groups": [
                        {
                            "id": req_id + ".g1",
                            "description": text,
                            "required": True,
                            "weight": 1.0,
                            "min_hits": 1,
                            "patterns": _pattern_from_texts(evidence_texts, "assistant"),
                        }
                    ],
                }
            )
        if not reqs:
            text = node.get("name") or node.get("id") or ""
            reqs = [
                {
                    "id": str(node.get("id") or "node") + ".r1",
                    "text": text,
                    "required": True,
                    "weight": 1.0,
                    "evidence_groups": [
                        {
                            "id": str(node.get("id") or "node") + ".g1",
                            "description": text,
                            "required": True,
                            "weight": 1.0,
                            "min_hits": 1,
                            "patterns": _pattern_from_texts([text], "assistant"),
                        }
                    ],
                }
            ]
        nodes.append(
            {
                "id": str(node.get("id")),
                "name": str(node.get("name") or node.get("id")),
                "type": "process",
                "required": str(node.get("obligation") or "required") != "optional",
                "activation": {"mode": latest_mode, "patterns": _pattern_from_texts(trigger_texts, "user")},
                "requirements": reqs,
            }
        )
    edges = []
    groups = []
    for rel in flow.get("relations", []) or []:
        typ = str(rel.get("type") or "")
        if typ in {"strict_before", "soft_before"} and rel.get("from") and rel.get("to"):
            edges.append(
                {
                    "source": str(rel.get("from")),
                    "target": str(rel.get("to")),
                    "relation": "strict_order" if typ == "strict_before" else "soft_order",
                    "weight": 1.0,
                }
            )
        elif typ in {"unordered_group", "partial_order_group", "choice"}:
            members = rel.get("members") or rel.get("options") or []
            groups.append(
                {
                    "id": str(rel.get("id") or f"group_{len(groups)+1}"),
                    "name": str(rel.get("description") or rel.get("id") or "关系组"),
                    "type": "any_of" if typ == "choice" else "unordered",
                    "nodes": [str(x) for x in members],
                    "min_completed": rel.get("min_required") or (1 if typ == "choice" else len(members)),
                    "weight": 1.0,
                    "required": True,
                    "description": str(rel.get("description") or ""),
                }
            )
    knowledge = []
    for idx, item in enumerate(raw.get("knowledge_table", []) or [], start=1):
        text = item.get("correct_value") or item.get("value") or item.get("expected") or item.get("name") or item.get("subject") or ""
        kid = str(item.get("id") or f"k_{idx}")
        knowledge.append(
            {
                "id": kid,
                "name": str(item.get("name") or item.get("subject") or kid),
                "node_id": item.get("node_id"),
                "judge_type": "claim_evidence",
                "severity": item.get("severity", "medium"),
                "claims": [
                    {
                        "id": kid + ".claim",
                        "name": str(item.get("name") or item.get("subject") or kid),
                        "support_patterns": _pattern_from_texts([text] + list(item.get("equivalent_values") or []), "assistant"),
                        "refute_patterns": item.get("refute_patterns") or item.get("conflict_patterns") or [],
                        "severity": item.get("severity", "medium"),
                        "reason": "知识表核验",
                    }
                ],
            }
        )
    constraints = []
    raw_constraints = []
    for key, enforcement, default_kind in (
        ("hard_constraint_table", "hard", "semantic_object"),
        ("soft_constraint_table", "soft", "fuzzy_quality"),
    ):
        for item in raw.get(key, []) or []:
            if isinstance(item, dict):
                cloned = dict(item)
                cloned.setdefault("enforcement", enforcement)
                cloned.setdefault("constraint_kind", default_kind)
                raw_constraints.append(cloned)
    for item in raw.get("constraint_table", []) or []:
        if isinstance(item, dict):
            raw_constraints.append(dict(item))
    for idx, item in enumerate(raw_constraints, start=1):
        cid = str(item.get("id") or f"c_{idx}")
        pats = item.get("prohibited") or item.get("patterns") or []
        prohibited = pats if pats and isinstance(pats[0], dict) else _pattern_from_texts(pats, "assistant")
        constraints.append(
            {
                "id": cid,
                "name": str(item.get("name") or item.get("rule") or cid),
                "node_id": item.get("node_id"),
                "severity": item.get("severity", "high"),
                "description": str(item.get("description") or item.get("rule") or ""),
                "enforcement": item.get("enforcement") or ("soft" if item.get("constraint_kind") == "fuzzy_quality" else "hard"),
                "constraint_kind": item.get("constraint_kind") or item.get("constraint_type") or ("fuzzy_quality" if item.get("enforcement") == "soft" else "semantic_object"),
                "trigger_policy": item.get("trigger_policy") or ("global_style" if item.get("enforcement") == "soft" else ("requires_user_trigger" if item.get("trigger") else "self_sufficient")),
                "negative_object": item.get("negative_object") or {},
                "detection_scope": item.get("detection_scope") or {},
                "verdict_logic": item.get("verdict_logic") or "",
                "trigger": item.get("trigger") if isinstance(item.get("trigger"), list) else [],
                "safe_context": item.get("safe_context") or [],
                "prohibited": prohibited,
                "unresolved": item.get("unresolved") or item.get("grey_zone") or [],
                "violation_scope": item.get("violation_scope") or {},
                "soft_rule": item.get("soft_rule") or {},
                "quality_dimension": item.get("quality_dimension") or "",
                "evaluation_basis": item.get("evaluation_basis") or {},
                "score_effect": item.get("score_effect") or {},
                "requires_resolution": bool(item.get("requires_resolution", False)),
                "allow_multiple": bool(item.get("allow_multiple", False)),
            }
        )
    return {
        "graph_id": str(raw.get("graph_id") or raw.get("flow_id") or "llm_graph"),
        "name": str(raw.get("name") or raw.get("flow_id") or "LLM 状态图"),
        "metadata": {"domain": raw.get("domain") or (raw.get("metadata") or {}).get("domain") or "general", "generated_by": "llm"},
        "nodes": nodes,
        "edges": edges,
        "relation_groups": groups,
        "knowledge_table": knowledge,
        "constraint_table": constraints,
        "terminal_policies": raw.get("terminal_policies") or raw.get("termination_policies") or [],
    }


def _load_prompt(root: Path, name: str = "latest_schema_graph_prompt.md") -> str:
    prompt_path = root / "prompts" / name
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    raise RuntimeError(f"缺少 LLM 提示词：{prompt_path}")


def _load_stage_prompt(root: Path, name: str) -> str:
    """Load one stage prompt with the shared SCEG method memory prepended.

    The memory prompt is a compact method lexicon: it explains atom/element,
    main/fact/pool and the graph/table responsibility split once, before every
    stage.  Stage prompts remain short and stage-specific.
    """
    memory_path = root / "prompts" / "sceg_method_memory_prompt.md"
    stage = _load_prompt(root, name)
    if not memory_path.exists():
        return stage
    memory = memory_path.read_text(encoding="utf-8").strip()
    return memory + "\n\n【本阶段专门提示词】\n" + stage.strip()


def _compact_schema_for_stage2(graph: dict[str, Any]) -> dict[str, Any]:
    """Return a compact atom skeleton for required element细化.

    Stage 2 must be fast: it should not ask LLM to rewrite the full graph.
    The model receives ids, names, atom texts and any existing element hints, and
    returns a small refinement delta keyed by existing ids.
    """
    def keep(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {k: copy.deepcopy(d.get(k)) for k in keys if k in d and d.get(k) not in (None, [], {})}

    nodes: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        n = keep(node, ("id", "name", "type", "required", "activation", "atom_relations"))
        atoms = []
        for atom in node.get("atoms") or []:
            if isinstance(atom, dict):
                atoms.append(keep(atom, ("id", "name", "text", "atom_type", "object_role", "required", "weight", "primary_elements", "positive_elements", "negative_elements", "element_groups", "positive_element_groups", "negative_element_groups", "match_policy")))
        n["atoms"] = atoms
        nodes.append(n)

    def compact_items(name: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        out = []
        for item in graph.get(name) or []:
            if isinstance(item, dict):
                out.append(keep(item, keys))
        return out

    return {
        "graph_id": graph.get("graph_id"),
        "name": graph.get("name"),
        "nodes": nodes,
        "edges": copy.deepcopy(graph.get("edges") or []),
        "relation_groups": copy.deepcopy(graph.get("relation_groups") or []),
        "knowledge_table": compact_items("knowledge_table", ("id", "name", "node_id", "judge_type", "severity", "positive_elements", "negative_elements", "match_policy")),
        "hard_constraint_table": compact_items("hard_constraint_table", ("id", "name", "node_id", "constraint_kind", "severity", "trigger_policy", "trigger_object", "negative_object", "negative_elements", "positive_elements", "match_policy")),
        "soft_constraint_table": compact_items("soft_constraint_table", ("id", "name", "quality_dimension", "global_elements", "metric", "score_effect", "description")),
        "terminal_policies": copy.deepcopy(graph.get("terminal_policies") or {}),
    }


def _compact_schema_for_stage3(graph: dict[str, Any]) -> dict[str, Any]:
    """Return only ids and primary elements needed for element扩张."""
    compact = _compact_schema_for_stage2(graph)
    # Stage 3 only needs existing primary/global/positive/negative/zero elements
    # and existing secondary pools.  It does not need metadata, reports, or old
    # compatibility fields.
    return compact


def _compact_graph_core_for_tables(graph: dict[str, Any]) -> dict[str, Any]:
    """Small graph view used by knowledge/constraint generation.

    Knowledge and constraint tables do not need compiler metadata, old runtime
    compatibility fields or generated elements.  Passing only state names, atom
    texts and topology keeps LLM table calls shorter and reduces accidental
    schema drift.
    """
    nodes: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        activation = node.get("activation") if isinstance(node.get("activation"), dict) else {}
        atoms: list[dict[str, Any]] = []
        for atom in node.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            atoms.append({
                "atom_id": atom.get("atom_id") or atom.get("id"),
                "name": atom.get("name"),
                "text": atom.get("text"),
                "required": atom.get("required", True),
                "weight": atom.get("weight", 1),
            })
        nodes.append({
            "node_id": node.get("node_id") or node.get("id"),
            "name": node.get("name"),
            "node_type": node.get("node_type") or node.get("type"),
            "required": node.get("required", True),
            "activation": {
                "mode": activation.get("mode"),
                "trigger_hint": activation.get("trigger_hint") or activation.get("description") or activation.get("hint"),
            },
            "atoms": atoms,
            "atom_relations": node.get("atom_relations") or [],
        })
    return {
        "graph_id": graph.get("graph_id"),
        "name": graph.get("name"),
        "nodes": nodes,
        "edges": graph.get("edges") or [],
        "relation_groups": graph.get("relation_groups") or [],
        "terminal_policies": graph.get("terminal_policies") or {},
    }


def _compact_knowledge_index(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in graph.get("knowledge_table") or []:
        if not isinstance(item, dict):
            continue
        atoms = item.get("atoms")
        if isinstance(atoms, list):
            rows.append({
                "knowledge_id": item.get("knowledge_id") or item.get("id"),
                "name": item.get("name"),
                "atoms": [
                    {"atom_id": a.get("atom_id") or a.get("id"), "name": a.get("name"), "text": a.get("text"), "value_check": a.get("value_check")}
                    for a in atoms if isinstance(a, dict)
                ],
            })
        else:
            rows.append({"knowledge_id": item.get("knowledge_id") or item.get("id"), "name": item.get("name"), "text": item.get("text"), "value_check": item.get("value_check")})
    return rows


def _build_element_refinement_instruction(instruction: str, graph: dict[str, Any], audit: dict[str, Any], binding_hints: str | None) -> str:
    payload = {
        "task": "stage2_element_refinement_delta_only",
        "original_complex_instruction": instruction,
        "atom_schema_skeleton": _compact_schema_for_stage2(graph),
        "local_refinement_hints": audit,
        "binding_hints_tail": parse_binding_hints(binding_hints),
        "return_contract": {
            "output_type": "delta_only_not_full_schema",
            "allowed_top_level_keys": [
                "node_refinements",
                "knowledge_refinements",
                "hard_constraint_refinements",
                "soft_constraint_refinements",
                "terminal_policy_refinement",
            ],
            "notes": [
                "Do not return full nodes/edges/relation_groups/metadata.",
                "Use existing ids only. Missing ids are ignored by local merge.",
                "Fill element_groups first; use primary_elements/positive_elements/negative_elements only as flat compatibility fields where needed.",
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_element_expansion_instruction(instruction: str, graph: dict[str, Any]) -> str:
    """Build third-phase delta payload for secondary element expansion.

    Stage 3 should be much faster than full graph generation: it receives a
    compact element skeleton and returns only secondary-pool deltas.
    """
    payload = {
        "task": "stage3_element_expansion_delta_only",
        "instruction": instruction,
        "element_schema_skeleton": _compact_schema_for_stage3(graph),
        "requirements": [
            "return only secondary expression pool deltas keyed by existing ids",
            "do not return full schema, metadata, nodes, edges, relation_groups, or unchanged fields",
            "do not change primary_elements, positive_elements, negative_elements, match_policy, business facts, numbers, times, fees, or rule conclusions",
            "zero_level_elements are deprecated and must not be generated",
        ],
        "return_contract": {
            "output_type": "secondary_delta_only_not_full_schema",
            "allowed_top_level_keys": [
                "node_refinements",
                "knowledge_refinements",
                "hard_constraint_refinements",
                "soft_constraint_refinements",
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _update_allowed(target: dict[str, Any], source: dict[str, Any], keys: tuple[str, ...]) -> None:
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for key in keys:
        if key in source and source.get(key) is not None:
            target[key] = copy.deepcopy(source.get(key))


def _iter_delta_nodes(delta: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(delta.get("node_refinements"), list):
        return [x for x in delta.get("node_refinements") or [] if isinstance(x, dict)]
    if isinstance(delta.get("nodes"), list):
        return [x for x in delta.get("nodes") or [] if isinstance(x, dict)]
    return []


def _delta_items(delta: dict[str, Any], delta_key: str, full_key: str) -> list[dict[str, Any]]:
    items = delta.get(delta_key)
    if not isinstance(items, list):
        items = delta.get(full_key)
    return [x for x in (items or []) if isinstance(x, dict)] if isinstance(items, list) else []


def _merge_stage2_refinement_delta(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Merge stage-2 element细化 deltas into the base graph.

    The merge is deliberately conservative: it accepts element rules and
    decision policies for existing ids, but it does not accept structural graph
    changes such as new node ids, relation rewrites, or metadata rewrites.
    """
    if not isinstance(delta, dict):
        return base
    merged = copy.deepcopy(base)
    delta_nodes = {str(n.get("node_id") or n.get("id")): n for n in _iter_delta_nodes(delta) if n.get("node_id") or n.get("id")}
    for node in merged.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        src_node = delta_nodes.get(str(node.get("id"))) or {}
        if src_node:
            if isinstance(src_node.get("activation"), dict):
                node.setdefault("activation", {})
                _update_allowed(node["activation"], src_node["activation"], ("trigger_hint", "trigger_object", "primary_elements", "element_groups", "trigger_groups", "trigger_element_groups", "secondary_elements", "match_policy"))
            src_atoms_list = src_node.get("atom_refinements") if isinstance(src_node.get("atom_refinements"), list) else src_node.get("atoms")
            src_atoms = {str(a.get("atom_id") or a.get("id")): a for a in (src_atoms_list or []) if isinstance(a, dict) and (a.get("atom_id") or a.get("id"))}
            for atom in node.get("atoms") or []:
                if not isinstance(atom, dict):
                    continue
                src_atom = src_atoms.get(str(atom.get("id"))) or {}
                _update_allowed(atom, src_atom, ("primary_elements", "positive_elements", "negative_elements", "element_groups", "positive_element_groups", "negative_element_groups", "secondary_elements", "secondary_pools", "match_policy", "required", "weight"))

    def merge_table(table_name: str, delta_key: str, keys: tuple[str, ...]) -> None:
        src = {str(x.get("id")): x for x in _delta_items(delta, delta_key, table_name) if x.get("id")}
        for item in merged.get(table_name) or []:
            if isinstance(item, dict):
                _update_allowed(item, src.get(str(item.get("id"))) or {}, keys)

    merge_table("knowledge_table", "knowledge_refinements", ("judge_type", "severity", "selector_groups", "correct_groups", "wrong_groups", "selector_element_groups", "correct_element_groups", "wrong_element_groups", "positive_elements", "negative_elements", "secondary_elements", "negation_rule", "value_check", "match_policy"))
    merge_table("hard_constraint_table", "hard_constraint_refinements", ("constraint_kind", "severity", "trigger_policy", "trigger_groups", "negative_groups", "safe_groups", "trigger_element_groups", "negative_element_groups", "positive_element_groups", "trigger_object", "negative_object", "negative_elements", "positive_elements", "secondary_elements", "match_policy", "verdict_logic", "allow_multiple"))
    merge_table("soft_constraint_table", "soft_constraint_refinements", ("quality_dimension", "global_elements", "secondary_elements", "metric", "score_effect", "description"))
    if isinstance(delta.get("terminal_policy_refinement"), dict):
        merged["terminal_policies"] = copy.deepcopy(delta.get("terminal_policy_refinement"))
    merged.setdefault("metadata", {})["stage2_merge_policy"] = "element_refinement_delta_only"
    return merged

def _copy_secondary_only(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Copy only secondary expression pools from source into target.

    This guard makes stage 3 logically strict: element扩张 may enrich wording
    variants, aliases, templates and negative examples, but it cannot change
    ids, atoms, primary elements, facts, constraints, relations or weights.
    """
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for key in ("secondary_elements", "secondary_pools"):
        if isinstance(source.get(key), dict):
            target[key] = source.get(key)
    # Nested target objects may also hold secondary pools.
    for key in ("trigger_object", "positive_object", "negative_object", "soft_rule"):
        if isinstance(target.get(key), dict) and isinstance(source.get(key), dict):
            _copy_secondary_only(target[key], source[key])


def _by_id(items: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out[str(item.get("id"))] = item
    return out



def _merge_stage3_secondary_pools(base: dict[str, Any], expanded: dict[str, Any]) -> dict[str, Any]:
    """Return base schema enriched only with stage-3 secondary pools.

    Accepts both the new delta-only contract and accidental full-schema output.
    In all cases, only secondary_elements / secondary_pools fields are copied.
    """
    merged = copy.deepcopy(base)
    if not isinstance(expanded, dict):
        return merged

    expanded_nodes = {str(n.get("node_id") or n.get("id")): n for n in _iter_delta_nodes(expanded) if n.get("node_id") or n.get("id")}
    for node in merged.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        src_node = expanded_nodes.get(str(node.get("id"))) or {}
        if isinstance(node.get("activation"), dict) and isinstance(src_node.get("activation"), dict):
            _copy_secondary_only(node["activation"], src_node["activation"])
        src_atoms_list = src_node.get("atom_refinements") if isinstance(src_node.get("atom_refinements"), list) else src_node.get("atoms")
        src_atoms = {str(a.get("atom_id") or a.get("id")): a for a in (src_atoms_list or []) if isinstance(a, dict) and (a.get("atom_id") or a.get("id"))}
        for atom in node.get("atoms") or []:
            if isinstance(atom, dict):
                _copy_secondary_only(atom, src_atoms.get(str(atom.get("id"))) or {})
        src_reqs = _by_id(src_node.get("requirements"))
        for req in node.get("requirements") or []:
            if isinstance(req, dict):
                _copy_secondary_only(req, src_reqs.get(str(req.get("id"))) or {})

    table_specs = (
        ("knowledge_table", "knowledge_refinements"),
        ("hard_constraint_table", "hard_constraint_refinements"),
        ("soft_constraint_table", "soft_constraint_refinements"),
        ("constraint_table", "constraint_refinements"),
    )
    for table_name, delta_key in table_specs:
        src_items = {str(x.get("id")): x for x in _delta_items(expanded, delta_key, table_name) if x.get("id")}
        for item in merged.get(table_name) or []:
            if isinstance(item, dict):
                _copy_secondary_only(item, src_items.get(str(item.get("id"))) or {})
    merged.setdefault("metadata", {})["stage3_merge_policy"] = "secondary_pools_delta_only"
    return merged


def _has_group_elements(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for group in value:
        if not isinstance(group, dict):
            continue
        elems = group.get("elements") or group.get("primary_elements") or group.get("required_elements") or []
        if isinstance(elems, list):
            for elem in elems:
                if isinstance(elem, dict) and str(elem.get("value") or elem.get("v") or elem.get("text") or elem.get("name") or "").strip():
                    return True
                if isinstance(elem, str) and elem.strip():
                    return True
    return False


def _has_executable_elements(item: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = item.get(key)
        if _has_group_elements(value):
            return True
        if isinstance(value, list) and value:
            # Flat element lists are still accepted for legacy graphs.
            for row in value:
                if isinstance(row, dict) and str(row.get("value") or row.get("v") or row.get("text") or row.get("name") or "").strip():
                    return True
                if isinstance(row, str) and row.strip():
                    return True
        if isinstance(value, dict):
            for nested_key in (
                "primary_elements", "required_elements", "surface_forms",
                "semantic_equivalents", "aliases", "evidence_phrases",
                "elements", "any", "all",
            ):
                nested = value.get(nested_key)
                if _has_group_elements(nested):
                    return True
                if isinstance(nested, list) and nested:
                    return True
                if isinstance(nested, str) and nested.strip():
                    return True
    return False


def _activation_has_trigger_material(activation: dict[str, Any]) -> bool:
    if not isinstance(activation, dict):
        return False
    if activation.get("patterns") or activation.get("trigger_object") or activation.get("primary_elements"):
        return True
    if str(activation.get("trigger_hint") or activation.get("description") or activation.get("hint") or "").strip():
        return True
    return any(_has_group_elements(activation.get(key)) for key in ("trigger_groups", "trigger_element_groups", "element_groups"))


def _iter_parent_atoms(items: Any, *, id_key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        atoms = item.get("atoms")
        if not isinstance(atoms, list):
            rows.append(item)
            continue
        parent = {k: v for k, v in item.items() if k != "atoms"}
        parent_id = str(parent.get(id_key) or parent.get("id") or f"{id_key}_{idx}")
        for j, atom in enumerate(atoms, start=1):
            if not isinstance(atom, dict):
                continue
            row = copy.deepcopy(parent)
            row.update(copy.deepcopy(atom))
            row.setdefault(id_key, parent_id)
            row.setdefault("id", atom.get("atom_id") or atom.get("id") or f"{parent_id}_atom_{j:02d}")
            rows.append(row)
    return rows


def _knowledge_rule_executable(item: dict[str, Any]) -> bool:
    # New clean contract: selector_groups identify the fact object; correct/wrong
    # groups and value_check make the fact executable.  A single row with only
    # selector material is not enough to judge support/refute.
    has_selector = _has_executable_elements(item, "selector_groups", "selector_element_groups", "element_groups", "primary_elements")
    has_value_side = (
        _has_executable_elements(item, "correct_groups", "correct_element_groups", "positive_element_groups", "positive_elements")
        or _has_executable_elements(item, "wrong_groups", "wrong_element_groups", "negative_element_groups", "negative_elements")
        or bool(item.get("value_check"))
        or bool(item.get("claims") or item.get("support_patterns") or item.get("conflict_patterns") or item.get("refute_patterns"))
    )
    return bool(has_selector and has_value_side)


def _hard_constraint_rule_executable(item: dict[str, Any]) -> bool:
    # Structural hard constraints are executable via metric.  Semantic hard
    # constraints need a negative side; trigger/safe groups are optional unless
    # the constraint is context-dependent.
    if isinstance(item.get("metric"), dict) and item.get("metric"):
        return True
    return _has_executable_elements(
        item,
        "negative_groups", "negative_element_groups", "negative_elements",
        "negative_object", "element_groups", "primary_elements", "prohibited",
    )



def _graph_has_constraint_boundary(graph: dict[str, Any]) -> bool:
    """Legacy coarse signal kept for diagnostics only.

    Earlier builds used this broad graph/knowledge scan as a blocking rule.  That
    was too aggressive: ordinary facts such as 生效状态、申请排序、页面入口、数量周期 can look like
    rule/system boundaries but do not necessarily require hard constraints.  The
    actual hard-table requirement must be derived from the original instruction
    via instruction_hard_constraint_requirement().
    """
    text_parts: list[str] = []
    for node in graph.get("nodes") or []:
        if isinstance(node, dict):
            text_parts.append(str(node.get("name") or ""))
            text_parts.append(str(node.get("type") or node.get("node_type") or ""))
            for atom in node.get("atoms") or []:
                if isinstance(atom, dict):
                    text_parts.append(str(atom.get("name") or ""))
                    text_parts.append(str(atom.get("text") or ""))
    for item in graph.get("knowledge_table") or []:
        if isinstance(item, dict):
            text_parts.append(str(item.get("name") or ""))
            text_parts.append(str(item.get("text") or ""))
    text = " ".join(text_parts)
    diagnostic_terms = ("职责", "越权", "承诺", "保证", "代操作", "人工修改", "安全", "隐私", "敏感")
    return any(t in text for t in diagnostic_terms)

def _stage2_quality_gate(graph: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    """Check whether mandatory element细化 produced executable local rules.

    The gate is structural and schema-driven.  It does not inspect dialogue
    answer keys; it only verifies that required atoms, knowledge facts and hard
    constraints have enough element-side material for the local evaluator.
    """
    required_atoms = 0
    missing_atom_elements: list[str] = []
    missing_activation_triggers: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        activation = node.get("activation") if isinstance(node.get("activation"), dict) else {}
        mode = str(activation.get("mode") or "always")
        if mode in {"user_triggered", "condition"}:
            if not _activation_has_trigger_material(activation):
                missing_activation_triggers.append(str(node.get("id") or node.get("node_id") or node.get("name") or "node"))
        atoms = [a for a in node.get("atoms") or [] if isinstance(a, dict)]
        reqs = [r for r in node.get("requirements") or [] if isinstance(r, dict)]
        for atom in atoms or reqs:
            # In the atom-only graph, node atoms are executable scoring units;
            # object_role can be rider/contract/enrollment, not just
            # positive_object.  Count all required node atoms except explicitly
            # negative/constraint atoms.
            role = str(atom.get("object_role") or atom.get("role") or "positive_object")
            if role in {"negative_object", "constraint", "hard_constraint", "soft_constraint"}:
                continue
            if atom.get("required", True) is False:
                continue
            required_atoms += 1
            if not _has_executable_elements(atom, "element_groups", "primary_elements", "positive_elements", "positive_object", "element_rule") and not atom.get("evidence_groups"):
                missing_atom_elements.append(str(atom.get("id") or atom.get("atom_id") or f"{node.get('id')}.atom"))
    required_atoms = max(required_atoms, 0)
    missing_ratio = len(missing_atom_elements) / max(1, required_atoms)

    knowledge_rows = _iter_parent_atoms(graph.get("knowledge_table") or [], id_key="knowledge_id")
    knowledge_total = 0
    weak_knowledge: list[str] = []
    for item in knowledge_rows:
        if not isinstance(item, dict):
            continue
        knowledge_total += 1
        if not _knowledge_rule_executable(item):
            weak_knowledge.append(str(item.get("id") or item.get("atom_id") or item.get("knowledge_id") or "knowledge"))

    hard_rows = _iter_parent_atoms((graph.get("hard_constraint_table") or graph.get("constraint_table") or []), id_key="constraint_id")
    hard_total = 0
    weak_hard: list[str] = []
    for item in hard_rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("enforcement") or "hard") == "soft":
            continue
        hard_total += 1
        if not _hard_constraint_rule_executable(item):
            weak_hard.append(str(item.get("id") or item.get("atom_id") or item.get("constraint_id") or "constraint"))

    blocking_reasons: list[str] = []
    warnings: list[str] = []
    hard_requirement = instruction_hard_constraint_requirement(instruction or "")
    if required_atoms and missing_ratio > 0.40:
        blocking_reasons.append("required atom executable element missing ratio %.2f > 0.40" % missing_ratio)
    if missing_activation_triggers:
        blocking_reasons.append("condition/user_triggered nodes missing trigger material: " + ", ".join(missing_activation_triggers[:10]))
    if hard_total == 0 and hard_requirement.get("required"):
        msg = "hard_constraint_table is empty although original instruction contains explicit hard-boundary signals"
        if str(os.getenv("SCEG_BLOCK_EMPTY_HARD_WHEN_EXPLICIT", "0")).lower().strip() in {"1", "true", "yes", "on"}:
            blocking_reasons.append(msg)
        else:
            warnings.append(msg)
    elif hard_total == 0 and _graph_has_constraint_boundary(graph):
        warnings.append("hard_constraint_table is empty; graph has boundary-like wording, but original instruction did not prove a hard constraint")
    if hard_total and len(weak_hard) == hard_total:
        blocking_reasons.append("all hard constraints lack executable negative groups or metrics")
    if knowledge_total and len(weak_knowledge) == knowledge_total:
        blocking_reasons.append("all knowledge atoms lack executable selector/value rules")
    return {
        "passed": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "hard_constraint_required_by_instruction": hard_requirement,
        "required_atoms": required_atoms,
        "missing_atom_elements": missing_atom_elements[:30],
        "missing_atom_element_ratio": round(missing_ratio, 4),
        "missing_activation_triggers": missing_activation_triggers[:30],
        "knowledge_total": knowledge_total,
        "weak_knowledge": weak_knowledge[:30],
        "hard_constraint_total": hard_total,
        "weak_hard_constraints": weak_hard[:30],
    }


def _enforce_stage2_quality_gate(graph: dict[str, Any], instruction: str | None = None) -> dict[str, Any]:
    report = _stage2_quality_gate(graph, instruction)
    graph.setdefault("metadata", {})["stage2_element_quality_gate"] = report
    if not report.get("passed") and str(os.getenv("SCEG_ALLOW_WEAK_STAGE2", "0")).lower().strip() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("第二阶段 element细化质量门槛未通过：" + "; ".join(report.get("blocking_reasons") or []))
    return report

def build_graph_with_llm(
    instruction: str,
    project_root: str | Path,
    api_key: str,
    base_url: str | None,
    model: str | None,
    timeout: int | None = None,
    binding_hints: str | None = None,
    progress_callback=None,
    repair_mode: str = "required",
    refine_mode: str | None = None,
    use_cache: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # 第二阶段 element细化是必要建图阶段；外部传入的旧模式只保留兼容，不再改变执行分支。
    repair_mode = "required"
    root = Path(project_root)
    client = LLMClient(api_key=api_key, base_url=base_url, model=model, timeout=timeout)
    if not client.enabled():
        raise RuntimeError("缺少 LLM API Key，无法离线生成状态图。")

    def emit_phase(phase: str, event: str, message: str, **extra: Any) -> None:
        if progress_callback:
            rec = {"stage": phase, "phase": phase, "event": event, "message": message}
            rec.update(extra)
            progress_callback(rec)

    phase_timing: dict[str, Any] = {
        "llm_build_graph_seconds": None,
        "llm_element_refinement_seconds": None,
        "llm_element_refinement_triggered": False,
        "llm_element_expansion_seconds": None,
        "llm_element_expansion_triggered": False,
    }

    # New default build mode: one graph + two tables + atom-registry element passes.
    # LLM no longer creates a monolithic schema in one shot.  The local code
    # first assembles a clean graph/table skeleton, then builds a canonical atom
    # registry and asks LLM to generate elements only against those anchors.
    core_prompt = _load_stage_prompt(root, "schema_core_graph_prompt.md")
    knowledge_prompt = _load_stage_prompt(root, "schema_knowledge_table_prompt.md")
    constraint_prompt = _load_stage_prompt(root, "schema_constraint_tables_prompt.md")
    element_prompt = _load_stage_prompt(root, "schema_atom_element_refinement_prompt.md")
    element_expansion_prompt_text = _load_stage_prompt(root, "schema_element_expansion_prompt.md")
    # Graph/table generation must read only the complex instruction.
    # Dataset binding hints are intentionally ignored in the LLM build path.

    element_batch_size = _env_int("SCEG_ELEMENT_BATCH_SIZE", 8, minimum=1)

    cache_key = {
        "instruction": instruction,
        "core_prompt": core_prompt,
        "knowledge_prompt": knowledge_prompt,
        "constraint_prompt": constraint_prompt,
        "element_prompt": element_prompt,
        "element_expansion_prompt": element_expansion_prompt_text,
        "binding_hints": "ignored_for_independent_graph_table_generation",
        "model": model or client.model,
        "element_batch_size": element_batch_size,
        "schema_cache_signature": "method_memory_prompt_v10_element_quality_source_text",
    }
    cache_path = _graph_cache_path(root, cache_key)
    if use_cache and cache_path.exists():
        cached = read_json(cache_path)
        cached.setdefault("metadata", {})["llm_cache_hit"] = True
        cached["metadata"]["llm_cache_path"] = str(cache_path)
        phase_timing = {
            "llm_core_graph_seconds": 0.0,
            "llm_knowledge_table_seconds": "cached",
            "llm_constraint_tables_seconds": "cached",
            "llm_atom_element_refinement_seconds": "cached",
            "llm_element_expansion_seconds": "cached",
            # Compatibility aliases for existing UI/report code.
            "llm_build_graph_seconds": 0.0,
            "llm_element_refinement_seconds": "cached",
            "llm_element_refinement_triggered": True,
            "llm_element_expansion_triggered": True,
        }
        emit_phase("llm_core_graph", "skipped", "命中本地 LLM 图缓存：跳过主图生成")
        emit_phase("llm_knowledge_table", "skipped", "命中本地 LLM 图缓存：跳过知识表生成")
        emit_phase("llm_constraint_tables", "skipped", "命中本地 LLM 图缓存：跳过限制表生成")
        emit_phase("llm_atom_element_refinement", "skipped", "命中本地 LLM 图缓存：跳过一级元素生成")
        emit_phase("llm_element_expansion", "skipped", "命中本地 LLM 图缓存：跳过二级元素扩张")
        usage = {
            "total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
            "cache": {"hit": True, "path": str(cache_path)},
            "phase_timing_seconds": phase_timing,
        }
        cached["metadata"]["llm_phase_timing_seconds"] = phase_timing
        cached["metadata"]["llm_token_usage"] = usage
        return cached, usage

    phase_timing: dict[str, Any] = {
        "llm_core_graph_seconds": None,
        "llm_knowledge_table_seconds": None,
        "llm_constraint_tables_seconds": None,
        "llm_atom_element_refinement_seconds": None,
        "llm_element_expansion_seconds": None,
        # Compatibility aliases consumed by existing UI/report code.
        "llm_build_graph_seconds": None,
        "llm_element_refinement_seconds": None,
        "llm_element_refinement_triggered": True,
        "llm_element_expansion_triggered": False,
    }

    emit_phase("llm_core_graph", "start", "第一步 LLM 主图生成开始：只生成 nodes / atoms / edges，不生成知识表和限制表")
    t_core = time.perf_counter()
    raw_core = client.generate_json(instruction, core_prompt, purpose="core_graph_only")
    if str(os.getenv("SCEG_CORE_SUPPLEMENT", "on")).lower().strip() not in {"0", "off", "false", "skip"}:
        core_seed = strip_graph_core(_legacy_to_latest(_unwrap_stage_output(raw_core, "graph_core")))
        core_supp_payload = json.dumps({
            "task": "supplement_core_graph_only",
            "original_complex_instruction": instruction,
            "current_graph_core": core_seed,
            "local_supplement_hints": build_core_supplement_hints(instruction, core_seed),
            "return_contract": {
                "output": "full_corrected_graph_core",
                "allowed_top_level_keys": ["graph_id", "name", "metadata", "nodes", "edges", "relation_groups", "terminal_policies"],
                "hint_policy": "local_supplement_hints are task-agnostic gap hints only; use them to inspect missing graph functions, not to invent unsupported content",
                "do_not_output": ["knowledge_table", "hard_constraint_table", "soft_constraint_table", "constraint_table", "element_groups"],
            },
        }, ensure_ascii=False, separators=(",", ":"))
        emit_phase("llm_core_graph", "progress", "第一步二次补图开始：只读取复杂指令和当前主图，补漏主图结构")
        raw_core_supp = _safe_llm_stage_json(client, core_supp_payload, core_prompt, purpose="core_graph_supplement_only", default={}, emit_phase=emit_phase, phase="llm_core_graph", label="主图二次补图")
        if isinstance(raw_core_supp, dict) and (raw_core_supp.get("nodes") or isinstance(raw_core_supp.get("graph_core"), dict)):
            raw_core = _unwrap_stage_output(raw_core_supp, "graph_core")
    core_elapsed = time.perf_counter() - t_core
    phase_timing["llm_core_graph_seconds"] = round(core_elapsed, 3)
    phase_timing["llm_build_graph_seconds"] = round(core_elapsed, 3)
    emit_phase("llm_core_graph", "done", "第一步主图生成与二次补图完成，用时 %.1f 秒" % core_elapsed, elapsed_seconds=core_elapsed)

    graph_core = strip_graph_core(_legacy_to_latest(_unwrap_stage_output(raw_core, "graph_core")))
    graph_core = remove_old_runtime_tables(graph_core)
    compiled = compile_state_graph(graph_core, legacy_dialogue_root=None)
    compiled, lint_report = lint_and_repair_schema(compiled)
    compiled = remove_old_runtime_tables(compiled)

    knowledge_payload = json.dumps({
        "task": "generate_knowledge_table_only",
        "original_complex_instruction": instruction,
        "return_contract": {
            "top_level_key": "knowledge_table",
            "atom_fields": ["atom_id", "name", "text", "severity", "selector_groups", "correct_groups", "wrong_groups", "value_check", "negation_rule"],
            "independence": "read only original_complex_instruction; do not rely on graph_core or constraint tables",
        },
    }, ensure_ascii=False, separators=(",", ":"))
    emit_phase("llm_knowledge_table", "start", "第二步 LLM 知识表生成开始：只读复杂指令，只生成 knowledge_table")
    t_knowledge = time.perf_counter()
    raw_knowledge = client.generate_json(knowledge_payload, knowledge_prompt, purpose="knowledge_table_only")
    if str(os.getenv("SCEG_KNOWLEDGE_SUPPLEMENT", "on")).lower().strip() not in {"0", "off", "false", "skip"}:
        current_knowledge_table = raw_knowledge.get("knowledge_table") if isinstance(raw_knowledge, dict) else []
        knowledge_supp_payload = json.dumps({
            "task": "supplement_knowledge_table_only",
            "original_complex_instruction": instruction,
            "current_knowledge_table": current_knowledge_table,
            "local_supplement_hints": build_knowledge_supplement_hints(instruction, current_knowledge_table),
            "return_contract": {
                "output": "full_corrected_knowledge_table",
                "top_level_key": "knowledge_table",
                "hint_policy": "local_supplement_hints identify generic fact-slot shapes only; values must be grounded in original_complex_instruction",
                "independence": "read only original_complex_instruction and current_knowledge_table; do not read graph or constraints",
            },
        }, ensure_ascii=False, separators=(",", ":"))
        emit_phase("llm_knowledge_table", "progress", "第二步二次补表开始：只读取复杂指令和当前知识表，补漏知识 atom")
        raw_knowledge_supp = _safe_llm_stage_json(client, knowledge_supp_payload, knowledge_prompt, purpose="knowledge_table_supplement_only", default={"knowledge_table": raw_knowledge.get("knowledge_table") if isinstance(raw_knowledge, dict) else []}, emit_phase=emit_phase, phase="llm_knowledge_table", label="知识表二次补表")
        if _table_output_nonempty(raw_knowledge_supp, "knowledge_table"):
            raw_knowledge = raw_knowledge_supp
    knowledge_elapsed = time.perf_counter() - t_knowledge
    phase_timing["llm_knowledge_table_seconds"] = round(knowledge_elapsed, 3)
    emit_phase("llm_knowledge_table", "done", "第二步知识表生成与二次补表完成，用时 %.1f 秒" % knowledge_elapsed, elapsed_seconds=knowledge_elapsed)
    compiled = merge_knowledge_table(compiled, raw_knowledge)
    compiled, lint_report = lint_and_repair_schema(compiled)
    compiled = remove_old_runtime_tables(compiled)

    constraint_payload = json.dumps({
        "task": "generate_hard_and_soft_constraint_tables_only",
        "original_complex_instruction": instruction,
        "return_contract": {
            "top_level_keys": ["hard_constraint_table", "soft_constraint_table"],
            "hard_atom_fields": ["atom_id", "name", "text", "severity", "trigger_groups", "negative_groups", "safe_groups"],
            "structural_metric_fields": ["constraint_id", "name", "enforcement", "constraint_kind", "severity", "metric"],
            "independence": "read only original_complex_instruction; do not rely on graph_core or knowledge_table",
        },
    }, ensure_ascii=False, separators=(",", ":"))
    emit_phase("llm_constraint_tables", "start", "第三步 LLM 限制表生成开始：只读复杂指令，硬限制和软限制分表输出")
    t_constraints = time.perf_counter()
    raw_constraints = client.generate_json(constraint_payload, constraint_prompt, purpose="constraint_tables_only")
    if str(os.getenv("SCEG_CONSTRAINT_SUPPLEMENT", "on")).lower().strip() not in {"0", "off", "false", "skip"}:
        current_hard_constraint_table = raw_constraints.get("hard_constraint_table") if isinstance(raw_constraints, dict) else []
        current_soft_constraint_table = raw_constraints.get("soft_constraint_table") if isinstance(raw_constraints, dict) else []
        constraint_supp_payload = json.dumps({
            "task": "supplement_hard_and_soft_constraint_tables_only",
            "original_complex_instruction": instruction,
            "current_hard_constraint_table": current_hard_constraint_table,
            "current_soft_constraint_table": current_soft_constraint_table,
            "local_supplement_hints": build_constraint_supplement_hints(instruction, current_hard_constraint_table, current_soft_constraint_table),
            "return_contract": {
                "output": "patch_only_not_full_rewrite",
                "top_level_keys": ["hard_candidate_decisions", "add_hard_constraint_table", "add_soft_constraint_table", "remove_constraint_ids"],
                "max_new_hard": 3,
                "max_total_hard_after_merge": 10,
                "hint_policy": "local_supplement_hints identify generic boundary or quality shapes only; if required=true, each signal must have a hard_candidate_decision and empty hard patch is valid only when all signals are rejected with reasons",
                "independence": "read only original_complex_instruction and current constraint tables; do not read graph or knowledge table",
            },
        }, ensure_ascii=False, separators=(",", ":"))
        emit_phase("llm_constraint_tables", "progress", "第三步二次补表开始：只读取复杂指令和当前限制表，补漏硬/软限制")
        raw_constraints_supp = _safe_llm_stage_json(client, constraint_supp_payload, constraint_prompt, purpose="constraint_tables_supplement_only", default={"hard_constraint_table": raw_constraints.get("hard_constraint_table") if isinstance(raw_constraints, dict) else [], "soft_constraint_table": raw_constraints.get("soft_constraint_table") if isinstance(raw_constraints, dict) else []}, emit_phase=emit_phase, phase="llm_constraint_tables", label="限制表二次补表")
        raw_constraints = merge_constraint_supplement(raw_constraints, raw_constraints_supp, instruction)
    constraints_elapsed = time.perf_counter() - t_constraints
    phase_timing["llm_constraint_tables_seconds"] = round(constraints_elapsed, 3)
    emit_phase("llm_constraint_tables", "done", "第三步限制表生成与二次补表完成，用时 %.1f 秒" % constraints_elapsed, elapsed_seconds=constraints_elapsed)
    raw_constraints = sanitize_constraint_tables(raw_constraints, instruction)
    compiled = merge_constraint_tables(compiled, raw_constraints)
    # Hard constraints must be produced by LLM from the semantic prompt contract.
    # Do not synthesize business restrictions locally; empty hard tables are a prompt/model issue to fix, not a scoring fallback.
    compiled = assign_element_anchor_ids(compiled)
    compiled, lint_report = lint_and_repair_schema(compiled)
    compiled = normalize_executable_groups(remove_old_runtime_tables(assign_element_anchor_ids(compiled)))

    atom_transport = build_atom_transport(compiled, instruction)
    element_batches = _split_atom_transport(atom_transport, batch_size=element_batch_size)
    emit_phase("llm_atom_element_refinement", "start", "第四步 LLM 一级元素生成开始：基于本地 atom 传输层分批生成")
    t_elements = time.perf_counter()
    primary_runs: list[dict[str, Any]] = []
    for batch_idx, batch_registry in enumerate(element_batches, start=1):
        batch_meta = dict(batch_registry.get("batch") or {})
        element_payload = json.dumps({
            "task": "split_atom_semantics_into_elements",
            "atom_transport": batch_registry,
            "batch_contract": {
                "current_batch": batch_idx,
                "total_batches": len(element_batches),
                "only_return_atom_ids_in_current_batch": True,
            },
            "generation_contract": {
                "top_level_key": "element_refinements",
                "keyed_by": "atom_id",
                "input_visibility": "atom_id + atom_source + parent_id + atom_name + atom_text + requested_slots only",
                "element_shape": {"value": "short semantic phrase", "main": True, "fact": False, "pool": []},
                "role_aware_policy": {
                    "assistant_side": "先根据 atom_text 生成客服最可能的自然答话，再从这句话拆 element",
                    "user_trigger_side": "先生成大量可能用户说法，再抽触发 element；activation trigger 可以在第一阶段写入用户表达 pool",
                    "knowledge_side": "先生成客服最可能正确事实答话，再拆 selector/correct",
                    "hard_side": "分别想象违规客服说法和安全客服说法，再拆 negative/safe"
                },
            },
        }, ensure_ascii=False, separators=(",", ":"))
        emit_phase("llm_atom_element_refinement", "progress", "第四步一级元素生成批次 %d/%d：%s，%d 个 atom" % (batch_idx, len(element_batches), batch_meta.get("source_kind", "mixed"), batch_registry.get("entry_count", 0)))
        raw_elements = _safe_llm_stage_json(client, element_payload, element_prompt, purpose="atom_element_primary_%02d" % batch_idx, default={"element_refinements": []}, emit_phase=emit_phase, phase="llm_atom_element_refinement", label="一级元素批次 %d/%d" % (batch_idx, len(element_batches)))
        compiled = merge_element_anchor_delta(compiled, raw_elements, secondary_only=False)
        primary_runs.append({
            "batch_index": batch_idx,
            "batch_total": len(element_batches),
            "source_kind": batch_meta.get("source_kind"),
            "entry_count": batch_registry.get("entry_count", 0),
            "merge_policy": "primary_elements_by_atom_id_only",
        })
    elements_elapsed = time.perf_counter() - t_elements
    phase_timing["llm_atom_element_refinement_seconds"] = round(elements_elapsed, 3)
    phase_timing["llm_element_refinement_seconds"] = round(elements_elapsed, 3)
    emit_phase("llm_atom_element_refinement", "done", "第四步一级元素分批生成完成，共 %d 批，用时 %.1f 秒" % (len(element_batches), elements_elapsed), elapsed_seconds=elements_elapsed)
    compiled, lint_report = lint_and_repair_schema(compiled)
    compiled = remove_old_runtime_tables(assign_element_anchor_ids(compiled))
    compiled = _fill_minimal_element_fallbacks(compiled)
    compiled = remove_old_runtime_tables(assign_element_anchor_ids(compiled))
    stage2_quality_report = _enforce_stage2_quality_gate(compiled, instruction)

    expansion_runs: list[dict[str, Any]] = []
    if str(os.getenv("SCEG_ELEMENT_EXPANSION", "on")).lower().strip() not in {"0", "off", "false", "skip", "disabled"}:
        phase_timing["llm_element_expansion_triggered"] = True
        expansion_transport = build_atom_transport(compiled, instruction)
        expansion_batches = _split_atom_transport(expansion_transport, batch_size=element_batch_size)
        emit_phase("llm_element_expansion", "start", "第五步 LLM 二级元素扩张开始：按 atom 批次扩表达池，不改事实和结构")
        t_expand = time.perf_counter()
        for batch_idx, batch_registry in enumerate(expansion_batches, start=1):
            batch_meta = dict(batch_registry.get("batch") or {})
            expansion_payload = json.dumps({
                "task": "expand_element_pools_for_atom_batch",
                "atom_transport": batch_registry,
                "batch_contract": {
                    "current_batch": batch_idx,
                    "total_batches": len(expansion_batches),
                    "only_return_atom_ids_in_current_batch": True,
                },
                "generation_contract": {
                    "top_level_key": "secondary_expansions",
                    "keyed_by": "atom_id",
                    "task": "expand pool for existing elements only",
                    "element_shape": {"value": "existing value", "main": True, "fact": False, "pool": ["equivalent expressions"]},
                    "role_aware_pool_policy": {
                        "activation": "用户触发表达开放度高，每个 trigger element 尽量给 8-15 个严格同意图口语 pool",
                        "assistant": "客服侧表达趋同，每个 element 给 2-6 个等价说法即可",
                        "fact": "fact=true 只能扩格式等价，不能改数值、时间、极性或条件",
                        "coverage": "必须尽量覆盖每个已有 element；不能只返回少数示例"
                    },
                },
            }, ensure_ascii=False, separators=(",", ":"))
            emit_phase("llm_element_expansion", "progress", "第五步二级元素扩张批次 %d/%d：%s，%d 个 atom" % (batch_idx, len(expansion_batches), batch_meta.get("source_kind", "mixed"), batch_registry.get("entry_count", 0)))
            expanded_raw = _safe_llm_stage_json(client, expansion_payload, element_expansion_prompt_text, purpose="atom_element_secondary_%02d" % batch_idx, default={"secondary_expansions": []}, emit_phase=emit_phase, phase="llm_element_expansion", label="二级元素扩张批次 %d/%d" % (batch_idx, len(expansion_batches)))
            compiled = merge_element_anchor_delta(compiled, expanded_raw, secondary_only=True)
            expansion_runs.append({
                "expansion_source": "llm_api",
                "batch_index": batch_idx,
                "batch_total": len(expansion_batches),
                "source_kind": batch_meta.get("source_kind"),
                "entry_count": batch_registry.get("entry_count", 0),
                "merge_policy": "secondary_pools_by_atom_id_only",
            })
        expand_elapsed = time.perf_counter() - t_expand
        phase_timing["llm_element_expansion_seconds"] = round(expand_elapsed, 3)
        emit_phase("llm_element_expansion", "done", "第五步二级元素分批扩张完成，共 %d 批，用时 %.1f 秒" % (len(expansion_batches), expand_elapsed), elapsed_seconds=expand_elapsed)
        compiled, lint_report = lint_and_repair_schema(compiled)
        compiled = remove_old_runtime_tables(assign_element_anchor_ids(compiled))
        compiled = _fill_minimal_element_fallbacks(compiled)
        compiled = remove_old_runtime_tables(assign_element_anchor_ids(compiled))
    else:
        phase_timing["llm_element_expansion_seconds"] = "未触发"
        emit_phase("llm_element_expansion", "skipped", "第五步二级元素扩张未触发：环境变量已关闭")

    compiled = normalize_executable_groups(compiled)
    final_registry = build_atom_registry(compiled, instruction)
    compiled.setdefault("metadata", {})["schema_generation_mode"] = "one_graph_two_tables_atom_registry_elements"
    compiled["metadata"]["graph_source"] = "llm_one_graph_two_tables_atom_elements"
    compiled["metadata"]["llm_model"] = client.model
    compiled["metadata"]["schema_linter_report"] = lint_report
    compiled["metadata"]["stage2_element_quality_gate"] = stage2_quality_report
    compiled["metadata"]["schema_element_refinement_runs"] = [{
        "source": "llm_api",
        "mode": "atom_registry_primary_elements_batched",
        "elapsed_seconds": phase_timing["llm_atom_element_refinement_seconds"],
        "quality_gate": stage2_quality_report,
        "atom_registry_entry_count": final_registry.get("entry_count"),
        "batch_size": element_batch_size,
        "batches": primary_runs,
    }]
    compiled["metadata"]["schema_element_expansion_runs"] = expansion_runs
    compiled["metadata"]["atom_registry_summary"] = {k: final_registry.get(k) for k in ("schema_mode", "id_policy", "entry_count")}
    compiled["metadata"]["llm_phase_timing_seconds"] = phase_timing
    usage = client.usage_summary()
    usage["phase_timing_seconds"] = phase_timing
    usage.setdefault("cache", {"hit": False, "path": str(cache_path)})
    compiled["metadata"]["llm_token_usage"] = usage
    compiled["metadata"]["llm_cache_hit"] = False
    compiled["metadata"]["llm_cache_path"] = str(cache_path)
    if str(os.getenv("SCEG_GRAPH_LANGUAGE_GATE", "on")).lower().strip() not in {"0", "off", "false", "skip"}:
        assert_chinese_context(compiled)
    if use_cache:
        write_json(cache_path, compiled)
    return compiled, usage


def _dialogue_root_for_pack(base: Path, pack_type: str | None) -> Path:
    if pack_type == "positive":
        p = base / "positive_pack"
        return p if p.exists() else base
    if pack_type == "negative":
        p = base / "negative_pack"
        return p if p.exists() else base
    return base


def _collect_strings(value: Any, out: list[str]) -> None:
    """Collect schema/dialogue text without any task-specific lexicon."""
    if value is None:
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _collect_strings(v, out)
    elif isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text:
            out.append(text)


def _char_ngrams(text: str) -> set[str]:
    """Small, local compatibility signal used only to avoid cross-task mixing.

    It is not a business dictionary: the tokens are derived from the current
    LLM graph and the candidate dialogue files themselves.
    """
    compact = re.sub(r"\s+", "", text)
    compact = re.sub(r"[\W_]+", "", compact, flags=re.UNICODE)
    grams = {compact[i : i + 2] for i in range(max(0, len(compact) - 1))}
    grams.update(re.findall(r"[A-Za-z0-9$]+", text.lower()))
    return {x for x in grams if x}


def _schema_text(graph: StateGraph) -> str:
    parts: list[str] = [graph.graph_id, graph.name]
    _collect_strings(graph.metadata, parts)
    for node in graph.nodes:
        parts.extend([node.id, node.name])
        parts.extend(node.tags)
        parts.extend(node.aliases)
        for req in node.requirements:
            parts.extend([req.id, req.text])
            parts.extend(req.aliases)
            for group in req.evidence_groups:
                parts.extend([group.id, group.description])
                parts.extend(group.aliases)
                _collect_strings(group.patterns, parts)
    for k in graph.knowledge:
        parts.extend([k.id, k.name, k.judge_type])
        parts.extend(k.aliases)
        _collect_strings(k.expected, parts)
        _collect_strings(k.conflict_patterns, parts)
        _collect_strings(k.support_patterns, parts)
        _collect_strings([c.__dict__ if hasattr(c, "__dict__") else c for c in k.claims], parts)
    for c in graph.constraints:
        parts.extend([c.id, c.name, c.severity, c.description])
        parts.extend(c.aliases)
        _collect_strings(c.prohibited, parts)
        _collect_strings(c.safe_context, parts)
        _collect_strings(c.trigger, parts)
        _collect_strings(c.unresolved, parts)
    return " ".join(parts)


def _dialogue_text(dialogue: dict[str, Any]) -> str:
    parts: list[str] = [str(dialogue.get("id") or ""), str(dialogue.get("domain") or ""), str(dialogue.get("sample_type") or "")]
    _collect_strings(dialogue.get("expected_errors") or dialogue.get("expected_error") or dialogue.get("metadata") or {}, parts)
    for turn in dialogue.get("turns") or []:
        if isinstance(turn, dict):
            parts.append(str(turn.get("speaker") or ""))
            parts.append(str(turn.get("text") or turn.get("content") or ""))
        else:
            parts.append(str(turn))
    return " ".join(parts)


def _select_domain_by_instruction(dialogues: list[dict[str, Any]], instruction: str) -> dict[str, Any]:
    domains = sorted({str(d.get("domain") or "").strip() for d in dialogues if str(d.get("domain") or "").strip()})
    info: dict[str, Any] = {"method": "none", "selected_domain": None, "scores": {}, "domains_seen": domains}
    if not domains:
        return info
    if len(domains) == 1:
        info.update({"method": "single_domain", "selected_domain": domains[0]})
        return info
    i_grams = _char_ngrams(instruction)
    if not i_grams:
        return info
    scores: dict[str, float] = {}
    for domain in domains:
        subset = [d for d in dialogues if str(d.get("domain") or "").strip() == domain]
        sample_text = " ".join(_dialogue_text(d) for d in subset[:28])
        d_grams = _char_ngrams(sample_text)
        scores[domain] = len(i_grams & d_grams) / ((len(i_grams) ** 0.5) * (len(d_grams) ** 0.5)) if d_grams else 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    info["scores"] = {k: round(v, 4) for k, v in scores.items()}
    if ranked:
        best, best_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score >= 0.06 and (second <= 0.0001 or best_score >= second * 1.25):
            info.update({"method": "instruction_dialogue_overlap", "selected_domain": best, "best_score": round(best_score, 4), "second_score": round(second, 4)})
        else:
            info.update({"method": "ambiguous_instruction_overlap", "best_score": round(best_score, 4), "second_score": round(second, 4)})
    return info


def _clip_hint(value: Any, limit: int = 120) -> str:
    s = str(value or "").strip()
    return s if len(s) <= limit else s[: limit - 10] + "……"


def _sample_turn_texts(dialogue: dict[str, Any], speaker: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for turn in dialogue.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        if str(turn.get("speaker") or "") != speaker:
            continue
        text = _clip_hint(turn.get("text") or turn.get("content") or "", 120)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _build_binding_hints(dialogues: list[dict[str, Any]], selected_domain: str | None, max_items: int = 64) -> tuple[str, dict[str, Any]]:
    """Build compact, non-answer-key binding hints for LLM.

    These hints are *schema alignment anchors*, not business facts.  They never
    include dialogue turns, evidence spans, wrong statements, or injected
    negative answers.  Compared with the earlier row-by-row payload, this
    aggregates duplicate targets so rider-like packages do not send tens of
    thousands of characters to the first graph-building call.
    """
    if selected_domain:
        dialogues = [d for d in dialogues if str(d.get("domain") or "").strip() == selected_domain]

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_rows = 0

    def _append_unique(slot: dict[str, Any], key: str, value: Any, limit: int = 5) -> None:
        text = _clip_hint(value, 160).strip()
        if not text:
            return
        arr = slot.setdefault(key, [])
        if text not in arr and len(arr) < limit:
            arr.append(text)

    def add_row(row: dict[str, Any]) -> None:
        nonlocal raw_rows
        target_node = str(row.get("target_node_id") or "").strip()
        target_id = str(row.get("target_id") or "").strip()
        target_kind = str(row.get("target_kind") or row.get("error_family") or "").strip()
        if not target_node and not target_id:
            return
        raw_rows += 1
        group_key = (target_node, target_id, target_kind)
        slot = grouped.setdefault(
            group_key,
            {
                "target_node_id": target_node,
                "target_id": target_id,
                "target_kind": target_kind,
                "sample_types": [],
                "error_families": [],
                "source_nodes": [],
                "positive_designs": [],
                "count": 0,
            },
        )
        slot["count"] = int(slot.get("count") or 0) + 1
        _append_unique(slot, "sample_types", row.get("sample_type"), 4)
        _append_unique(slot, "error_families", row.get("error_family"), 6)
        _append_unique(slot, "source_nodes", row.get("source_node"), 5)
        if str(row.get("sample_type") or "") == "positive":
            _append_unique(slot, "positive_designs", row.get("source_positive_design"), 4)

    for d in dialogues:
        for err in d.get("injected_errors") or []:
            if not isinstance(err, dict):
                continue
            add_row({
                "target_node_id": str(err.get("node_id") or err.get("target_node_id") or d.get("target_node_id") or ""),
                "target_id": str(err.get("requirement_id") or err.get("knowledge_id") or err.get("constraint_id") or d.get("target_id") or ""),
                "source_node": str(d.get("source_node") or ""),
                "target_kind": str(d.get("target_kind") or ""),
                "error_family": str(err.get("error_family") or ""),
                "sample_type": str(d.get("sample_type") or ""),
            })
        for cov in d.get("coverage_targets") or []:
            if not isinstance(cov, dict):
                continue
            sample_type = str(d.get("sample_type") or "")
            add_row({
                "target_node_id": str(cov.get("node_id") or cov.get("target_node_id") or d.get("target_node_id") or ""),
                "target_id": str(cov.get("target_id") or d.get("target_id") or ""),
                "source_node": str(d.get("source_node") or ""),
                "target_kind": str(cov.get("target_kind") or d.get("target_kind") or ""),
                "error_family": str(d.get("source_error_type") or ""),
                "sample_type": sample_type,
                "source_positive_design": d.get("source_positive_design") if sample_type == "positive" else "",
            })

    rows = sorted(
        grouped.values(),
        key=lambda x: (
            0 if "positive" in x.get("sample_types", []) else 1,
            str(x.get("target_node_id") or ""),
            str(x.get("target_id") or ""),
            -int(x.get("count") or 0),
        ),
    )[:max_items]

    # Remove empty arrays to keep the prompt compact.
    compact_rows: list[dict[str, Any]] = []
    positive_design_rows = 0
    for r in rows:
        out = {k: v for k, v in r.items() if v not in ([], "", None)}
        if out.get("positive_designs"):
            positive_design_rows += 1
        compact_rows.append(out)

    info = {
        "selected_domain": selected_domain,
        "hint_count": len(compact_rows),
        "raw_hint_rows": raw_rows,
        "max_items": max_items,
        "format": "compact_grouped_targets_v2",
        "positive_design_hint_count": positive_design_rows,
    }
    if not compact_rows:
        return "", info
    import json as _json
    block = {"binding_hints": compact_rows}
    text = (
        "当前 data/dialogues 仅提供紧凑 schema 绑定锚点，不是业务规则或答案。\n"
        "请优先复用 target_node_id / target_id，无法复用时写入 aliases；positive_designs 只代表覆盖意图，不得当作唯一标准答案。\n"
        "禁止从负包错句、证据片段或样本文本推导业务事实。\n"
        + _json.dumps(block, ensure_ascii=False, separators=(",", ":"))
    )
    return text, info

def _domain_compatibility_filter(dialogues: list[dict[str, Any]], graph: StateGraph) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    domains = sorted({str(d.get("domain") or "").strip() for d in dialogues if str(d.get("domain") or "").strip()})
    info: dict[str, Any] = {"method": "none", "domains_seen": domains, "selected_domain": None, "scores": {}, "skipped": 0}
    if len(domains) <= 1:
        selected = domains[0] if domains else None
        info.update({"method": "single_domain" if selected else "none", "selected_domain": selected, "skipped": 0})
        return dialogues, info

    explicit_domain = str((graph.metadata or {}).get("domain") or "").strip()
    if explicit_domain:
        matched = [d for d in dialogues if str(d.get("domain") or "").strip() == explicit_domain]
        if matched:
            info.update({"method": "metadata.domain", "selected_domain": explicit_domain, "skipped": len(dialogues) - len(matched)})
            return matched, info

    g_grams = _char_ngrams(_schema_text(graph))
    if not g_grams:
        return dialogues, info
    scores: dict[str, float] = {}
    for domain in domains:
        subset = [d for d in dialogues if str(d.get("domain") or "").strip() == domain]
        sample_text = " ".join(_dialogue_text(d) for d in subset[:24])
        d_grams = _char_ngrams(sample_text)
        if not d_grams:
            scores[domain] = 0.0
            continue
        # Cosine-like overlap. It only decides whether a generated single graph
        # is being applied to clearly different task folders.
        scores[domain] = len(g_grams & d_grams) / ((len(g_grams) ** 0.5) * (len(d_grams) ** 0.5))
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    info["scores"] = {k: round(v, 4) for k, v in scores.items()}
    if not ranked:
        return dialogues, info
    best_domain, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    # Conservative threshold: only auto-filter when the schema is clearly closer
    # to one folder than the others. This avoids any task-name-specific routing.
    if best_score >= 0.06 and (second_score <= 0.0001 or best_score >= second_score * 1.45):
        matched = [d for d in dialogues if str(d.get("domain") or "").strip() == best_domain]
        info.update({
            "method": "schema_dialogue_overlap",
            "selected_domain": best_domain,
            "best_score": round(best_score, 4),
            "second_score": round(second_score, 4),
            "skipped": len(dialogues) - len(matched),
            "note": "检测到一个 LLM 状态图对应多个 domain 的对话目录，已按状态图与对话文本的结构兼容度自动保留最匹配的一组。",
        })
        graph.metadata["auto_selected_dialogue_domain"] = best_domain
        graph.metadata["dialogue_filter_method"] = "schema_dialogue_overlap"
        return matched, info
    info.update({
        "method": "ambiguous_overlap",
        "best_score": round(best_score, 4),
        "second_score": round(second_score, 4),
        "note": "发现多个 domain，但状态图兼容度差异不足，未自动过滤。建议指定对话目录或在 LLM 输出 metadata.domain。",
    })
    return dialogues, info


def _filter_dialogues(dialogues: list[dict[str, Any]], graph: StateGraph, pack_type: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before = len(dialogues)
    if pack_type:
        dialogues = [d for d in dialogues if str(d.get("sample_type") or "").lower() == pack_type]
    filtered, info = _domain_compatibility_filter(dialogues, graph)
    info["pack_type"] = pack_type
    info["count_before_pack_filter"] = before
    info["count_after_pack_filter"] = len(dialogues)
    info["count_after_domain_filter"] = len(filtered)
    return filtered, info


def _recompile_graph_for_selected_dialogues(
    graph_data: dict[str, Any],
    selected_root: Path,
    filter_info: dict[str, Any],
) -> tuple[dict[str, Any], StateGraph]:
    """Bind generated LLM schema to the selected dialogue package.

    LLM may generate fresh node IDs and a natural-language domain label on
    every run. The latest positive/negative package may still carry legacy
    injected_error ids. This pass is not a business dictionary: it recompiles
    the schema using only the selected package's metadata as aliases, so local
    negative-package validation can trace targets back to the generated graph.
    """
    selected_domain = str(filter_info.get("selected_domain") or "").strip()
    data = copy.deepcopy(graph_data)
    meta = data.setdefault("metadata", {})
    original_domain = str(meta.get("domain") or "")
    if selected_domain:
        if original_domain and original_domain != selected_domain:
            meta["llm_domain_label"] = original_domain
        meta["domain"] = selected_domain
        meta["dialogue_domain"] = selected_domain
    compiled = compile_state_graph(data, legacy_dialogue_root=selected_root)
    compiled, lint_report = lint_and_repair_schema(compiled)
    cmeta = compiled.setdefault("metadata", {})
    cmeta["dialogue_binding_method"] = "selected_package_metadata_aliases"
    cmeta["schema_linter_report"] = lint_report
    if selected_domain:
        cmeta["dialogue_domain"] = selected_domain
    return compiled, StateGraph.from_dict(compiled)


def _evaluate_records(graph: StateGraph, dialogues: list[dict[str, Any]], runtime: dict[str, Any], progress_callback=None) -> list[dict[str, Any]]:
    extractor = EvidenceExtractor()
    accepter = DatasetInterface(runtime)
    explainer = ReportExplainer()
    oracle_router = OracleRouter(runtime)
    records: list[dict[str, Any]] = []
    total = len(dialogues)
    for idx, dialogue in enumerate(dialogues, start=1):
        # Keep graph execution stateless across a large mixed pack.  The evaluator
        # is label-free and cheap to recreate; rebuilding it per dialogue avoids
        # cross-dialogue element/ledger growth from affecting later samples.
        evaluator = GraphEvaluator(copy.deepcopy(graph), copy.deepcopy(runtime), extractor)
        evaluation = evaluator.evaluate(dialogue)
        acceptance = accepter.accept(dialogue, evaluation)
        apply_dataset_score_adjustments(dialogue, evaluation, acceptance, runtime)
        oracle_candidates = oracle_router.build_candidates(evaluation, acceptance)
        explanation = explainer.explain(evaluation, acceptance, oracle_candidates)
        records.append(
            {
                "dialogue_id": evaluation.dialogue_id,
                "domain": dialogue.get("domain"),
                "sample_type": dialogue.get("sample_type"),
                "evaluation": evaluation.to_dict(),
                "acceptance": acceptance.to_dict(),
                "oracle_candidates": [x.to_dict() for x in oracle_candidates],
                "explanation": explanation,
                "runtime_version": runtime_version_info(),
            }
        )
        if progress_callback and (idx == 1 or idx == total or idx % max(1, total // 20 or 1) == 0):
            progress_callback({"stage": "evaluate", "current": idx, "total": total, "dialogue_count": total, "message": f"本地评估进度：{idx} / {total}"})
    return records


def run_project(
    instruction: str,
    project_root: str | Path,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_timeout: int | None = None,
    dialogue_root: str | Path | None = None,
    max_dialogues: int | None = None,
    pack_type: str | None = None,
    llm_verifier_mode: str | None = None,
    llm_verifier_max_items: int | None = None,
    report_mode: str = "simple",
    progress_callback=None,
    repair_mode: str = "required",
    refine_mode: str | None = None,
    use_graph_cache: bool = True,
) -> dict[str, Any]:
    # 第二阶段 element细化必跑；旧 refine_mode 参数只保留接口兼容。
    repair_mode = "required"
    root = Path(project_root).resolve()
    runs_dir = _ensure(root / "runs")
    graph_dir = _ensure(runs_dir / "graphs_llm")
    run_id = "llm_latest__" + _now_id()
    run_dir = _ensure(runs_dir / run_id)

    def emit(stage: str, current: int, total: int, message: str, **extra: Any) -> None:
        if progress_callback:
            rec = {"stage": stage, "current": current, "total": total, "message": message}
            rec.update(extra)
            progress_callback(rec)

    emit("load_dialogues", 1, 7, "正在读取最新正负包并准备 schema 绑定锚点")
    write_text(run_dir / "instruction.txt", instruction)
    base_dialogue_root = Path(dialogue_root).resolve() if dialogue_root else root / "data" / "dialogues"
    selected_root = _dialogue_root_for_pack(base_dialogue_root, pack_type)
    all_loaded_dialogues = load_dialogues(selected_root)
    hint_select_info = _select_domain_by_instruction(all_loaded_dialogues, instruction)
    binding_hints, binding_hint_info = _build_binding_hints(all_loaded_dialogues, hint_select_info.get("selected_domain"))
    binding_hint_info["domain_selection"] = hint_select_info
    binding_hint_info["runtime_version"] = runtime_version_info()
    write_json(run_dir / "schema_binding_hints_info.json", binding_hint_info)

    emit("build_graph", 2, 7, "正在用 LLM 离线生成 schema 状态图")
    debug_dir = _ensure(run_dir / "llm_debug")
    old_debug_dir = os.environ.get("SCEG_LLM_DEBUG_DIR")
    os.environ["SCEG_LLM_DEBUG_DIR"] = str(debug_dir)
    try:
        graph_data, token_usage = build_graph_with_llm(
            instruction,
            root,
            llm_api_key or "",
            llm_base_url,
            llm_model,
            timeout=llm_timeout,
            binding_hints=binding_hints,
            progress_callback=progress_callback,
            repair_mode=repair_mode,
            refine_mode="required",
            use_cache=use_graph_cache,
        )
    finally:
        if old_debug_dir is None:
            os.environ.pop("SCEG_LLM_DEBUG_DIR", None)
        else:
            os.environ["SCEG_LLM_DEBUG_DIR"] = old_debug_dir
    graph = StateGraph.from_dict(graph_data)
    graph_slug = _slug(graph.graph_id, "llm_graph")
    graph_path = graph_dir / f"{graph_slug}.json"
    write_json(graph_path, graph_data)
    write_json(run_dir / "graph.json", graph_data)

    emit("filter_dialogues", 3, 7, "正在按状态图匹配评估对话")
    dialogues, filter_info = _filter_dialogues(all_loaded_dialogues, graph, pack_type)

    # Recompile after the domain/task package has been selected. This solves the
    # common LLM issue where generated schema IDs differ from the injected
    # error IDs in the newest formal positive/negative package. The binding is
    # derived from selected package metadata only; no task words are embedded in code.
    graph_data, graph = _recompile_graph_for_selected_dialogues(graph_data, selected_root, filter_info)
    graph_slug = _slug(graph.graph_id, "llm_graph")
    graph_path = graph_dir / f"{graph_slug}.json"
    write_json(graph_path, graph_data)
    write_json(run_dir / "graph.json", graph_data)
    schema_linter_report = (graph_data.get("metadata") or {}).get("schema_linter_report") or {}
    write_json(run_dir / "schema_linter_report.json", schema_linter_report)
    filter_info["schema_linter_issue_count"] = schema_linter_report.get("issue_count", 0)
    filter_info["schema_linter_counts"] = schema_linter_report.get("counts", {})
    filter_info["graph_dialogue_binding"] = (graph_data.get("metadata") or {}).get("dialogue_binding_method")
    filter_info["graph_domain_after_binding"] = (graph_data.get("metadata") or {}).get("domain")
    filter_info["schema_binding_hint_count"] = binding_hint_info.get("hint_count")
    filter_info["preselected_hint_domain"] = (binding_hint_info.get("domain_selection") or {}).get("selected_domain")
    filter_info["runtime_version"] = runtime_version_info()
    write_json(run_dir / "dialogue_filter_info.json", filter_info)
    if max_dialogues:
        dialogues = dialogues[:max_dialogues]
        filter_info["max_dialogues"] = max_dialogues
        filter_info["count_after_limit"] = len(dialogues)
        write_json(run_dir / "dialogue_filter_info.json", filter_info)
    if not dialogues:
        write_json(run_dir / "dialogue_load_debug.json", {"dialogue_root": str(selected_root), "graph_domain": graph.metadata.get("domain"), "pack_type": pack_type, "filter_info": filter_info})
        raise RuntimeError("没有找到可评估的对话 JSON。请检查 data/dialogues 下的正负包，或确认 LLM 输出的 metadata.domain 与样本 domain 能对应。")

    runtime_path = root / "config" / "default_runtime.json"
    runtime = read_json(runtime_path)
    emit("evaluate", 4, 7, f"开始使用最新本地评估内核，共 {len(dialogues)} 条", dialogue_count=len(dialogues))
    records = _evaluate_records(graph, dialogues, runtime, progress_callback=progress_callback)

    emit("llm_verifier", 5, 7, "正在处理可选大模型二级判断")
    oracle_budget = runtime.get("oracle_budget", {}) if isinstance(runtime, dict) else {}
    if llm_verifier_max_items is not None and int(llm_verifier_max_items) < 0:
        llm_budget = -1
    else:
        llm_budget = llm_verifier_max_items if llm_verifier_max_items is not None else (oracle_budget.get("max_batch_candidates") or 36)
    llm_summary = apply_llm_verifier(
        records,
        api_key=llm_api_key or "",
        base_url=llm_base_url,
        model=llm_model,
        mode=llm_verifier_mode or "off",
        max_items=int(llm_budget) if llm_budget is not None else None,
    )

    reports_dir = _ensure(run_dir / "reports")
    for rec in records:
        write_json(reports_dir / f"{rec['dialogue_id']}.report.json", rec)
    write_json(run_dir / "llm_verifier_summary.json", llm_summary)

    emit("summaries", 6, 7, "正在写入结果 JSON")
    merged_path = run_dir / "all_reports_merged.json"
    write_json(merged_path, records)
    token_usage_path = run_dir / "run_token_usage.json"
    timing_path = run_dir / "run_timing_summary.json"
    llm_phase_timing = token_usage.get("phase_timing_seconds") or {}
    combined_usage = {
        "total": {
            "prompt_tokens": int((token_usage.get("total") or {}).get("prompt_tokens") or 0) + int(((llm_summary.get("token_usage") or {}).get("total") or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((token_usage.get("total") or {}).get("completion_tokens") or 0) + int(((llm_summary.get("token_usage") or {}).get("total") or {}).get("completion_tokens") or 0),
            "total_tokens": int((token_usage.get("total") or {}).get("total_tokens") or 0) + int(((llm_summary.get("token_usage") or {}).get("total") or {}).get("total_tokens") or 0),
            "calls": int((token_usage.get("total") or {}).get("calls") or 0) + int(((llm_summary.get("token_usage") or {}).get("total") or {}).get("calls") or 0),
        },
        "build_graph": token_usage,
        "llm_verifier": llm_summary.get("token_usage") or {},
    }
    write_json(token_usage_path, combined_usage)
    write_json(timing_path, {
        "runtime_version": runtime_version_info(),
        "llm_phase_timing_seconds": llm_phase_timing,
        "refine_mode": "required",
        "use_graph_cache": use_graph_cache,
        "notes": "LLM 分段计时：Pass 1 主图、Pass 2 知识表、Pass 3 硬软限制表、Pass 4 一级元素、Pass 5 二级元素扩张。",
    })

    emit("html_reports", 6, 7, "正在生成中文 HTML 报告")
    case_dir = _ensure(run_dir / "case_reports")
    for rec in records:
        case_name = _slug(rec.get("dialogue_id"), "dialogue") + ".html"
        rec["_detail_href"] = "case_reports/" + case_name
        write_text(case_dir / case_name, render_case_html(rec, mode="detail"))
    report_info = {"token_usage": combined_usage, "filter_info": filter_info, "schema_linter": schema_linter_report, "runtime_version": runtime_version_info()}
    html_simple = run_dir / "report_simple.html"
    html_detail = run_dir / "report_detail.html"
    write_text(html_simple, render_html(records, report_info, mode="simple"))
    write_text(html_detail, render_html(records, report_info, mode="detail"))
    html_path = html_detail if str(report_mode or "simple") == "detail" else html_simple
    write_text(run_dir / "report.html", render_html(records, report_info, mode=str(report_mode or "simple")))

    summary = {
        "run_id": run_id,
        "runtime_version": runtime_version_info(),
        "run_dir": str(run_dir),
        "graph_path": str(graph_path),
        "all_reports_merged": str(merged_path),
        "html_report": str(html_path),
        "html_report_simple": str(html_simple),
        "html_report_detail": str(html_detail),
        "selected_report_mode": report_mode,
        "run_token_usage": str(token_usage_path),
        "run_timing_summary": str(timing_path),
        "llm_verifier_summary": str(run_dir / "llm_verifier_summary.json"),
        "refine_mode": "required",
        "use_graph_cache": use_graph_cache,
        "llm_cache_hit": bool(((graph_data.get("metadata") or {}).get("llm_cache_hit"))),
        "dialogue_root": str(selected_root),
        "dialogue_count": len(records),
        "skipped_dialogue_count": max(0, len(all_loaded_dialogues) - len(dialogues)),
        "dialogue_filter_info": str(run_dir / "dialogue_filter_info.json"),
        "schema_linter_report": str(run_dir / "schema_linter_report.json"),
        "schema_binding_hints_info": str(run_dir / "schema_binding_hints_info.json"),
        "graph_source": "llm",
        "pack_filter": pack_type,
        "llm_verifier_summary": str(run_dir / "llm_verifier_summary.json"),
    }
    write_json(run_dir / "run_manifest.json", summary)

    emit("bundle", 7, 7, "正在打包本次运行结果")
    bundle_path = run_dir / "upload_bundle.zip"
    _zip_dir(run_dir, bundle_path)
    summary["upload_bundle"] = str(bundle_path)
    write_json(runs_dir / "latest_run.json", summary)
    return summary


def run_offline_project(
    graph_path: str | Path,
    project_root: str | Path,
    dialogue_root: str | Path | None = None,
    max_dialogues: int | None = None,
    pack_type: str | None = None,
    llm_verifier_mode: str | None = None,
    llm_verifier_max_items: int | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    report_mode: str = "detail",
    progress_callback=None,
) -> dict[str, Any]:
    """Evaluate dialogues with an existing graph JSON, without graph building.

    This is the offline demo path: it never calls LLM for graph generation.
    Optional LLM verification is still available after local evaluation when the
    caller explicitly provides a key and selects shadow/assist mode.
    """
    root = Path(project_root).resolve()
    graph_input = Path(graph_path).resolve()
    if not graph_input.exists():
        raise RuntimeError(f"离线状态图不存在：{graph_input}")

    runs_dir = _ensure(root / "runs")
    graph_dir = _ensure(runs_dir / "graphs_offline")
    run_id = "offline_graph_latest__" + _now_id()
    run_dir = _ensure(runs_dir / run_id)

    def emit(stage: str, current: int, total: int, message: str, **extra: Any) -> None:
        if progress_callback:
            rec = {"stage": stage, "current": current, "total": total, "message": message}
            rec.update(extra)
            progress_callback(rec)

    emit("load_graph", 1, 6, "正在读取离线状态图 JSON")
    raw_graph = read_json(graph_input)
    write_json(run_dir / "source_graph.json", raw_graph)
    raw_latest = _legacy_to_latest(raw_graph)
    compiled = compile_state_graph(raw_latest, legacy_dialogue_root=root / "data" / "dialogues")
    compiled, lint_report = lint_and_repair_schema(compiled)
    compiled.setdefault("metadata", {})["graph_source"] = "offline_graph"
    compiled["metadata"]["offline_graph_input"] = str(graph_input)
    compiled["metadata"]["schema_linter_report"] = lint_report
    graph = StateGraph.from_dict(compiled)

    emit("load_dialogues", 2, 6, "正在读取本地对话包")
    base_dialogue_root = Path(dialogue_root).resolve() if dialogue_root else root / "data" / "dialogues"
    selected_root = _dialogue_root_for_pack(base_dialogue_root, pack_type)
    all_loaded_dialogues = load_dialogues(selected_root)

    emit("filter_dialogues", 3, 6, "正在按离线图匹配对话包")
    dialogues, filter_info = _filter_dialogues(all_loaded_dialogues, graph, pack_type)
    compiled, graph = _recompile_graph_for_selected_dialogues(compiled, selected_root, filter_info)
    compiled.setdefault("metadata", {})["graph_source"] = "offline_graph"
    compiled["metadata"]["offline_graph_input"] = str(graph_input)
    compiled["metadata"]["runtime_version"] = runtime_version_info()
    graph_slug = _slug(graph.graph_id, "offline_graph")
    compiled_graph_path = graph_dir / f"{graph_slug}.json"
    write_json(compiled_graph_path, compiled)
    write_json(run_dir / "graph.json", compiled)

    schema_linter_report = (compiled.get("metadata") or {}).get("schema_linter_report") or {}
    filter_info["schema_linter_issue_count"] = schema_linter_report.get("issue_count", 0)
    filter_info["schema_linter_counts"] = schema_linter_report.get("counts", {})
    filter_info["graph_dialogue_binding"] = (compiled.get("metadata") or {}).get("dialogue_binding_method")
    filter_info["graph_domain_after_binding"] = (compiled.get("metadata") or {}).get("domain")
    filter_info["runtime_version"] = runtime_version_info()
    write_json(run_dir / "dialogue_filter_info.json", filter_info)
    write_json(run_dir / "schema_linter_report.json", schema_linter_report)

    if max_dialogues:
        dialogues = dialogues[:max_dialogues]
        filter_info["max_dialogues"] = max_dialogues
        filter_info["count_after_limit"] = len(dialogues)
        write_json(run_dir / "dialogue_filter_info.json", filter_info)
    if not dialogues:
        write_json(run_dir / "dialogue_load_debug.json", {"dialogue_root": str(selected_root), "graph_domain": graph.metadata.get("domain"), "pack_type": pack_type, "filter_info": filter_info})
        raise RuntimeError("没有找到可评估的对话 JSON。请检查对话目录或离线图 metadata.domain。")

    runtime_path = root / "config" / "default_runtime.json"
    runtime = read_json(runtime_path)
    emit("evaluate", 4, 6, f"开始离线评估，共 {len(dialogues)} 条", dialogue_count=len(dialogues))
    records = _evaluate_records(graph, dialogues, runtime, progress_callback=progress_callback)

    emit("llm_verifier", 5, 6, "正在处理可选大模型二级判断")
    oracle_budget = runtime.get("oracle_budget", {}) if isinstance(runtime, dict) else {}
    if llm_verifier_max_items is not None and int(llm_verifier_max_items) < 0:
        llm_budget = -1
    else:
        llm_budget = llm_verifier_max_items if llm_verifier_max_items is not None else (oracle_budget.get("max_batch_candidates") or 36)
    llm_summary = apply_llm_verifier(
        records,
        api_key=llm_api_key or "",
        base_url=llm_base_url,
        model=llm_model,
        mode=llm_verifier_mode or "off",
        max_items=int(llm_budget) if llm_budget is not None else None,
    )

    reports_dir = _ensure(run_dir / "reports")
    for rec in records:
        write_json(reports_dir / f"{rec['dialogue_id']}.report.json", rec)
    write_json(run_dir / "llm_verifier_summary.json", llm_summary)

    emit("summaries", 6, 6, "正在写入离线评估结果")
    merged_path = run_dir / "all_reports_merged.json"
    write_json(merged_path, records)
    combined_usage = {
        "total": ((llm_summary.get("token_usage") or {}).get("total") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}),
        "build_graph": {"offline_graph": True, "calls": 0},
        "llm_verifier": llm_summary.get("token_usage") or {},
    }
    token_usage_path = run_dir / "run_token_usage.json"
    timing_path = run_dir / "run_timing_summary.json"
    write_json(token_usage_path, combined_usage)
    write_json(timing_path, {"runtime_version": runtime_version_info(), "offline_graph": True, "notes": "本次运行直接读取离线状态图，不执行指令建图。"})

    case_dir = _ensure(run_dir / "case_reports")
    for rec in records:
        case_name = _slug(rec.get("dialogue_id"), "dialogue") + ".html"
        rec["_detail_href"] = "case_reports/" + case_name
        write_text(case_dir / case_name, render_case_html(rec, mode="detail"))
    report_info = {"token_usage": combined_usage, "filter_info": filter_info, "schema_linter": schema_linter_report, "runtime_version": runtime_version_info(), "offline_graph": True}
    html_simple = run_dir / "report_simple.html"
    html_detail = run_dir / "report_detail.html"
    write_text(html_simple, render_html(records, report_info, mode="simple"))
    write_text(html_detail, render_html(records, report_info, mode="detail"))
    html_path = html_detail if str(report_mode or "detail") == "detail" else html_simple
    write_text(run_dir / "report.html", render_html(records, report_info, mode=str(report_mode or "detail")))

    summary = {
        "run_id": run_id,
        "runtime_version": runtime_version_info(),
        "run_dir": str(run_dir),
        "graph_path": str(compiled_graph_path),
        "offline_graph_input": str(graph_input),
        "all_reports_merged": str(merged_path),
        "html_report": str(html_path),
        "html_report_simple": str(html_simple),
        "html_report_detail": str(html_detail),
        "selected_report_mode": report_mode,
        "run_token_usage": str(token_usage_path),
        "run_timing_summary": str(timing_path),
        "llm_verifier_summary": str(run_dir / "llm_verifier_summary.json"),
        "dialogue_root": str(selected_root),
        "dialogue_count": len(records),
        "skipped_dialogue_count": max(0, len(all_loaded_dialogues) - len(dialogues)),
        "dialogue_filter_info": str(run_dir / "dialogue_filter_info.json"),
        "schema_linter_report": str(run_dir / "schema_linter_report.json"),
        "graph_source": "offline_graph",
        "pack_filter": pack_type,
    }
    write_json(run_dir / "run_manifest.json", summary)
    emit("bundle", 6, 6, "正在打包离线评估结果")
    bundle_path = run_dir / "upload_bundle.zip"
    _zip_dir(run_dir, bundle_path)
    summary["upload_bundle"] = str(bundle_path)
    write_json(runs_dir / "latest_offline_run.json", summary)
    write_json(runs_dir / "latest_run.json", summary)
    return summary
