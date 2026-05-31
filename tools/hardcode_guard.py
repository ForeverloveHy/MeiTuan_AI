#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cfg = json.loads((ROOT / "config" / "hardcode_guard.json").read_text(encoding="utf-8"))
    terms = list(cfg.get("forbidden_terms", []))
    allowed = {str(ROOT / p) for p in cfg.get("allowed_files", [])}
    errors: list[str] = []
    exts = set(cfg.get("scan_extensions") or [".py"])
    for rel in cfg.get("scan_roots", ["src"]):
        base = ROOT / rel
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in exts:
                continue
            if str(path) in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            for term in terms:
                if term and term in text:
                    errors.append(f"{path.relative_to(ROOT)} contains forbidden business term: {term}")
    if errors:
        print("\n".join(errors))
        return 1
    print("hardcode guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
