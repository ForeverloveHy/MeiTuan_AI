from __future__ import annotations

"""Static / report-based preflight audit before an expensive graph rebuild.

This tool does not judge samples and does not use answer-key text to score.  It
summarizes structural risks that a no-memory graph builder is likely to repeat,
then prints repair hints that can be fed into a second-pass graph prompt.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any


def text_of(x: Any) -> str:
    parts: list[str] = []
    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for vv in v.values(): walk(vv)
        elif isinstance(v, list):
            for vv in v: walk(vv)
        elif v is not None:
            s = str(v).strip()
            if s: parts.append(s)
    walk(x)
    return " ".join(parts)


def node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "")


def node_kind(node: dict[str, Any]) -> str:
    return str(node.get("type") or node.get("node_type") or "").lower()


def atom_rows(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in node.get("atoms") or node.get("requirements") or [] if isinstance(x, dict)]


def simple_tokens(s: Any) -> set[str]:
    raw = str(s or "").lower()
    out: set[str] = set()
    for chunk in re.findall(r"[a-z0-9\u4e00-\u9fff]+", raw):
        out.add(chunk)
        if re.search(r"[\u4e00-\u9fff]", chunk):
            out.update(chunk[i:i+2] for i in range(max(0, len(chunk)-1)))
            out.update(chunk[i:i+3] for i in range(max(0, len(chunk)-2)))
    return {x for x in out if x}


def sim(a: Any, b: Any) -> float:
    ta, tb = simple_tokens(a), simple_tokens(b)
    if not ta or not tb: return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def audit_graph(graph: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    nodes = [x for x in graph.get("nodes") or [] if isinstance(x, dict)]
    node_map = {node_id(n): n for n in nodes}

    for n in nodes:
        nid = node_id(n)
        kind = node_kind(n)
        name = str(n.get("name") or "")
        atoms = atom_rows(n)
        if (kind in {"faq", "out_of_scope"} or any(x in name for x in ("FAQ", "追问", "问题"))) and len(atoms) >= 3:
            pair_sims = []
            for i in range(len(atoms)):
                for j in range(i + 1, len(atoms)):
                    pair_sims.append(sim(atoms[i].get("text") or atoms[i].get("name"), atoms[j].get("text") or atoms[j].get("name")))
            avg = sum(pair_sims) / max(1, len(pair_sims))
            if avg < 0.18:
                issues.append({
                    "type": "faq_overpacked",
                    "node_id": nid,
                    "atom_count": len(atoms),
                    "avg_atom_similarity": round(avg, 4),
                    "repair": "split into one user-triggered node per question object; do not score unrelated sibling atoms after one trigger",
                })
        if kind in {"main", "process", "start", ""}:
            ask_atoms = []
            for a in atoms:
                t = str(a.get("text") or a.get("name") or "")
                if any(x in t for x in ("询问", "确认", "检查", "是否", "哪", "还是", "提供")):
                    ask_atoms.append(str(a.get("id") or a.get("atom_id") or a.get("name") or ""))
            if ask_atoms:
                issues.append({
                    "type": "info_request_should_accept_user_provided_state",
                    "node_id": nid,
                    "atom_ids": ask_atoms[:8],
                    "repair": "write the atom as obtain/confirm information; if the user already provided it, do not require the agent to ask again",
                })

    conditional = {node_id(n) for n in nodes if node_kind(n) not in {"main", "process", "start", ""} or str((n.get("activation") or {}).get("mode") or "") in {"condition", "user_triggered", "optional"}}
    for rg in graph.get("relation_groups") or []:
        if not isinstance(rg, dict):
            continue
        gtype = str(rg.get("type") or rg.get("relation") or "").lower()
        req = bool(rg.get("required", True))
        members = [str(x) for x in (rg.get("nodes") or [])]
        bad = [x for x in members if x in conditional]
        if req and gtype in {"sequential", "before", "ordered", "strict_order"} and bad:
            issues.append({
                "type": "conditional_node_in_required_sequence",
                "group_id": rg.get("id") or rg.get("group_id"),
                "node_ids": bad,
                "repair": "remove conditional nodes from required sequential groups; score their own conditional edges only when triggered",
            })

    hard = [x for x in graph.get("hard_constraint_table") or [] if isinstance(x, dict)]
    clusters: dict[str, list[str]] = {}
    for h in hard:
        blob = text_of({k:v for k,v in h.items() if k not in {"source_quote"}})
        cluster = ""
        if re.search(r"(禁用|禁止使用|不说).{0,20}(表达|词)", blob): cluster = "banned-expression"
        elif re.search(r"(安全|危险|不方便|开着).{0,25}(继续|推进|稍后|挂断)", blob): cluster = "safety-stop"
        elif re.search(r"(职责|权限|范围).{0,25}(擅自|编造|越权|确认|回电)", blob): cluster = "scope-boundary"
        elif re.search(r"(承诺|保证|确保|一定).{0,25}(权益|优惠|福利|结果)", blob): cluster = "unfounded-promise"
        if cluster:
            clusters.setdefault(cluster, []).append(str(h.get("id") or h.get("constraint_id") or h.get("name") or ""))
    for cluster, ids in clusters.items():
        if len(ids) > 1:
            issues.append({"type":"duplicate_hard_cluster", "cluster": cluster, "constraint_ids": ids, "repair":"merge duplicates and keep the narrowest object/action pair"})
    return issues


def summarize_reports(path: Path) -> list[dict[str, Any]]:
    if not path or not path.exists(): return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("reports", []) if isinstance(data, dict) else []
    clusters: dict[str, dict[str, Any]] = {}
    for r in rows:
        acc = r.get("acceptance") or {}
        for e in acc.get("unexpected_bad_events") or []:
            key = "|".join(str(e.get(k) or "") for k in ("kind","node_id","requirement_id","knowledge_id","constraint_id","relation"))
            item = clusters.setdefault(key, {"count":0, "sample": e})
            item["count"] += 1
        for e in acc.get("missing_expected") or []:
            fam = str(e.get("error_family") or e.get("type") or "")
            key = "miss|" + fam + "|" + str(e.get("knowledge_id") or e.get("constraint_id") or e.get("node_id") or e.get("target_id") or "")
            item = clusters.setdefault(key, {"count":0, "sample": {"kind":"expected_miss", "error_family": fam, "target": key}})
            item["count"] += 1
    out = sorted(clusters.values(), key=lambda x: x["count"], reverse=True)[:40]
    return [{"count": x["count"], "sample": x["sample"]} for x in out]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("graph")
    ap.add_argument("--reports", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    result = {"graph_issues": audit_graph(graph), "report_clusters": summarize_reports(Path(args.reports)) if args.reports else []}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
