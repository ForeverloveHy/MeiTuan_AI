from __future__ import annotations

try:
    from src.sceg.schema_atomic_pipeline import build_atom_transport, merge_element_anchor_delta
except ModuleNotFoundError:
    from sceg.schema_atomic_pipeline import build_atom_transport, merge_element_anchor_delta


def main() -> None:
    graph = {
        "nodes": [
            {"id": "n1", "node_type": "branch", "activation": {"mode": "condition", "trigger_hint": "用户表示现在很忙", "trigger_groups": [{"elements": [{"value": "忙", "main": True, "fact": False, "pool": []}]}]}, "atoms": []},
            {"id": "n2", "node_type": "main", "activation": {"mode": "always"}, "atoms": [{"id": "a1", "text": "提醒用户稍后查看结果", "element_groups": [{"elements": [{"value": "稍后查看", "main": True, "fact": False, "pool": []}]}]}]},
        ],
    }
    transport = build_atom_transport(graph, "")
    activation = next(e for e in transport["entries"] if e["atom_source"] == "activation")
    assert activation["role_aware_element_hints"]["derivation_mode"] == "user_text_variants_then_elementize"
    assert "likely_user_utterance_hints" not in activation["role_aware_element_hints"]
    node_atom = next(e for e in transport["entries"] if e["atom_source"] == "node_atom")
    assert node_atom["role_aware_element_hints"]["derivation_mode"] == "assistant_element_first"

    raw_pool = {"secondary_expansions": [{"atom_id": node_atom["atom_id"], "element_groups": [{"elements": [{"value": "稍后查看", "main": True, "fact": False, "pool": ["晚点看", "稍后再看"]}]}]}]}
    merged = merge_element_anchor_delta(graph, raw_pool, secondary_only=True)
    pool = merged["nodes"][1]["atoms"][0]["element_groups"][0]["elements"][0].get("pool") or []
    assert "晚点看" in pool and "稍后再看" in pool

    raw_trigger = {"secondary_expansions": [{"atom_id": activation["atom_id"], "trigger_groups": [
        {"source_text": "我现在没空", "elements": [{"value": "没空", "main": True, "fact": False, "pool": ["没时间", "不方便"]}]},
        {"source_text": "我这会儿有事", "elements": [{"value": "有事", "main": True, "fact": False, "pool": ["在忙", "走不开"]}]},
    ]}]}
    merged2 = merge_element_anchor_delta(graph, raw_trigger, secondary_only=True)
    groups = merged2["nodes"][0]["activation"].get("trigger_groups") or []
    values = [e.get("value") for g in groups for e in (g.get("elements") or [])]
    assert "没空" in values and "有事" in values
    print("role_aware_element_smoke passed")

if __name__ == "__main__":
    main()
