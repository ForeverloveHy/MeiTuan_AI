#!/usr/bin/env python3
from __future__ import annotations

from sceg.schema_final_tightener import tighten_graph_contracts


def main() -> int:
    graph = {
        "nodes": [
            {"id": "n1", "type": "start", "node_type": "start", "activation": {"mode": "always"}, "atoms": []},
            {"id": "n2", "type": "branch", "node_type": "main", "required": False, "activation": {"mode": "user_triggered", "trigger_hint": "确认后进入"}, "atoms": []},
            {"id": "n3", "type": "branch", "node_type": "branch", "activation": {"mode": "condition", "trigger_hint": "用户忙碌", "trigger_groups": [{"elements": [{"value": "忙碌", "main": True, "fact": False}]}]}, "atoms": [{"id": "a3", "text": "就一分钟"}]},
            {"id": "n4", "type": "main", "node_type": "main", "activation": {"mode": "always"}, "atoms": [{"id": "a4", "text": "询问是否已经处理"}]},
            {"id": "n5", "type": "terminal", "node_type": "terminal", "activation": {"mode": "optional", "trigger_hint": "用户表示开车"}, "atoms": []},
        ],
        "edges": [{"source": "n3", "target": "n4", "type": "suppress_after", "relation": "suppress_after"}],
        "relation_groups": [{"id": "rg", "type": "sequential", "required": True, "nodes": ["n1", "n2", "n4"]}],
        "knowledge_table": [
            {"id": "k1", "correct_groups": [{"elements": [{"value": "保住结果", "main": False, "fact": True}]}]},
            {"id": "k2", "correct_groups": [{"elements": [{"value": "在App中自己操作", "main": False, "fact": True}]}]},
        ],
        "hard_constraint_table": [],
    }
    fixed = tighten_graph_contracts(graph)
    n2 = next(n for n in fixed["nodes"] if n["id"] == "n2")
    n5 = next(n for n in fixed["nodes"] if n["id"] == "n5")
    assert n2["activation"]["mode"] == "always", n2
    assert fixed["edges"][0]["type"] == "required_after", fixed["edges"][0]
    assert n5["activation"]["mode"] == "condition", n5
    assert n5["activation"].get("trigger_groups"), n5
    assert "已提供" in next(n for n in fixed["nodes"] if n["id"] == "n4")["atoms"][0]["text"]
    assert len(fixed["hard_constraint_table"]) >= 2, fixed["hard_constraint_table"]
    print("final_tightener_smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
