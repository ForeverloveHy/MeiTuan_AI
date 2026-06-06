from __future__ import annotations

import argparse
import json
from pathlib import Path

from sceg.schema import StateGraph


def main() -> int:
    parser = argparse.ArgumentParser(description="Load one or more graph JSON files through the runtime schema.")
    parser.add_argument("graphs", nargs="+", help="graph.json paths")
    args = parser.parse_args()
    for raw in args.graphs:
        path = Path(raw)
        data = json.loads(path.read_text(encoding="utf-8"))
        graph = StateGraph.from_dict(data)
        print(
            f"loaded {path}: nodes={len(graph.nodes)} relation_groups={len(graph.relation_groups)} "
            f"knowledge={len(graph.knowledge)} constraints={len(graph.constraints)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
