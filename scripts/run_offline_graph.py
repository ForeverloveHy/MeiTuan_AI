#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sceg.demo_runner import run_offline_project


def main() -> None:
    parser = argparse.ArgumentParser(description="Run evaluation from an existing graph JSON without instruction graph building")
    parser.add_argument("--graph", required=True, help="Path to graph.json")
    parser.add_argument("--dialogues", default="data/dialogues", help="Dialogue root or JSON file")
    parser.add_argument("--pack", choices=["all", "positive", "negative"], default="all")
    parser.add_argument("--max", type=int, default=None, help="Optional dialogue limit")
    parser.add_argument("--llm-mode", choices=["off", "shadow", "assist"], default="off")
    parser.add_argument("--llm-max-items", type=int, default=-1)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--report-mode", choices=["simple", "detail"], default="detail")
    args = parser.parse_args()

    result = run_offline_project(
        graph_path=args.graph,
        project_root=ROOT,
        dialogue_root=args.dialogues,
        max_dialogues=args.max,
        pack_type=None if args.pack == "all" else args.pack,
        llm_verifier_mode=args.llm_mode,
        llm_verifier_max_items=args.llm_max_items,
        llm_api_key=args.api_key,
        llm_base_url=args.base_url,
        llm_model=args.model,
        report_mode=args.report_mode,
    )
    print("完成离线评估：%s 条" % result["dialogue_count"])
    print("JSON：%s" % result["all_reports_merged"])
    print("HTML：%s" % result["html_report"])
    print("上传包：%s" % result["upload_bundle"])


if __name__ == "__main__":
    main()
