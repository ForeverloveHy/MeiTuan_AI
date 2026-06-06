from __future__ import annotations

from sceg.llm_client import extract_json_object, _salvage_stage_json


def main() -> None:
    broken_elements = '{"element_refinements":[{"atom_id":"a1","element_groups":[{"elements":[{"value":"身份确认","main":true,"fact":false,"pool":[]}]}]} {"atom_id":"a2","element_groups":[{"elements":[{"value":"状态开启","main":true,"fact":false,"pool":[]}]}]}]}'
    obj = extract_json_object(broken_elements)
    assert len(obj.get("element_refinements") or []) == 2

    broken_knowledge = '{"knowledge_table":[{"id":"k1","name":"A","text":"A"} {"id":"k2","name":"B","text":"B"}]}'
    obj2 = _salvage_stage_json(broken_knowledge, "knowledge_table_supplement_only")
    assert obj2 and len(obj2.get("knowledge_table") or []) == 2

    broken_constraints = '{"hard_constraint_table":[{"id":"hc1","name":"禁止承诺","text":"禁止承诺系统结果","negative_groups":[]} {"id":"hc2","name":"禁止代操作","text":"禁止代替对象操作","negative_groups":[]}],"soft_constraint_table":[]}'
    obj3 = _salvage_stage_json(broken_constraints, "constraint_tables_supplement_only")
    assert obj3 and len(obj3.get("hard_constraint_table") or []) == 2
    print("json salvage smoke test passed")


if __name__ == "__main__":
    main()
