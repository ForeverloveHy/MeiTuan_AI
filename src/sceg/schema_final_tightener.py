from __future__ import annotations

"""Final schema tightening guards for LLM-generated graphs.

The functions here are deliberately domain-neutral.  They only repair structural
contracts that are unsafe regardless of business domain: conditional nodes inside
required sequences, terminal nodes without user triggers, busy-return edges,
information-acquisition wording, duplicate hard rows, and missing hard rows that
can be derived from the graph's own facts.
"""

import copy
import hashlib
import re
from typing import Any


_COND_TYPES = {"branch", "faq", "out_of_scope"}
_COND_MODES = {"condition", "user_triggered"}
_BUSY_RE = re.compile(r"忙|没空|不方便|没时间|稍后")
_TERMINAL_STATE_RE = re.compile(r"开车|驾驶|骑行|危险|不方便|坚持|无法|没法|不能|不愿|不想|拒绝")
_INFO_RE = re.compile(r"确认|询问|核实|了解|是否|有没有|能否|可否")
_ADVISORY_RE = re.compile(r"规则|机制|影响|有助于|减少|解释|原因|建议|提醒")
_ABSTRACT_ELEM_RE = re.compile(r"^(问题|情况|处理|规则|信息|内容|事情|相关|其他问题|知识库|确认|可以|不可|忙碌)$")


def tighten_graph_contracts(graph: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(graph)
    meta = out.setdefault("metadata", {})
    audit: list[dict[str, Any]] = []
    _sync_node_types(out, audit)
    _repair_required_sequences(out, audit)
    _repair_terminal_triggers(out, audit)
    _repair_busy_edges(out, audit)
    _demote_optional_advisory_main(out, audit)
    _tighten_information_atoms(out, audit)
    _prune_abstract_trigger_main(out, audit)
    _dedupe_hard_rows(out, audit)
    _derive_hard_rows_from_graph_facts(out, audit)
    if audit:
        meta.setdefault("final_tightener_audit", []).extend(audit[:80])
    return out


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "").strip()


def _node_kind(node: dict[str, Any]) -> str:
    return str(node.get("node_type") or node.get("type") or "").strip().lower()


def _mode(node: dict[str, Any]) -> str:
    act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    return str(act.get("mode") or "").strip().lower()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return str(value)


def _compact(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _stable(prefix: str, seed: str) -> str:
    return f"{prefix}_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:8]}"


def _elements(*pairs: tuple[str, bool, bool]) -> dict[str, Any]:
    return {"elements": [{"value": v, "main": m, "fact": f, "pool": []} for v, m, f in pairs if str(v or "").strip()]}


def _has_trigger_groups(node: dict[str, Any]) -> bool:
    act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    for key in ("trigger_groups", "element_groups", "trigger_element_groups"):
        val = act.get(key)
        if isinstance(val, list) and any(isinstance(g, dict) and g.get("elements") for g in val):
            return True
    return False


def _trigger_hint(node: dict[str, Any]) -> str:
    act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    return _compact(act.get("trigger_hint") or node.get("name") or "")


def _strip_actor_words(text: str) -> str:
    text = re.sub(r"^(用户|对方|客户|老板|老师|负责人|本人)表示", "", text)
    text = re.sub(r"^(用户|对方|客户|老板|老师|负责人|本人)", "", text)
    text = text.replace("状态", "").replace("问题", "").strip(" ：:，,。")
    return text or "不方便"


def _make_user_text_groups(hint: str) -> list[dict[str, Any]]:
    phrase = _strip_actor_words(hint)
    # Keep this small and generic; the expansion prompt can create richer pools.
    texts = [
        f"我现在{phrase}",
        f"这边{phrase}",
        f"确实{phrase}",
        f"现在不太方便，{phrase}",
    ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in texts:
        src = _compact(src)
        if src in seen:
            continue
        seen.add(src)
        val = phrase if len(phrase) <= 18 else phrase[:18]
        out.append({"source_text": src, "elements": [{"value": val, "main": True, "fact": False, "pool": [val]}]})
    return out


def _required_sequential_node_ids(graph: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for rg in graph.get("relation_groups") or []:
        if not isinstance(rg, dict):
            continue
        typ = str(rg.get("type") or rg.get("relation") or "").lower()
        if typ == "sequential" and bool(rg.get("required")):
            for nid in rg.get("nodes") or []:
                ids.add(str(nid))
    return ids


def _sync_node_types(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    required_seq = _required_sequential_node_ids(graph)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = _node_id(node)
        if nid in required_seq and _node_kind(node) == "main" and _mode(node) in _COND_MODES:
            act = node.setdefault("activation", {})
            act["mode"] = "always"
            act.pop("trigger_groups", None)
            node["type"] = "main"
            node["node_type"] = "main"
            node["required"] = True
            audit.append({"type": "main_node_in_required_sequence_forced_always", "node_id": nid})


def _repair_required_sequences(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    by_id = {_node_id(n): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
    for rg in graph.get("relation_groups") or []:
        if not isinstance(rg, dict):
            continue
        typ = str(rg.get("type") or rg.get("relation") or "").lower()
        if typ != "sequential" or not bool(rg.get("required")):
            continue
        keep: list[str] = []
        removed: list[str] = []
        for nid in [str(x) for x in (rg.get("nodes") or [])]:
            n = by_id.get(nid)
            if not isinstance(n, dict):
                keep.append(nid)
                continue
            if _node_kind(n) in _COND_TYPES or _mode(n) in _COND_MODES:
                removed.append(nid)
            else:
                keep.append(nid)
        if removed and keep:
            rg["nodes"] = keep
            audit.append({"type": "conditional_removed_from_required_sequence", "relation_group_id": rg.get("id") or rg.get("group_id"), "node_ids": removed})


def _repair_terminal_triggers(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        kind = _node_kind(node)
        hint = _trigger_hint(node)
        if kind == "terminal" and _mode(node) in {"optional", ""} and _TERMINAL_STATE_RE.search(hint) and not _has_trigger_groups(node):
            act = node.setdefault("activation", {})
            act["mode"] = "condition"
            act["trigger_groups"] = _make_user_text_groups(hint)
            audit.append({"type": "terminal_trigger_groups_added", "node_id": _node_id(node), "hint": hint})


def _repair_busy_edges(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    by_id = {_node_id(n): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = by_id.get(str(edge.get("source") or ""))
        target = by_id.get(str(edge.get("target") or ""))
        if not source or not target:
            continue
        source_text = _text(source)
        if str(edge.get("type") or edge.get("relation") or "").lower() == "suppress_after" and _BUSY_RE.search(source_text) and _node_kind(target) == "main":
            edge["type"] = "required_after"
            edge["relation"] = "required_after"
            audit.append({"type": "busy_edge_returned_to_main", "source": edge.get("source"), "target": edge.get("target")})


def _demote_optional_advisory_main(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    by_id = {_node_id(n): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
    incoming: dict[str, set[str]] = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        target = str(edge.get("target") or "")
        rel = str(edge.get("type") or edge.get("relation") or "").lower()
        incoming.setdefault(target, set()).add(rel)
    demoted: set[str] = set()
    for nid, node in by_id.items():
        rels = incoming.get(nid, set())
        if _node_kind(node) == "main" and bool(node.get("required")) and "optional_after" in rels and "required_after" in rels and _ADVISORY_RE.search(_text(node)):
            node["required"] = False
            demoted.add(nid)
            audit.append({"type": "advisory_main_demoted_to_optional", "node_id": nid})
    if not demoted:
        return
    for rg in graph.get("relation_groups") or []:
        if not isinstance(rg, dict):
            continue
        typ = str(rg.get("type") or rg.get("relation") or "").lower()
        if typ == "sequential" and bool(rg.get("required")):
            nodes = [str(x) for x in (rg.get("nodes") or [])]
            new_nodes = [x for x in nodes if x not in demoted]
            if new_nodes and len(new_nodes) != len(nodes):
                rg["nodes"] = new_nodes
                audit.append({"type": "advisory_main_removed_from_required_sequence", "relation_group_id": rg.get("id") or rg.get("group_id"), "node_ids": sorted(demoted)})


def _tighten_information_atoms(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for atom in node.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            text = str(atom.get("text") or "")
            if _INFO_RE.search(text) and "用户已提供" not in text and "已提供" not in text:
                atom["text"] = "确认或根据用户已提供信息获取：" + text
                audit.append({"type": "information_atom_accepts_user_provided_state", "node_id": _node_id(node), "atom_id": atom.get("id") or atom.get("atom_id")})


def _prune_abstract_trigger_main(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if _mode(node) not in _COND_MODES:
            continue
        act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
        changed = False
        for group in act.get("trigger_groups") or []:
            if not isinstance(group, dict):
                continue
            elems = group.get("elements") or []
            if not isinstance(elems, list):
                continue
            has_specific = any(isinstance(e, dict) and bool(e.get("main")) and not _ABSTRACT_ELEM_RE.match(str(e.get("value") or "")) for e in elems)
            for e in elems:
                if not isinstance(e, dict):
                    continue
                if _ABSTRACT_ELEM_RE.match(str(e.get("value") or "")) and has_specific and bool(e.get("main")):
                    e["main"] = False
                    changed = True
        if changed:
            audit.append({"type": "abstract_trigger_element_demoted", "node_id": _node_id(node)})


def _hard_cluster(row: dict[str, Any]) -> str:
    blob = _text({k: v for k, v in row.items() if k not in {"source_quote"}})
    if re.search(r"禁用|禁止.{0,8}表达|不要说|不能说|语气词|口头禅", blob):
        return "lexical_ban"
    if re.search(r"承诺|保证|确保|一定|肯定", blob) and re.search(r"权益|资源|补偿|赠送|券|结果|通过|成功|状态", blob):
        return "promise_result"
    if re.search(r"开车|驾驶|骑行|安全|危险|不方便", blob) and re.search(r"继续|推进|稍后|挂断|停止", blob):
        return "safety_stop"
    if re.search(r"职责范围|范围外|权限|越权|同事确认|回电", blob):
        return "scope_boundary"
    if re.search(r"代操作|代为|代替|帮.*操作|人工.*改|越权处理", blob):
        return "delegate_or_override"
    return ""


def _dedupe_hard_rows(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    rows = [r for r in (graph.get("hard_constraint_table") or []) if isinstance(r, dict)]
    if not rows:
        return
    best: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    removed: list[str] = []
    for row in rows:
        cluster = _hard_cluster(row)
        if not cluster:
            others.append(row)
            continue
        current = best.get(cluster)
        score = len(_text(row)) + 120 * int(bool(row.get("negative_groups"))) + 80 * int(bool(row.get("safe_groups")))
        cur_score = len(_text(current)) + 120 * int(bool(current and current.get("negative_groups"))) + 80 * int(bool(current and current.get("safe_groups"))) if current else -1
        if current is None or score > cur_score:
            if current is not None:
                removed.append(str(current.get("id") or current.get("constraint_id") or current.get("name") or ""))
            best[cluster] = row
        else:
            removed.append(str(row.get("id") or row.get("constraint_id") or row.get("name") or ""))
    graph["hard_constraint_table"] = others + list(best.values())
    if removed:
        audit.append({"type": "duplicate_hard_rows_removed", "constraint_ids": [x for x in removed if x][:20]})


def _existing_hard_clusters(graph: dict[str, Any]) -> set[str]:
    return {_hard_cluster(r) for r in (graph.get("hard_constraint_table") or []) if isinstance(r, dict) and _hard_cluster(r)}


def _elem_values(row: Any) -> list[str]:
    vals: list[str] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("value"):
                vals.append(str(x.get("value")))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(row)
    return vals


def _make_hard(cid: str, name: str, text: str, obj: str, bad: str, safe: str, kind: str) -> dict[str, Any]:
    return {
        "constraint_id": cid,
        "id": cid,
        "name": name,
        "enforcement": "hard",
        "constraint_kind": "semantic_object",
        "severity": "high",
        "atom_id": cid + "_a1",
        "text": text,
        "negative_groups": [_elements((obj, True, False), (bad, False, True))],
        "safe_groups": [_elements((obj, True, False), (safe, False, False))],
        "source_kind": "hard_constraint",
        "auto_source": kind,
    }


def _derive_hard_rows_from_graph_facts(graph: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    clusters = _existing_hard_clusters(graph)
    rows = graph.setdefault("hard_constraint_table", [])
    knowledge = [r for r in (graph.get("knowledge_table") or []) if isinstance(r, dict)]
    added: list[str] = []

    if "promise_result" not in clusters:
        obj = ""
        for row in knowledge:
            vals = _elem_values(row)
            for val in vals:
                if re.search(r"保住|保留|维持|入选|通过|成功|占用|获得|奖励", val):
                    obj = val[:24]
                    break
            if obj:
                break
        if obj:
            cid = _stable("hard_auto", "promise_result" + obj)
            rows.append(_make_hard(cid, "不得承诺结果", f"不得承诺或保证{obj}一定实现。", obj, "承诺、保证或确保", "以规则或实际结果为准", "promise_result"))
            added.append(cid)
            clusters.add("promise_result")

    if "delegate_or_override" not in clusters:
        obj = ""
        for row in knowledge:
            vals = _elem_values(row)
            joined = " ".join(vals)
            if re.search(r"App|APP|客户端|应用", joined) and re.search(r"本人|自己|自行|手动|操作|提交|保存|勾选|选择|取消", joined):
                obj = next((v for v in vals if re.search(r"App|APP|客户端|应用|操作|提交|保存|勾选|选择|取消", v)), "需本人处理事项")[:24]
                break
        if obj:
            cid = _stable("hard_auto", "delegate" + obj)
            rows.append(_make_hard(cid, "不得代为操作", f"涉及{obj}时不得代为操作或越权处理。", obj, "代为操作或越权处理", "引导对方按正规路径自行处理", "delegate_or_override"))
            added.append(cid)
            clusters.add("delegate_or_override")

    if "scope_boundary" in clusters and "promise_result" not in clusters:
        # Scope boundary already exists; no extra generic result row is forced.
        pass
    if added:
        audit.append({"type": "hard_rows_derived_from_graph_facts", "constraint_ids": added})
