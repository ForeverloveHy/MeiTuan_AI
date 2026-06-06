from __future__ import annotations

from sceg.schema import StateGraph
from sceg.graph_evaluator import GraphEvaluator


def test_faq_atom_scope() -> None:
    graph = StateGraph.from_dict({
        "graph_id": "g",
        "name": "g",
        "nodes": [
            {"id":"n1","name":"FAQ汇总","type":"faq","required":False,
             "activation":{"mode":"user_triggered","trigger_groups":[{"elements":[{"value":"取消计划","main":True,"pool":["怎么取消"]}]}]},
             "atoms":[
                 {"id":"a_cancel","text":"说明取消方式","element_groups":[{"elements":[{"value":"取消计划","main":True,"pool":["取消"]},{"value":"前一天18点","main":True,"pool":["18点前"]}]}]},
                 {"id":"a_bonus","text":"说明激励规则","element_groups":[{"elements":[{"value":"激励","main":True},{"value":"7天","main":True}]}]},
             ]}
        ],
        "edges":[],"knowledge_table":[],"hard_constraint_table":[],"soft_constraint_table":[],
    })
    ev = GraphEvaluator(graph, {"thresholds": {}}).evaluate({"id":"d","turns":[
        {"speaker":"user","text":"这个计划怎么取消？"},
        {"speaker":"assistant","text":"要在前一天18点前取消。"},
    ]})
    node = ev.node_results[0]
    assert node.active
    reqs = {r.requirement_id: r for r in node.requirement_results}
    assert reqs["a_cancel"].matched, reqs["a_cancel"].element_audit
    assert reqs["a_bonus"].required is False and reqs["a_bonus"].matched, reqs["a_bonus"].element_audit


def test_user_provided_info_skip() -> None:
    graph = StateGraph.from_dict({
        "graph_id": "g2",
        "name": "g2",
        "nodes": [
            {"id":"n1","name":"确认办理渠道","type":"main","required":True,
             "activation":{"mode":"always"},
             "atoms":[
                 {"id":"a_ask","text":"询问用户是通过渠道甲、渠道乙还是渠道丙办理", "element_groups":[{"elements":[{"value":"渠道甲","main":True},{"value":"渠道丙","main":True},{"value":"办理","main":False}]}]},
             ]}
        ],
        "edges":[],"knowledge_table":[],"hard_constraint_table":[],"soft_constraint_table":[],
    })
    ev = GraphEvaluator(graph, {"thresholds": {"info_request_user_provided_satisfies": True}}).evaluate({"id":"d2","turns":[
        {"speaker":"user","text":"我们通过渠道丙办理。"},
        {"speaker":"assistant","text":"那我按对应渠道给您说明。"},
    ]})
    req = ev.node_results[0].requirement_results[0]
    assert req.required is False and req.matched and req.score == 1.0, req.element_audit


if __name__ == "__main__":
    test_faq_atom_scope()
    test_user_provided_info_skip()
    print("activation scope smoke passed")
