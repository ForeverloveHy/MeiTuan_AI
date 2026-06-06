from __future__ import annotations

from sceg.element_engine import ElementEngine
from sceg.evidence_units import EvidenceUnit
from sceg.schema import ConstraintRule, KnowledgeItem, StateGraph
from sceg.constraint_judge import ConstraintJudge
from sceg.graph_evaluator import GraphEvaluator
from sceg.knowledge_judge import KnowledgeJudge
from sceg.demo_runner import _merge_stage2_refinement_delta, _merge_stage3_secondary_pools
from sceg.schema_repair_audit import audit_schema_repair_need
from sceg.schema_compiler import compile_state_graph


def unit(i: int, speaker: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(i, speaker, text, text)


def main() -> None:
    engine = ElementEngine()

    # 第二阶段 element细化是必要建图阶段，不能由旧 mode/off/审计结果跳过。
    audit = audit_schema_repair_need({"nodes": [{"id": "n"}], "knowledge_table": [{}], "constraint_table": [{}]}, None, repair_mode="off")
    assert audit["needs_repair"] is True and audit["refine_mode"] == "required", "stage-2 element refinement must be mandatory"

    atoms = engine.build_atoms([unit(0, "assistant", "您好")])
    speaker_only = engine.make_rule(
        "speaker_only",
        "self_check",
        primary=[{"type": "speaker_role", "value": "assistant"}],
        policy={"hit_threshold": 0.5, "review_threshold": 0.3},
    )
    assert engine.match_rule(speaker_only, atoms).verdict == "miss", "speaker_role must not recall alone"

    atoms = engine.build_atoms([unit(0, "assistant", "请问是您本人吗")])
    confirm_identity = engine.make_rule(
        "confirm_identity",
        "self_check",
        primary=[
            {"type": "intent", "value": "confirm"},
            {"type": "target", "value": "identity"},
            {"type": "form", "value": "question"},
        ],
        policy={"hit_threshold": 0.7, "review_threshold": 0.4, "must_have": ["target"]},
    )
    assert engine.match_rule(confirm_identity, atoms).verdict == "hit", "confirm identity should hit by semantic elements"

    grouped_atoms = engine.build_atoms([unit(0, "assistant", "项目甲是根据系统规则来申请的")])
    grouped_rule = engine.make_rule(
        "grouped_selection",
        "node_positive",
        element_groups=[{
            "group_id": "g_main_1",
            "group_role": "main",
            "required": True,
            "threshold": 0.75,
            "min_element_hits": 3,
            "elements": [
                {"element_id": "e_target", "type": "business_target", "value": "项目甲", "required": True, "secondary_pool": ["项目甲", "业务项目"]},
                {"element_id": "e_action", "type": "action", "value": "申请", "required": True, "secondary_pool": ["申请", "提交"]},
                {"element_id": "e_relation", "type": "relation", "value": "按", "secondary_pool": ["按", "根据", "以"]},
                {"element_id": "e_value", "type": "value", "value": "排序", "required": True, "secondary_pool": ["排序", "顺序"]},
            ],
        }],
        policy={"group_logic": "any_main_group", "hit_threshold": 0.75, "review_threshold": 0.45},
    )
    grouped_match = engine.match_rule(grouped_rule, grouped_atoms)
    assert grouped_match.verdict == "hit", "grouped element rule must hit per-element secondary pools inside one turn atom"

    hard = ConstraintRule.from_dict({
        "id": "hc_no_promise",
        "name": "禁止承诺",
        "enforcement": "hard",
        "trigger_policy": "self_sufficient",
        "trigger_object": {"primary_elements": [{"type": "context_state", "value": "driving"}]},
        "negative_elements": [
            {"type": "intent", "value": "promise"},
            {"type": "modality", "value": "guarantee"},
        ],
        "positive_elements": [
            {"type": "intent", "value": "refuse"},
            {"type": "polarity", "value": "negation"},
        ],
        "match_policy": {"hit_threshold": 0.7, "review_threshold": 0.4, "must_have": ["intent"]},
    })
    check = ConstraintJudge(engine=engine).judge([hard], [unit(0, "assistant", "我保证一定能成功")])[0]
    assert check.verdict == "违规" and check.trigger_verdict == "hit", "self_sufficient hard constraint must not require trigger"
    user_only = ConstraintJudge(engine=engine).judge([hard], [unit(0, "user", "你能保证一定成功吗")])
    assert not user_only or all(c.verdict != "违规" for c in user_only), "assistant-side hard constraints must not be triggered by user wording"

    legacy_hard = ConstraintRule.from_dict({
        "id": "hc_legacy",
        "name": "legacy hard",
        "prohibited": [{"speaker": "assistant", "all": ["保证"], "any": ["结果", "通过"]}],
        "safe_context": [{"speaker": "assistant", "any": ["不能保证"]}],
    })
    legacy_check = ConstraintJudge(engine=engine).judge([legacy_hard], [unit(0, "assistant", "我保证给您改成通过结果")])[0]
    assert legacy_check.verdict == "违规", "legacy prohibited all/any pattern must still execute without being flattened away"

    soft = ConstraintRule.from_dict({
        "id": "sc",
        "name": "软限制",
        "enforcement": "soft",
        "global_elements": [{"type": "form", "value": "question"}],
    })
    assert len(soft.zero_level_elements) == 0, "zero-level elements are removed from the runtime model"
    soft_low = ConstraintRule.from_dict({
        "id": "sc_low",
        "name": "软限制密度上限",
        "enforcement": "soft",
        "global_elements": [{"type": "intent", "value": "inform"}],
        "metric": {"direction": "lower_better", "good_threshold": 0.2},
    })
    soft_low_check = ConstraintJudge(engine=engine).judge([soft_low], [unit(0, "assistant", "我通知您一下"), unit(1, "assistant", "再告知您一下")])[0]
    assert soft_low_check.score is not None and soft_low_check.score < 1.0, "soft constraints must support lower_better metrics"

    dup_graph = StateGraph.from_dict({
        "graph_id": "dup",
        "nodes": [],
        "hard_constraint_table": [{"id": "c", "name": "c", "negative_elements": [{"type": "surface", "value": "x"}]}],
        "soft_constraint_table": [{"id": "s", "name": "s", "enforcement": "soft", "global_elements": [{"type": "form", "value": "question"}]}],
        "constraint_table": [
            {"id": "c", "name": "c", "negative_elements": [{"type": "surface", "value": "x"}]},
            {"id": "s", "name": "s", "enforcement": "soft", "global_elements": [{"type": "form", "value": "question"}]},
        ],
    })
    assert len(dup_graph.constraints) == 2, "compiled constraint_table must not be double-merged with hard/soft source tables"

    knowledge = KnowledgeItem.from_dict({
        "id": "k_generic",
        "name": "属性核验",
        "positive_elements": [
            {"type": "business_target", "value": "对象甲"},
            {"type": "attribute", "value": "属性甲"},
            {"type": "value", "value": "存在"},
        ],
        "negative_elements": [
            {"type": "business_target", "value": "对象甲"},
            {"type": "attribute", "value": "属性甲"},
            {"type": "polarity", "value": "negation"},
        ],
        "secondary_elements": {
            "business_target:对象甲": {"surface_forms": ["对象甲"]},
            "attribute:属性甲": {"surface_forms": ["属性甲"]},
            "value:存在": {"surface_forms": ["存在"]},
        },
    })
    checks = KnowledgeJudge(engine=engine).judge([knowledge], [unit(0, "assistant", "对象甲不存在属性甲")])
    assert checks and checks[0].verdict in {"证据不足", "冲突"}, "knowledge negation must not be ignored"



    base2 = {"graph_id":"g","nodes":[{"id":"n1","atoms":[{"id":"a1","name":"确认身份"}]}],"knowledge_table":[{"id":"k1"}],"hard_constraint_table":[{"id":"hc1"}],"soft_constraint_table":[{"id":"sc1"}]}
    delta2 = {"graph_id":"SHOULD_IGNORE","node_refinements":[{"node_id":"n1","atom_refinements":[{"atom_id":"a1","primary_elements":[{"type":"intent","value":"confirm"}],"match_policy":{"must_have":["intent"]}}]}],"knowledge_refinements":[{"id":"k1","positive_elements":[{"type":"value","value":"对"}]}],"hard_constraint_refinements":[{"id":"hc1","negative_elements":[{"type":"modality","value":"guarantee"}]}],"soft_constraint_refinements":[{"id":"sc1","global_elements":[{"type":"form","value":"question"}]}]}
    merged2 = _merge_stage2_refinement_delta(base2, delta2)
    assert merged2["graph_id"] == "g", "stage-2 delta must not rewrite graph id"
    assert merged2["nodes"][0]["atoms"][0]["primary_elements"][0]["value"] == "confirm", "stage-2 delta must merge atom primary elements"
    assert merged2["knowledge_table"][0]["positive_elements"][0]["value"] == "对", "stage-2 delta must merge knowledge elements"
    assert merged2["hard_constraint_table"][0]["negative_elements"][0]["value"] == "guarantee", "stage-2 delta must merge hard negative elements"

    base = {
        "graph_id": "g",
        "nodes": [{"id": "n1", "atoms": [{"id": "a1", "primary_elements": [{"type": "intent", "value": "confirm"}], "secondary_elements": {}}]}],
        "knowledge_table": [{"id": "k1", "positive_elements": [{"type": "value", "value": "12单"}], "secondary_elements": {}}],
        "hard_constraint_table": [],
        "soft_constraint_table": [],
    }
    expanded = {
        "graph_id": "CHANGED",
        "nodes": [{"id": "n1", "atoms": [{"id": "a1", "primary_elements": [{"type": "intent", "value": "wrong"}], "secondary_elements": {"intent:confirm": {"surface_forms": ["核实一下"]}}}]}],
        "knowledge_table": [{"id": "k1", "positive_elements": [{"type": "value", "value": "10单"}], "secondary_elements": {"value:12单": {"surface_forms": ["十二单"]}}}],
    }
    merged = _merge_stage3_secondary_pools(base, expanded)
    assert merged["graph_id"] == "g"
    assert merged["nodes"][0]["atoms"][0]["primary_elements"][0]["value"] == "confirm"
    assert merged["knowledge_table"][0]["positive_elements"][0]["value"] == "12单"
    assert "intent:confirm" in merged["nodes"][0]["atoms"][0]["secondary_elements"]



    delta3 = {"node_refinements":[{"node_id":"n1","atom_refinements":[{"atom_id":"a1","primary_elements":[{"type":"intent","value":"wrong"}],"secondary_elements":{"intent:confirm":{"surface_forms":["请问"]}}}]}],"knowledge_refinements":[{"id":"k1","positive_elements":[{"type":"value","value":"wrong"}],"secondary_elements":{"value:12单":{"surface_forms":["十二单"]}}}]}
    merged3 = _merge_stage3_secondary_pools(base, delta3)
    assert merged3["nodes"][0]["atoms"][0]["primary_elements"][0]["value"] == "confirm", "stage-3 delta must not change primary elements"
    assert "intent:confirm" in merged3["nodes"][0]["atoms"][0]["secondary_elements"], "stage-3 delta must merge secondary pools"

    relation_graph = StateGraph.from_dict({
        "graph_id": "rel",
        "nodes": [{
            "id": "n",
            "name": "n",
            "requirements": [
                {"id": "a", "text": "A", "positive_object": {"surface_forms": ["后置"]}},
                {"id": "b", "text": "B", "positive_object": {"surface_forms": ["前置"]}},
            ],
            "atom_relations": [{"type": "before", "source": "a", "target": "b"}],
        }],
    })
    relation_eval = GraphEvaluator(relation_graph, {"weights": {}}).evaluate({"id": "rel_case", "turns": [{"speaker": "assistant", "text": "先说前置，再说后置"}]})
    assert any(e.relation.startswith("atom:") for e in relation_eval.relation_events), "atom_relations must be executed and surfaced as relation events"

    compiled_node_id_graph = compile_state_graph({
        "graph_id": "node_id_compat",
        "nodes": [{
            "node_id": "n_node_id",
            "name": "node_id 兼容节点",
            "atoms": [{
                "id": "a_node",
                "name": "任意业务对象也应作为 atom 执行",
                "object_role": "contract",
                "primary_elements": [{"type": "intent", "value": "notify"}],
                "secondary_elements": {"intent:notify": {"surface_forms": ["通知"]}},
            }],
        }],
        "knowledge_table": [{"id": "k_node", "name": "绑定检查", "node_id": "n_node_id", "atom_ids": ["a_node"]}],
    })
    assert compiled_node_id_graph["nodes"][0]["id"] == "n_node_id", "compiler must accept node_id-only LLM nodes"
    assert not compiled_node_id_graph.get("metadata", {}).get("table_binding_warnings"), "valid node_id/atom binding must not produce stale warnings"
    node_id_state_graph = StateGraph.from_dict(compiled_node_id_graph)
    assert node_id_state_graph.nodes[0].id == "n_node_id", "StateGraph must parse id/node_id compatibly"
    assert len(node_id_state_graph.nodes[0].requirements) == 1, "all node atoms, regardless of object_role, must become executable requirements"


    # Simplified prompt contract: no element_id/type/weight/threshold; local
    # executor must still use adjacent windows and schema-local thresholds.
    simple_atoms = engine.build_atoms([unit(0, "assistant", "目标服务是根据系统规则来处理的，人工角色干预不了。")])
    simple_rule = engine.make_rule(
        "simple_grouped",
        "node_positive",
        element_groups=[
            {"role": "main", "elements": [
                {"value": "目标服务", "main": True, "pool": ["相关服务"]},
                {"value": "处理", "main": False, "pool": ["处理", "提交"]},
                {"value": "按", "main": False, "pool": ["按", "根据", "依据"]},
                {"value": "系统规则", "main": True, "pool": ["系统规则", "规则", "排序"]},
            ]},
            {"role": "supporting", "elements": [
                {"value": "否定", "main": True, "pool": ["不是", "并非", "不能", "干预不了"]},
                {"value": "人工角色", "main": False, "pool": ["人工角色", "人工", "负责人"]},
                {"value": "干预", "main": True, "pool": ["干预", "决定", "安排", "说了算"]},
            ]},
        ],
    )
    simple_match = engine.match_rule(simple_rule, simple_atoms)
    assert simple_match.verdict == "hit" and simple_match.score >= 0.95, "same-turn grouped atom should hit without unnecessary locality loss"

    selector_knowledge = KnowledgeItem.from_dict({
        "knowledge_id": "k_minimal",
        "name": "系统规则机制",
        "atoms": [{
            "atom_id": "k_minimal_atom",
            "name": "按系统规则",
            "selector_groups": [{"elements": [{"value": "目标服务", "main": True, "pool": ["目标服务"]}, {"value": "处理", "main": False, "pool": ["处理", "提交"]}]}],
            "correct_groups": [{"elements": [{"value": "系统规则", "main": True, "pool": ["系统规则", "排序"]}]}],
            "wrong_groups": [{"elements": [{"value": "人工决定", "main": True, "pool": ["人工决定", "人工指定"]}]}],
        }],
    })
    # from_dict flattens individual atom rows via StateGraph, but a direct row should parse selector groups too.
    selector_row = KnowledgeItem.from_dict({
        "id": "k_selector_row",
        "name": "按系统规则",
        "selector_groups": [{"elements": [{"value": "目标服务", "main": True, "pool": ["目标服务"]}, {"value": "处理", "main": False, "pool": ["处理", "提交"]}]}],
        "correct_groups": [{"elements": [{"value": "系统规则", "main": True, "pool": ["系统规则", "排序"]}]}],
        "wrong_groups": [{"elements": [{"value": "人工决定", "main": True, "pool": ["人工决定", "人工指定"]}]}],
    })
    k_checks = KnowledgeJudge(engine=engine).judge([selector_row], [unit(0, "assistant", "目标服务处理是按系统规则来的")])
    assert k_checks and k_checks[0].verdict == "支持", "knowledge selector/correct groups should support minimal structure"



    # State-graph branch discipline: branch-like nodes are not active just
    # because they exist or have a before edge; they need trigger/local evidence.
    branch_graph = StateGraph.from_dict({
        "graph_id": "branch_check",
        "nodes": [
            {"id": "n_start", "name": "开始", "node_type": "start", "activation": {"mode": "always"}, "atoms": [{"id": "a_start", "name": "开场", "text": "开场", "required": True, "element_groups": [{"role": "main", "elements": [{"value": "你好", "main": True, "pool": ["您好"]}]}]}]},
            {"id": "n_branch", "name": "未触发分支", "node_type": "branch", "activation": {"mode": "always"}, "required": False, "atoms": [{"id": "a_branch", "name": "分支说明", "text": "分支说明", "required": True, "element_groups": [{"role": "main", "elements": [{"value": "分支", "main": True, "pool": []}]}]}]},
        ],
        "edges": [{"source": "n_start", "target": "n_branch", "type": "before"}],
    })
    branch_eval = GraphEvaluator(branch_graph, {"weights": {}}).evaluate({"id": "b", "turns": [{"speaker": "assistant", "text": "您好"}]})
    bnodes = {n.node_id: n for n in branch_eval.node_results}
    assert bnodes["n_start"].active and not bnodes["n_branch"].active, "branch node must not auto-activate through a before edge"

    # Terminal/suppress edge: once the terminal node is handled, downstream
    # required nodes are marked not-applicable, not missing.
    terminal_graph = StateGraph.from_dict({
        "graph_id": "terminal_check",
        "nodes": [
            {"id": "n1", "name": "主干一", "node_type": "main", "activation": {"mode": "always"}, "atoms": [{"id": "a1", "name": "主干一", "element_groups": [{"role": "main", "elements": [{"value": "开始", "main": True, "pool": []}]}]}]},
            {"id": "n_stop", "name": "终止", "node_type": "terminal", "activation": {"mode": "user_triggered", "trigger_groups": [{"elements": [{"value": "不方便", "main": True, "pool": ["没空"]}]}]}, "required": False, "atoms": [{"id": "a_stop", "name": "安全结束", "element_groups": [{"role": "main", "elements": [{"value": "再联系", "main": True, "pool": []}]}]}]},
            {"id": "n_later", "name": "后续主干", "node_type": "main", "activation": {"mode": "always"}, "atoms": [{"id": "a_later", "name": "后续", "element_groups": [{"role": "main", "elements": [{"value": "后续", "main": True, "pool": []}]}]}]},
        ],
        "edges": [
            {"source": "n1", "target": "n_stop", "type": "terminal_after", "terminal_effect": {"suppress_nodes": ["n_later"]}},
            {"source": "n_stop", "target": "n_later", "type": "before"},
        ],
    })
    term_eval = GraphEvaluator(terminal_graph, {"weights": {}}).evaluate({"id": "t", "turns": [{"speaker": "assistant", "text": "开始"}, {"speaker": "user", "text": "我现在没空"}, {"speaker": "assistant", "text": "那我们稍后再联系"}]})
    tnodes = {n.node_id: n for n in term_eval.node_results}
    assert tnodes["n_stop"].active and tnodes["n_later"].status == "不适用", "terminal transition must suppress downstream required nodes"

    # Exclusive branch groups should surface duplicate branch choices.
    exclusive_graph = StateGraph.from_dict({
        "graph_id": "exclusive_check",
        "nodes": [
            {"id": "a", "name": "分支A", "node_type": "branch", "activation": {"mode": "user_triggered", "trigger_groups": [{"elements": [{"value": "A", "main": True, "pool": []}]}]}, "atoms": [{"id": "aa", "name": "A", "element_groups": [{"role": "main", "elements": [{"value": "A", "main": True, "pool": []}]}]}]},
            {"id": "b", "name": "分支B", "node_type": "branch", "activation": {"mode": "user_triggered", "trigger_groups": [{"elements": [{"value": "B", "main": True, "pool": []}]}]}, "atoms": [{"id": "bb", "name": "B", "element_groups": [{"role": "main", "elements": [{"value": "B", "main": True, "pool": []}]}]}]},
        ],
        "relation_groups": [{"group_id": "rg_ex", "name": "互斥分支", "relation": "exclusive_branch", "nodes": ["a", "b"], "required": False}],
    })
    ex_eval = GraphEvaluator(exclusive_graph, {"weights": {}}).evaluate({"id": "ex", "turns": [{"speaker": "assistant", "text": "A"}, {"speaker": "assistant", "text": "B"}]})
    assert any(e.status == "互斥分支重复触发" for e in ex_eval.relation_events), "exclusive branch relation must detect multiple chosen branches"

    print("atom/element logic self-check passed")


if __name__ == "__main__":
    main()
