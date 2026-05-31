from __future__ import annotations

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
from .longcat_client import LongCatClient
from .llm_verifier import apply_llm_verifier
from .oracle_router import OracleRouter
from .report_explainer import ReportExplainer
from .report_html import render_case_html, render_html
from .schema import StateGraph
from .version import CORE_VERSION, runtime_version_info
from .score_adjuster import apply_dataset_score_adjustments
from .schema_compiler import compile_state_graph
from .schema_linter import lint_and_repair_schema
from .schema_repair_audit import audit_schema_repair_need, build_repair_instruction


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
    cache_dir = root / "runs" / "graphs_longcat" / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / ("graph_" + _stable_hash(key_data) + ".json")


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
        mode = str(activation.get("mode") or "start")
        latest_mode = "always" if mode in {"start", "always"} else "user_triggered"
        trigger_texts = []
        for key in ("seed_intents", "positive_examples"):
            trigger_texts.extend(activation.get(key) or [])
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
    for idx, item in enumerate(raw.get("constraint_table", []) or [], start=1):
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
                "trigger": item.get("trigger") if isinstance(item.get("trigger"), list) else [],
                "safe_context": item.get("safe_context") or [],
                "prohibited": prohibited,
                "unresolved": item.get("unresolved") or item.get("grey_zone") or [],
                "requires_resolution": bool(item.get("requires_resolution", False)),
            }
        )
    return {
        "graph_id": str(raw.get("graph_id") or raw.get("flow_id") or "longcat_graph"),
        "name": str(raw.get("name") or raw.get("flow_id") or "LongCat 状态图"),
        "metadata": {"domain": raw.get("domain") or (raw.get("metadata") or {}).get("domain") or "general", "generated_by": "longcat"},
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
    raise RuntimeError(f"缺少 LongCat 提示词：{prompt_path}")


def build_graph_with_longcat(
    instruction: str,
    project_root: str | Path,
    api_key: str,
    base_url: str | None,
    model: str | None,
    timeout: int | None = None,
    binding_hints: str | None = None,
    progress_callback=None,
    repair_mode: str = "blocking",
    use_cache: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(project_root)
    client = LongCatClient(api_key=api_key, base_url=base_url, model=model, timeout=timeout)
    if not client.enabled():
        raise RuntimeError("缺少 LongCat API Key，无法离线生成状态图。")

    def emit_phase(phase: str, event: str, message: str, **extra: Any) -> None:
        if progress_callback:
            rec = {"stage": phase, "phase": phase, "event": event, "message": message}
            rec.update(extra)
            progress_callback(rec)

    phase_timing: dict[str, Any] = {
        "longcat_build_graph_seconds": None,
        "longcat_repair_graph_seconds": None,
        "longcat_repair_triggered": False,
    }

    prompt = _load_prompt(root)
    repair_prompt_text = _load_prompt(root, "schema_graph_repair_prompt.md")
    if binding_hints:
        prompt += "\n\n" + binding_hints

    cache_key = {
        "instruction": instruction,
        "build_prompt": prompt,
        "repair_prompt": repair_prompt_text,
        "binding_hints": binding_hints or "",
        "model": model or client.model,
        "repair_mode": str(repair_mode or "blocking"),
        "schema_cache_version": "fix68_cache_after_compiler_and_oracle_payload_v1",
    }
    cache_path = _graph_cache_path(root, cache_key)
    if use_cache and cache_path.exists():
        cached = read_json(cache_path)
        cached.setdefault("metadata", {})["longcat_cache_hit"] = True
        cached["metadata"]["longcat_cache_path"] = str(cache_path)
        phase_timing["longcat_build_graph_seconds"] = 0.0
        phase_timing["longcat_repair_graph_seconds"] = "cached"
        phase_timing["longcat_repair_triggered"] = bool(((cached.get("metadata") or {}).get("schema_repair_runs") or []))
        emit_phase("longcat_build_graph", "skipped", "命中本地 LongCat 图缓存：跳过第一次建图")
        emit_phase("longcat_repair_graph", "skipped", "命中本地 LongCat 图缓存：跳过二次补图")
        usage = {
            "total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
            "cache": {"hit": True, "path": str(cache_path)},
            "phase_timing_seconds": phase_timing,
        }
        cached["metadata"]["longcat_phase_timing_seconds"] = phase_timing
        cached["metadata"]["longcat_token_usage"] = usage
        return cached, usage

    emit_phase("longcat_build_graph", "start", "第一次 LongCat 建图开始：生成状态主图、知识表和限制表")
    t_build = time.perf_counter()
    raw = client.generate_json(instruction, prompt, purpose="build_graph")
    build_elapsed = time.perf_counter() - t_build
    phase_timing["longcat_build_graph_seconds"] = round(build_elapsed, 3)
    emit_phase("longcat_build_graph", "done", "第一次 LongCat 建图完成，用时 %.1f 秒" % build_elapsed, elapsed_seconds=build_elapsed)

    raw_latest = _legacy_to_latest(raw)
    compiled = compile_state_graph(raw_latest, legacy_dialogue_root=root / "data" / "dialogues")
    compiled, lint_report = lint_and_repair_schema(compiled)

    # Generic LLM补图链路：本地只做 schema gap 审计，不直接写入业务事实。
    # 如果 ID 绑定、知识表、限制表或终止策略有缺口，把审计结果和当前图交给
    # LongCat 二次生成完整 schema。无 API 的测试环境可以用
    # SCEG_SIMULATED_LONGCAT_DIR 回放“模拟 LongCat 返回”，但生产路径仍然是
    # LongCat 调用，而不是本地补图。
    repair_audit = audit_schema_repair_need(compiled, binding_hints, repair_mode=repair_mode)
    repair_runs: list[dict[str, Any]] = []
    if repair_audit.get("needs_repair"):
        phase_timing["longcat_repair_triggered"] = True
        repair_prompt = repair_prompt_text
        repair_payload = build_repair_instruction(instruction, compiled, repair_audit, binding_hints)
        emit_phase("longcat_repair_graph", "start", "第二次 LongCat repair 建图开始：根据 schema gap 审计补全图结构")
        t_repair = time.perf_counter()
        repaired_raw = client.generate_json(repair_payload, repair_prompt, purpose="repair_schema_by_audit")
        repair_elapsed = time.perf_counter() - t_repair
        phase_timing["longcat_repair_graph_seconds"] = round(repair_elapsed, 3)
        emit_phase("longcat_repair_graph", "done", "第二次 LongCat repair 建图完成，用时 %.1f 秒" % repair_elapsed, elapsed_seconds=repair_elapsed)
        repaired_latest = _legacy_to_latest(repaired_raw)
        compiled = compile_state_graph(repaired_latest, legacy_dialogue_root=root / "data" / "dialogues")
        compiled, lint_report = lint_and_repair_schema(compiled)
        repair_runs.append({"audit_before_repair": repair_audit, "repair_source": "longcat_or_simulated_longcat", "elapsed_seconds": round(repair_elapsed, 3)})
        repair_audit = audit_schema_repair_need(compiled, binding_hints, repair_mode=repair_mode)
    else:
        if str(repair_mode or "blocking").lower().strip() in {"off", "none", "skip", "disabled"}:
            msg = "第二次 LongCat repair 建图未触发：已选择只建一次/跳过补图"
        elif repair_audit.get("quality_repair_needed") and repair_audit.get("quality_warnings_kept_as_advisory"):
            msg = "第二次 LongCat repair 建图未触发：快速模式只补硬缺口，质量提示已保留为 advisory"
        else:
            msg = "第二次 LongCat repair 建图未触发：schema gap 审计未发现必须补图项"
        emit_phase("longcat_repair_graph", "skipped", msg)

    compiled.setdefault("metadata", {})["graph_source"] = "longcat_with_schema_repair" if repair_runs else "longcat"
    compiled["metadata"]["longcat_model"] = client.model
    compiled["metadata"]["schema_linter_report"] = lint_report
    compiled["metadata"]["schema_repair_audit"] = repair_audit
    compiled["metadata"]["schema_repair_runs"] = repair_runs
    compiled["metadata"]["longcat_phase_timing_seconds"] = phase_timing
    usage = client.usage_summary()
    usage["phase_timing_seconds"] = phase_timing
    usage.setdefault("cache", {"hit": False, "path": str(cache_path)})
    compiled["metadata"]["longcat_token_usage"] = usage
    compiled["metadata"]["longcat_cache_hit"] = False
    compiled["metadata"]["longcat_cache_path"] = str(cache_path)
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
    LongCat graph and the candidate dialogue files themselves.
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
    """Build compact, non-answer-key binding hints for LongCat.

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
            "note": "检测到一个 LongCat 状态图对应多个 domain 的对话目录，已按状态图与对话文本的结构兼容度自动保留最匹配的一组。",
        })
        graph.metadata["auto_selected_dialogue_domain"] = best_domain
        graph.metadata["dialogue_filter_method"] = "schema_dialogue_overlap"
        return matched, info
    info.update({
        "method": "ambiguous_overlap",
        "best_score": round(best_score, 4),
        "second_score": round(second_score, 4),
        "note": "发现多个 domain，但状态图兼容度差异不足，未自动过滤。建议指定对话目录或在 LongCat 输出 metadata.domain。",
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
    """Bind generated LongCat schema to the selected dialogue package.

    LongCat may generate fresh node IDs and a natural-language domain label on
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
            meta["longcat_domain_label"] = original_domain
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
    evaluator = GraphEvaluator(graph, runtime, extractor)
    accepter = DatasetInterface(runtime)
    explainer = ReportExplainer()
    oracle_router = OracleRouter(runtime)
    records: list[dict[str, Any]] = []
    total = len(dialogues)
    for idx, dialogue in enumerate(dialogues, start=1):
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
    longcat_api_key: str | None = None,
    longcat_base_url: str | None = None,
    longcat_model: str | None = None,
    longcat_timeout: int | None = None,
    dialogue_root: str | Path | None = None,
    max_dialogues: int | None = None,
    pack_type: str | None = None,
    llm_verifier_mode: str | None = None,
    llm_verifier_max_items: int | None = None,
    report_mode: str = "simple",
    progress_callback=None,
    repair_mode: str = "blocking",
    use_graph_cache: bool = True,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    runs_dir = _ensure(root / "runs")
    graph_dir = _ensure(runs_dir / "graphs_longcat")
    run_id = "longcat_latest__" + _now_id()
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

    emit("build_graph", 2, 7, "正在用 LongCat 离线生成 schema 状态图")
    debug_dir = _ensure(run_dir / "longcat_debug")
    old_debug_dir = os.environ.get("SCEG_LONGCAT_DEBUG_DIR")
    os.environ["SCEG_LONGCAT_DEBUG_DIR"] = str(debug_dir)
    try:
        graph_data, token_usage = build_graph_with_longcat(
            instruction,
            root,
            longcat_api_key or "",
            longcat_base_url,
            longcat_model,
            timeout=longcat_timeout,
            binding_hints=binding_hints,
            progress_callback=progress_callback,
            repair_mode=repair_mode,
            use_cache=use_graph_cache,
        )
    finally:
        if old_debug_dir is None:
            os.environ.pop("SCEG_LONGCAT_DEBUG_DIR", None)
        else:
            os.environ["SCEG_LONGCAT_DEBUG_DIR"] = old_debug_dir
    graph = StateGraph.from_dict(graph_data)
    graph_slug = _slug(graph.graph_id, "longcat_graph")
    graph_path = graph_dir / f"{graph_slug}.json"
    write_json(graph_path, graph_data)
    write_json(run_dir / "graph.json", graph_data)

    emit("filter_dialogues", 3, 7, "正在按状态图匹配评估对话")
    dialogues, filter_info = _filter_dialogues(all_loaded_dialogues, graph, pack_type)

    # Recompile after the domain/task package has been selected. This solves the
    # common LongCat issue where generated schema IDs differ from the injected
    # error IDs in the newest formal positive/negative package. The binding is
    # derived from selected package metadata only; no task words are embedded in code.
    graph_data, graph = _recompile_graph_for_selected_dialogues(graph_data, selected_root, filter_info)
    graph_slug = _slug(graph.graph_id, "longcat_graph")
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
        raise RuntimeError("没有找到可评估的对话 JSON。请检查 data/dialogues 下的正负包，或确认 LongCat 输出的 metadata.domain 与样本 domain 能对应。")

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
        api_key=longcat_api_key or "",
        base_url=longcat_base_url,
        model=longcat_model,
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
    longcat_phase_timing = token_usage.get("phase_timing_seconds") or {}
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
        "longcat_phase_timing_seconds": longcat_phase_timing,
        "repair_mode": repair_mode,
        "use_graph_cache": use_graph_cache,
        "notes": "LongCat 分段计时：第一次为初始 schema 建图；第二次只在所选 repair_mode 需要时补图。快速模式只补硬缺口，质量提示保留为 advisory。",
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
        "repair_mode": repair_mode,
        "use_graph_cache": use_graph_cache,
        "longcat_cache_hit": bool(((graph_data.get("metadata") or {}).get("longcat_cache_hit"))),
        "dialogue_root": str(selected_root),
        "dialogue_count": len(records),
        "skipped_dialogue_count": max(0, len(all_loaded_dialogues) - len(dialogues)),
        "dialogue_filter_info": str(run_dir / "dialogue_filter_info.json"),
        "schema_linter_report": str(run_dir / "schema_linter_report.json"),
        "schema_binding_hints_info": str(run_dir / "schema_binding_hints_info.json"),
        "graph_source": "longcat",
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
    longcat_api_key: str | None = None,
    longcat_base_url: str | None = None,
    longcat_model: str | None = None,
    report_mode: str = "detail",
    progress_callback=None,
) -> dict[str, Any]:
    """Evaluate dialogues with an existing graph JSON, without graph building.

    This is the offline demo path: it never calls LongCat for graph generation.
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
        api_key=longcat_api_key or "",
        base_url=longcat_base_url,
        model=longcat_model,
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
