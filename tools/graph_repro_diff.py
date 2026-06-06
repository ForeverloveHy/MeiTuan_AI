from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def names(items: list[dict[str, Any]], id_keys: tuple[str, ...], name_keys: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, item in enumerate(items or []):
        ident = next((str(item.get(k)) for k in id_keys if item.get(k)), f"item_{i:03d}")
        name = next((str(item.get(k)) for k in name_keys if item.get(k)), ident)
        out[ident] = name
    return out


def atom_names(table: list[dict[str, Any]], item_id_key: str, atom_prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ii, item in enumerate(table or []):
        parent = str(item.get(item_id_key) or item.get("id") or f"{atom_prefix}_{ii:03d}")
        for ai, atom in enumerate(item.get("atoms") or [item]):
            if not isinstance(atom, dict):
                continue
            aid = str(atom.get("atom_id") or atom.get("id") or f"{parent}_atom_{ai:03d}")
            out[aid] = str(atom.get("name") or atom.get("text") or aid)
    return out


RELATION_ALIASES = {
    "ordered_all": "sequential",
    "ordered": "sequential",
    "strict_order": "sequential",
    "before": "sequential",
    "unordered_all": "all_of",
    "parallel_optional": "optional_parallel",
    "optional": "optional_parallel",
    "one_of": "any_of",
    "choice": "any_of",
}


def norm_relation(value: Any, default: str = "before") -> str:
    raw = str(value or default).strip() or default
    return RELATION_ALIASES.get(raw, raw)


def edge_keys(g: dict[str, Any]) -> set[str]:
    keys = set()
    for e in g.get("edges") or []:
        if not isinstance(e, dict):
            continue
        etype = norm_relation(e.get("type") or e.get("relation") or e.get("edge_type"), "before")
        keys.add(f"{e.get('source')}->{e.get('target')}:{etype}")
    return keys


def relation_group_keys(g: dict[str, Any]) -> set[str]:
    keys = set()
    for rg in g.get("relation_groups") or []:
        if not isinstance(rg, dict):
            continue
        rtype = norm_relation(rg.get("type") or rg.get("relation") or rg.get("group_type"), "all_of")
        nodes = ",".join(str(x) for x in (rg.get("nodes") or rg.get("node_ids") or []))
        keys.add(f"{rtype}:{nodes}")
    return keys


def grams(text: str, n: int = 2) -> set[str]:
    t = "".join(ch for ch in str(text) if not ch.isspace())
    if not t:
        return set()
    if len(t) <= n:
        return {t}
    return {t[i:i+n] for i in range(len(t)-n+1)}


def sim(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def fuzzy_pairs(target_vals: set[str], cand_vals: set[str], threshold: float = 0.36) -> tuple[list[tuple[str, str, float]], list[str]]:
    pairs: list[tuple[str, str, float]] = []
    fuzzy_missing: list[str] = []
    for tv in sorted(target_vals):
        best = ("", 0.0)
        for cv in cand_vals:
            score = sim(tv, cv)
            if score > best[1]:
                best = (cv, score)
        if best[1] >= threshold:
            pairs.append((tv, best[0], best[1]))
        else:
            fuzzy_missing.append(tv)
    return pairs, fuzzy_missing


def compare_map(title: str, target: dict[str, str], cand: dict[str, str]) -> list[str]:
    tvals = set(target.values())
    cvals = set(cand.values())
    missing = sorted(tvals - cvals)
    extra = sorted(cvals - tvals)
    fuzzy, fuzzy_missing = fuzzy_pairs(tvals, cvals)
    lines = [
        f"## {title}",
        f"target={len(tvals)} candidate={len(cvals)} exact_missing={len(missing)} extra={len(extra)} fuzzy_missing={len(fuzzy_missing)} fuzzy_matched={len(fuzzy)}",
    ]
    if fuzzy_missing:
        lines.append("fuzzy_missing:")
        lines.extend(f"- {x}" for x in fuzzy_missing[:80])
    if missing:
        lines.append("exact_missing:")
        lines.extend(f"- {x}" for x in missing[:80])
    if extra:
        lines.append("extra:")
        lines.extend(f"- {x}" for x in extra[:80])
    if fuzzy:
        lines.append("probable_matches:")
        for tv, cv, score in sorted(fuzzy, key=lambda x: x[2], reverse=True)[:30]:
            if tv != cv:
                lines.append(f"- {tv} ~= {cv} ({score:.2f})")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare a generated SCEG graph against a target graph at structure/name level.")
    ap.add_argument("--target", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    t = load(args.target)
    c = load(args.candidate)
    sections: list[str] = []
    sections.extend(compare_map("nodes", names(t.get("nodes") or [], ("node_id", "id"), ("name", "title")), names(c.get("nodes") or [], ("node_id", "id"), ("name", "title"))))
    sections.append("")
    sections.extend(compare_map("knowledge_atoms", atom_names(t.get("knowledge_table") or [], "knowledge_id", "k"), atom_names(c.get("knowledge_table") or [], "knowledge_id", "k")))
    sections.append("")
    sections.extend(compare_map("hard_constraint_atoms", atom_names(t.get("hard_constraint_table") or [], "constraint_id", "hc"), atom_names(c.get("hard_constraint_table") or [], "constraint_id", "hc")))
    sections.append("")
    te, ce = edge_keys(t), edge_keys(c)
    sections.extend(["## edges", f"target={len(te)} candidate={len(ce)} missing={len(te-ce)} extra={len(ce-te)}"])
    if te - ce:
        sections.append("missing:")
        sections.extend(f"- {x}" for x in sorted(te-ce)[:100])
    if ce - te:
        sections.append("extra:")
        sections.extend(f"- {x}" for x in sorted(ce-te)[:100])
    sections.append("")
    trg, crg = relation_group_keys(t), relation_group_keys(c)
    sections.extend(["## relation_groups", f"target={len(trg)} candidate={len(crg)} missing={len(trg-crg)} extra={len(crg-trg)}"])
    if trg - crg:
        sections.append("missing:")
        sections.extend(f"- {x}" for x in sorted(trg-crg)[:100])
    if crg - trg:
        sections.append("extra:")
        sections.extend(f"- {x}" for x in sorted(crg-trg)[:100])
    report = "\n".join(sections) + "\n"
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()
