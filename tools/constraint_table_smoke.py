from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.sceg.schema_atomic_pipeline import sanitize_constraint_tables


def main() -> None:
    sample = {
        "hard_constraint_table": [
            {"id":"soft_001","name":"回复长度控制","constraint_kind":"fuzzy_quality","quality_dimension":"简洁","metric":"字数","negative_groups":[{"elements":[{"value":"回复冗长","main":True,"fact":False,"pool":[]}]}]},
            {"id":"h1","name":"禁止承诺优惠券","text":"不能承诺优惠券","negative_groups":[{"elements":[{"value":"优惠券","main":True,"fact":False,"pool":[]},{"value":"承诺","main":True,"fact":False,"pool":[]}]}],"safe_groups":[{"elements":[{"value":"优惠券","main":True,"fact":False,"pool":[]},{"value":"以官方活动为准","main":False,"fact":False,"pool":[]}]}]},
        ] * 20,
        "soft_constraint_table": []
    }
    out = sanitize_constraint_tables(sample, "平台优惠券以官方活动为准，客服不能承诺优惠券。")
    assert len(out["hard_constraint_table"]) <= 10, len(out["hard_constraint_table"])
    assert len(out["hard_constraint_table"]) >= 1
    assert all(x.get("constraint_kind") == "semantic_object" for x in out["hard_constraint_table"])
    assert out.get("metadata", {}).get("constraint_sanitize", {}).get("hard_before", 0) >= 2
    print("constraint_table_smoke passed")

if __name__ == "__main__":
    main()
