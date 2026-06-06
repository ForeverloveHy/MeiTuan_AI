from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sceg.graph_language import chinese_context_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check graph human-facing fields are in Chinese context.")
    parser.add_argument("graphs", nargs="+", help="graph.json paths")
    parser.add_argument("--allow-fail", action="store_true", help="print report but do not fail")
    args = parser.parse_args()
    ok = True
    for raw in args.graphs:
        path = Path(raw)
        data = json.loads(path.read_text(encoding="utf-8"))
        report = chinese_context_report(data)
        print(
            f"language {path}: passed={report['passed']} human={report['human_string_count']} "
            f"english_issues={report['english_issue_count']} ratio={report['english_issue_ratio']}"
        )
        for issue in report.get("sample_issues", [])[:8]:
            print(f"  - {issue['path']}: {issue['text']}")
        ok = ok and bool(report.get("passed"))
    if args.allow_fail:
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
