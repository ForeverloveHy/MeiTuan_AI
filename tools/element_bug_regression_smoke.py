from __future__ import annotations

import json
from pathlib import Path

try:
    from sceg.element_engine import ElementEngine
    from sceg.evidence_units import EvidenceUnit
    from sceg.normalizer import normalize_text
    from sceg.schema_atomic_pipeline import sanitize_constraint_tables
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from sceg.element_engine import ElementEngine
    from sceg.evidence_units import EvidenceUnit
    from sceg.normalizer import normalize_text
    from sceg.schema_atomic_pipeline import sanitize_constraint_tables


def _atoms(text: str, speaker: str = "user"):
    unit = EvidenceUnit(turn_index=0, speaker=speaker, text=text, normalized=normalize_text(text))
    return ElementEngine().build_atoms([unit])


def main() -> None:
    engine = ElementEngine()
    trigger_rule = engine.make_rule(
        "trigger_false_positive",
        "node_trigger",
        element_groups=[
            {"elements": [
                {"value": "参与者", "main": True, "fact": False, "pool": ["我"]},
                {"value": "不方便", "main": True, "fact": False, "pool": ["没空", "忙"]},
            ]}
        ],
    )
    assert engine.match_rule(trigger_rule, _atoms("是我，你说")).verdict == "miss"
    assert engine.match_rule(trigger_rule, _atoms("我现在没空")).verdict == "hit"

    duplicate_raw = {
        "hard_constraint_table": [
            {
                "id": "h1", "name": "禁用语气词", "enforcement": "hard",
                "negative_groups": [{"elements": [{"value": "好的", "main": True, "fact": False}]}],
            },
            {
                "id": "h2", "name": "禁止使用明确禁用表达", "enforcement": "hard",
                "negative_groups": [{"elements": [{"value": "禁用表达", "main": True, "fact": False}, {"value": "好的", "main": True, "fact": False}]}],
            },
            {
                "id": "h3", "name": "禁止承诺折扣券或优惠券", "enforcement": "hard",
                "negative_groups": [{"elements": [{"value": "折扣券", "main": True, "fact": False}, {"value": "承诺", "main": False, "fact": True}]}],
            },
            {
                "id": "h4", "name": "禁止无依据承诺", "enforcement": "hard",
                "negative_groups": [{"elements": [{"value": "优惠权益", "main": True, "fact": False}, {"value": "承诺", "main": False, "fact": True}]}],
            },
        ],
        "soft_constraint_table": [],
    }
    cleaned = sanitize_constraint_tables(duplicate_raw, "不能承诺给用户折扣券或优惠券；不说好的、哈哈等语气词")
    hard_ids = [x.get("id") for x in cleaned.get("hard_constraint_table", [])]
    assert "h1" in hard_ids and "h2" not in hard_ids, hard_ids
    assert "h3" in hard_ids and "h4" not in hard_ids, hard_ids
    print("element bug regression smoke passed")


if __name__ == "__main__":
    main()
