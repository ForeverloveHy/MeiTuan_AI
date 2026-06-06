from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

PUNCT = set('。！？!?；;，,：:')
NUM_RE = re.compile(r"\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+")
NUM_UNITS = ("单", "天", "元", "秒", "点", "%", "分钟", "小时", "次")
PROMPT_OVERFIT_TERMS = (
    "飞毛腿", "飞猫腿", "骑手", "商家", "标准直播", "低延迟直播", "低延迟",
    "直播", "合同", "课程", "退出", "报名", "排名", "配送", "派单", "名额",
    "资格", "收益", "12单", "10单", "1.5元", "前一天18点", "前一天下午6点", "18点前", "7天",
)
METADATA_TUNING_TERMS = ("正包", "负包", "误杀", "wrong_statement", "evidence_span", "injected_errors")


PROMPT_JSON_CONTRACT_FILES = (
    "schema_core_graph_prompt.md",
    "schema_knowledge_table_prompt.md",
    "schema_constraint_tables_prompt.md",
    "schema_atom_element_refinement_prompt.md",
    "schema_element_expansion_prompt.md",
)


def check_prompt_json_contract(root: Path, issues: list[dict[str, str]]) -> None:
    prompt_root = root / "prompts"
    for name in PROMPT_JSON_CONTRACT_FILES:
        path = prompt_root / name
        if not path.exists():
            add(issues, str(path), "五阶段 prompt 缺失。")
            continue
        text = path.read_text(encoding="utf-8")
        required = ("只输出一个合法 JSON 对象", "不能输出 Markdown", "不能有尾随逗号", "不要输出省略号")
        for phrase in required:
            if phrase not in text:
                add(issues, str(path), f"缺少严格 JSON 输出契约：{phrase}")
        forbidden = ("```", "//", "...", "……")
        for token in forbidden:
            if token in text:
                add(issues, str(path), f"prompt 含易诱导非法 JSON 的标记：{token}")
        if "注释" not in text:
            add(issues, str(path), "应明确禁止输出注释，避免 LLM 复制 JSON 注释。")
        if name == "schema_core_graph_prompt.md":
            for bad in ("ordered_all", "unordered_all"):
                if bad in text:
                    add(issues, str(path), f"主图 prompt 使用了非 canonical 关系枚举：{bad}")
            for good in ("sequential", "exclusive_branch", "optional_parallel", "all_of"):
                if good not in text:
                    add(issues, str(path), f"主图 prompt 缺少 canonical 关系枚举：{good}")
            if '"type": "before"' not in text or '"relation": "before"' not in text:
                add(issues, str(path), "edge 输出契约必须同时包含 type 与 relation，避免 diff/runtime 口径不一致。")
        if name == "schema_knowledge_table_prompt.md":
            if "expected_value" not in text:
                add(issues, str(path), "知识 prompt 必须使用 expected_value 作为 value_check 主字段。")
        if name in {"schema_atom_element_refinement_prompt.md", "schema_element_expansion_prompt.md"}:
            if "当前批次" not in text:
                add(issues, str(path), "元素 prompt 必须声明只返回当前批次 atom，避免长 JSON 失稳。")
            if "atom_id" not in text:
                add(issues, str(path), "元素 prompt 必须使用 atom_id 作为回写键，不得引入 anchor 概念。")
            if "anchor" in text or "锚点" in text:
                add(issues, str(path), "元素 prompt 不应暴露 anchor/锚点 概念，应只使用 atom。")


def compact(x: Any) -> str:
    return re.sub(r"\s+", "", str(x or ""))


def iter_elements(groups: Any):
    if not isinstance(groups, list):
        return
    for gi, g in enumerate(groups):
        if not isinstance(g, dict):
            continue
        for ei, e in enumerate(g.get("elements") or []):
            if isinstance(e, dict):
                yield gi, ei, e


def looks_like_sentence(value: str) -> bool:
    v = compact(value)
    if not v:
        return False
    if any(ch in v for ch in PUNCT):
        return True
    # Generic sentence-like action chains.  This is intentionally domain-neutral.
    if len(v) >= 12 and any(x in v for x in ("需要", "必须", "可以", "不能", "不会", "已经", "是否", "怎么", "为什么", "如果", "否则")):
        return True
    return False


def numeric_like(value: str) -> bool:
    v = compact(value)
    return bool(NUM_RE.search(v) and any(u in v for u in NUM_UNITS))


def add(issues: list[dict[str, str]], path: str, msg: str) -> None:
    issues.append({"path": path, "message": msg})


def check_node_atoms(graph: dict[str, Any], issues: list[dict[str, str]]) -> None:
    for ni, node in enumerate(graph.get("nodes") or []):
        nid = node.get("node_id") or node.get("id") or f"node[{ni}]"
        for ai, atom in enumerate(node.get("atoms") or []):
            aid = atom.get("atom_id") or atom.get("id") or f"atom[{ai}]"
            groups = atom.get("element_groups") or atom.get("trigger_groups") or []
            for gi, ei, e in iter_elements(groups):
                val = str(e.get("value") or "")
                if e.get("fact") is True:
                    add(issues, f"nodes.{nid}.{aid}.g{gi}.e{ei}", "节点 atom 不应包含 fact；节点只做履约命中，不做事实真假判断。")
                if e.get("main") is True and looks_like_sentence(val):
                    add(issues, f"nodes.{nid}.{aid}.g{gi}.e{ei}", f"main 过长或像整句：{val}")
            # each node atom should have at least one main recall element
            mains = [e for _, _, e in iter_elements(atom.get("element_groups") or []) if e.get("main") is True]
            if not mains:
                add(issues, f"nodes.{nid}.{aid}", "节点 atom 缺少 main 召回元素。")


def check_knowledge(graph: dict[str, Any], issues: list[dict[str, str]]) -> None:
    for ki, item in enumerate(graph.get("knowledge_table") or []):
        kid = item.get("knowledge_id") or item.get("id") or f"knowledge[{ki}]"
        for ai, atom in enumerate(item.get("atoms") or [item]):
            if not isinstance(atom, dict):
                continue
            aid = atom.get("atom_id") or atom.get("id") or f"atom[{ai}]"
            # selector must have main and no fact
            selector_mains = []
            for gi, ei, e in iter_elements(atom.get("selector_groups") or []):
                if e.get("fact") is True:
                    add(issues, f"knowledge.{kid}.{aid}.selector.g{gi}.e{ei}", "selector 不应包含 fact；selector 只召回事实对象。")
                if e.get("main") is True:
                    selector_mains.append(e)
            if not selector_mains:
                add(issues, f"knowledge.{kid}.{aid}.selector", "知识 atom selector 缺少 main 主干。")
            # correct groups should bind fact to a main trunk unless value_check-only legacy row
            for group_name in ("correct_groups", "wrong_groups"):
                for gi, g in enumerate(atom.get(group_name) or []):
                    elems = [e for e in (g.get("elements") or []) if isinstance(e, dict)]
                    has_fact = any(e.get("fact") is True for e in elems)
                    has_main = any(e.get("main") is True and e.get("fact") is not True for e in elems)
                    if has_fact and not has_main:
                        add(issues, f"knowledge.{kid}.{aid}.{group_name}.g{gi}", "fact 必须绑定同组非 fact main 主干。")
            vc = atom.get("value_check") or {}
            # Numeric/time facts should not enumerate wrong examples.
            wrong_examples = vc.get("wrong_examples") or []
            if wrong_examples:
                vals = " ".join(map(str, wrong_examples))
                if numeric_like(vals):
                    add(issues, f"knowledge.{kid}.{aid}.value_check", "数字/时间/金额/单量类 wrong_examples 应为空，由 value_check 比较。")
            for gi, g in enumerate(atom.get("wrong_groups") or []):
                for _gi2, ei, e in iter_elements([g]):
                    val = str(e.get("value") or "")
                    if numeric_like(val):
                        add(issues, f"knowledge.{kid}.{aid}.wrong.g{gi}.e{ei}", "wrong_groups 不应枚举数字/时间反事实。")


def check_constraints(graph: dict[str, Any], issues: list[dict[str, str]]) -> None:
    hard = graph.get("hard_constraint_table") or []
    soft = graph.get("soft_constraint_table") or []
    for ci, c in enumerate(hard):
        cid = c.get("constraint_id") or c.get("id") or f"hard[{ci}]"
        if "metric" in c:
            add(issues, f"hard_constraint_table.{cid}", "metric 型质量项应放入 soft_constraint_table，不应放入 hard。")
        for ai, atom in enumerate(c.get("atoms") or [c]):
            if not isinstance(atom, dict):
                continue
            aid = atom.get("atom_id") or atom.get("id") or f"atom[{ai}]"
            neg_mains = []
            for gi, ei, e in iter_elements(atom.get("negative_groups") or []):
                if e.get("main") is True:
                    neg_mains.append(e)
            if not neg_mains:
                add(issues, f"hard_constraint_table.{cid}.{aid}", "硬限制缺少 negative main，无法形成负向对象。")
    for ci, c in enumerate(soft):
        cid = c.get("constraint_id") or c.get("id") or f"soft[{ci}]"
        if "negative_groups" in c or "safe_groups" in c:
            add(issues, f"soft_constraint_table.{cid}", "软限制不应携带 negative/safe 硬限制结构。")




def check_graph_metadata(graph: dict[str, Any], issues: list[dict[str, str]]) -> None:
    metadata = graph.get("metadata") or {}
    text = json.dumps(metadata, ensure_ascii=False)
    for term in METADATA_TUNING_TERMS:
        if term in text:
            add(issues, "metadata", f"图 metadata 含数据集调参/泄漏痕迹：{term}")



def check_canonical_schema(graph: dict[str, Any], issues: list[dict[str, str]]) -> None:
    canonical_group_types = {"sequential", "any_of", "exclusive_branch", "optional_parallel", "all_of", "before"}
    for ei, edge in enumerate(graph.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        etype = edge.get("type") or edge.get("relation")
        if not edge.get("type") or not edge.get("relation"):
            add(issues, f"edges[{ei}]", "edge 应同时包含 type 与 relation，避免 prompt、diff、runtime 口径不一致。")
        if edge.get("type") and edge.get("relation") and edge.get("type") != edge.get("relation"):
            add(issues, f"edges[{ei}]", "edge.type 与 edge.relation 不一致。")
        if etype in {"ordered_all", "unordered_all"}:
            add(issues, f"edges[{ei}]", f"edge 使用了非 canonical 枚举：{etype}")
    for gi, group in enumerate(graph.get("relation_groups") or []):
        if not isinstance(group, dict):
            continue
        gtype = group.get("type") or group.get("relation") or group.get("group_type")
        if not group.get("type") or not group.get("relation"):
            add(issues, f"relation_groups[{gi}]", "relation_group 应同时包含 type 与 relation。")
        if group.get("type") and group.get("relation") and group.get("type") != group.get("relation"):
            add(issues, f"relation_groups[{gi}]", "relation_group.type 与 relation 不一致。")
        if gtype not in canonical_group_types:
            add(issues, f"relation_groups[{gi}]", f"relation_group 使用了非 canonical 枚举：{gtype}")


def check_batched_element_runtime(root: Path, issues: list[dict[str, str]]) -> None:
    runner = root / "src" / "sceg" / "demo_runner.py"
    if not runner.exists():
        return
    text = runner.read_text(encoding="utf-8")
    required = ("_split_atom_transport", "SCEG_ELEMENT_BATCH_SIZE", "split_atom_semantics_into_elements", "expand_element_pools_for_atom_batch")
    for phrase in required:
        if phrase not in text:
            add(issues, str(runner), f"元素阶段缺少 atom 传输分批运行机制：{phrase}")
    forbidden_payload_terms = ("\"graph_core\": compact_graph_core", "\"knowledge_index\": _compact_knowledge_index", "original_complex_instruction\": instruction,\n            \"atom_transport\"")
    for bad in forbidden_payload_terms:
        if bad in text:
            add(issues, str(runner), f"生成阶段存在不符合独立/简化传输的新逻辑：{bad}")

def check_prompt_overfit(root: Path, issues: list[dict[str, str]]) -> None:
    prompt_root = root / "prompts"
    if not prompt_root.exists():
        return
    allowed = {prompt_root / "instructions" / "merchant_instruction.txt", prompt_root / "instructions" / "rider_instruction.txt"}
    for path in sorted(prompt_root.glob("*.md")):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for term in PROMPT_OVERFIT_TERMS:
            if term in text:
                add(issues, str(path), f"通用建图 prompt 含当前任务投影词：{term}")


def check_oracle_routing(root: Path, issues: list[dict[str, str]]) -> None:
    cfg_path = root / "config" / "default_runtime.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        budget = cfg.get("oracle_budget") or {}
        if budget.get("route_requirement_candidates", False) is not False:
            add(issues, str(cfg_path), "节点 requirement 仲裁路由默认必须关闭。")
        if budget.get("route_context_candidates", False) is not False:
            add(issues, str(cfg_path), "上下文/节点类仲裁路由默认必须关闭；LLM 只处理知识与硬限制灰区。")
    router_path = root / "src" / "sceg" / "oracle_router.py"
    if router_path.exists():
        text = router_path.read_text(encoding="utf-8")
        if "out.extend(self._requirement_candidates(result))" in text and 'route_requirement_candidates' not in text:
            add(issues, str(router_path), "oracle_router 存在无配置门控的 requirement 候选送审。")
        if "out.extend(self._context_candidates(result))" in text and 'route_context_candidates' not in text:
            add(issues, str(router_path), "oracle_router 存在无配置门控的 context 候选送审。")

def check_graph(path: Path) -> list[dict[str, str]]:
    graph = json.loads(path.read_text(encoding="utf-8"))
    issues: list[dict[str, str]] = []
    check_node_atoms(graph, issues)
    check_knowledge(graph, issues)
    check_constraints(graph, issues)
    check_graph_metadata(graph, issues)
    check_canonical_schema(graph, issues)
    return issues


def default_graphs() -> list[Path]:
    candidates = [
        Path("perfect_rider_graph_v15_iter8.json"),
        Path("perfect_merchant_graph_v15_iter8.json"),
        Path("examples/perfect_graphs/perfect_rider_graph_v15_iter8.json"),
        Path("examples/perfect_graphs/perfect_merchant_graph_v15_iter8.json"),
        Path("example/rider_graph_example.json"),
        Path("example/merchant_graph_example.json"),
    ]
    return [p for p in candidates if p.exists()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the current SCEG atom/elements method contract.")
    ap.add_argument("graphs", nargs="*", help="Graph JSON files. Defaults to latest packaged graphs.")
    args = ap.parse_args()
    graphs = [Path(x) for x in args.graphs] if args.graphs else default_graphs()
    if not graphs:
        raise SystemExit("no graph files found")
    all_issues: list[tuple[Path, list[dict[str, str]]]] = []
    root_issues: list[dict[str, str]] = []
    check_prompt_overfit(Path.cwd(), root_issues)
    check_prompt_json_contract(Path.cwd(), root_issues)
    check_oracle_routing(Path.cwd(), root_issues)
    check_batched_element_runtime(Path.cwd(), root_issues)
    if root_issues:
        all_issues.append((Path.cwd(), root_issues))
    for p in graphs:
        issues = check_graph(p)
        if issues:
            all_issues.append((p, issues))
    if all_issues:
        for p, issues in all_issues:
            print(f"[FAIL] {p} ({len(issues)} issues)")
            for issue in issues[:50]:
                print(f"  - {issue['path']}: {issue['message']}")
            if len(issues) > 50:
                print(f"  ... {len(issues)-50} more")
        raise SystemExit(1)
    print("method contract guard passed")


if __name__ == "__main__":
    main()
