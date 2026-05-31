from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    (
        ROOT / "src" / "sceg2" / "dataset_interface.py",
        "evidence_span shortcut must not directly accept negatives",
        "if evidence_span and self._span_present_in_dialogue(evidence_span, dialogue):\n                return True",
    ),
    (
        ROOT / "src" / "sceg2" / "demo_runner.py",
        "LongCat binding hints must not include sample user utterances",
        '"sample_user_texts"',
    ),
    (
        ROOT / "src" / "sceg2" / "demo_runner.py",
        "LongCat binding hints must not include sample assistant utterances",
        '"sample_assistant_texts"',
    ),
    (
        ROOT / "src" / "sceg2" / "demo_runner.py",
        "LongCat binding hints must not include injected wrong statements",
        '"wrong_statement": _clip_hint',
    ),
]


def _compact(value: object) -> str:
    import re
    return re.sub(r"\s+", "", str(value or "").lower())


def _collect_spans(root: Path) -> set[str]:
    import json
    spans: set[str] = set()
    droot = root / "data" / "dialogues"
    if not droot.exists():
        return spans
    for path in droot.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for err in data.get("injected_errors") or []:
            for key in ("evidence_span", "wrong_statement"):
                val = _compact(err.get(key))
                if len(val) >= 6:
                    spans.add(val)
    return spans


def _collect_pattern_texts(value: object, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k) in {"any", "all", "regex_any"}:
                _collect_pattern_texts(v, out)
            elif isinstance(v, (dict, list)):
                _collect_pattern_texts(v, out)
    elif isinstance(value, list):
        for item in value:
            _collect_pattern_texts(item, out)
    elif isinstance(value, str):
        out.append(value)


def _find_graph_span_leaks(root: Path, spans: set[str]) -> list[str]:
    import json
    if not spans:
        return []
    errors: list[str] = []
    for graph_root in sorted((root / "data").glob("graphs*")):
        if not graph_root.is_dir():
            continue
        for path in graph_root.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            texts: list[str] = []
            _collect_pattern_texts(data.get("knowledge_table") or [], texts)
            _collect_pattern_texts(data.get("constraint_table") or [], texts)
            for t in texts:
                ct = _compact(t)
                if not ct:
                    continue
                for span in spans:
                    if span and (ct == span or span in ct):
                        errors.append(f"{path.relative_to(root)} contains answer-key span in executable schema: {t[:80]}")
                        break
    return errors


def main() -> int:
    errors: list[str] = []
    for path, label, snippet in CHECKS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if snippet in text:
            errors.append(f"{path.relative_to(ROOT)}: {label}")

    schema = ROOT / "src" / "sceg2" / "schema_compiler.py"
    if schema.exists():
        text = schema.read_text(encoding="utf-8", errors="ignore")
        call = "_compile_legacy_evidence_patterns(graph, legacy)"
        if call in text:
            idx = text.find(call)
            window = text[max(0, idx - 160):idx]
            if "if allow_legacy_exact_patterns" not in window:
                errors.append("src/sceg2/schema_compiler.py: legacy exact wrong statements are compiled without explicit debug flag")

    spans = _collect_spans(ROOT)
    errors.extend(_find_graph_span_leaks(ROOT, spans))

    if errors:
        print("anti leak guard failed")
        for err in errors:
            print("-", err)
        return 1
    print("anti leak guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
