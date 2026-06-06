from __future__ import annotations

import re
from typing import Any

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# Keys whose values are meant for humans or semantic matching. Technical ids,
# enums, paths, URLs, model names and runtime metadata are intentionally ignored.
_HUMAN_KEYS = {
    "name", "text", "summary", "trigger_hint", "condition", "description",
    "value", "pool", "quality_dimension", "metric", "score_effect", "reason",
}
_TECH_KEY_HINTS = {
    "id", "node_id", "atom_id", "edge_id", "constraint_id", "group_id",
    "graph_id", "element_anchor_id", "source", "target", "from", "to",
    "type", "relation", "mode", "severity", "enforcement", "constraint_kind",
    "judge_type", "source_kind", "model", "base_url", "usage_source", "purpose",
    "path", "phase", "schema_mode", "schema_generation_mode", "schema_compiler",
    "graph_source", "merge_policy", "expansion_source", "llm_model",
}
_ALLOWED_LATIN_ONLY = {
    "app", "web", "saas", "api", "json", "html", "faq", "llm", "url",
    "id", "ui", "pc", "h5", "sms", "crm", "erp", "sdk", "ios", "tone", "closing", "similarity",
}
_ALLOWED_LATIN_IN_MIXED = _ALLOWED_LATIN_ONLY | {"a", "b", "c"}


def _has_chinese(s: str) -> bool:
    return bool(_CHINESE_RE.search(s or ""))


def _has_latin(s: str) -> bool:
    return bool(_LATIN_RE.search(s or ""))


def _latin_words(s: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]*", s or "")]


def _looks_technical_key(key: str) -> bool:
    key = str(key or "")
    if key in _TECH_KEY_HINTS:
        return True
    return key.endswith("_id") or key.endswith("_ids") or key.endswith("_url") or key.endswith("_path")


def _is_allowed_human_string(s: str) -> bool:
    text = str(s or "").strip()
    if not text or not _has_latin(text):
        return True
    words = _latin_words(text)
    if not words:
        return True
    if _has_chinese(text):
        # Chinese context may legitimately contain App/Web/SaaS/API etc.
        return all(w in _ALLOWED_LATIN_IN_MIXED or re.fullmatch(r"[a-z][0-9]?", w) for w in words)
    # Pure Latin strings are acceptable only when they are tiny product/format tokens.
    compact = re.sub(r"[^A-Za-z0-9]", "", text).lower()
    if compact in _ALLOWED_LATIN_ONLY:
        return True
    if len(text) <= 3 and compact in _ALLOWED_LATIN_ONLY:
        return True
    # Soft quality metric identifiers such as style_score/char_count are
    # configuration names, not dialogue semantics.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) and "_" in text:
        return True
    return False


def _walk_human_strings(obj: Any, path: str = "$", key: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sk = str(k)
            # Skip runtime metadata subtrees except summary-like content.
            # These fields may contain technical diagnostic tokens such as
            # duplicate_hard_cluster and should not be treated as dialogue
            # semantics for the Chinese-context gate.
            if path.endswith(".metadata") and sk in {"llm_token_usage", "schema_linter_report", "schema_element_refinement_runs", "schema_element_expansion_runs", "atom_registry_summary", "llm_phase_timing_seconds", "llm_cache_path", "llm_cache_hit", "constraint_sanitize"}:
                continue
            out.extend(_walk_human_strings(v, f"{path}.{sk}", sk))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_human_strings(v, f"{path}[{i}]", key))
    elif isinstance(obj, str):
        if key in _HUMAN_KEYS and not _looks_technical_key(key):
            out.append((path, obj))
    return out


def chinese_context_report(graph: dict[str, Any], max_english_only: int = 6, max_issue_ratio: float = 0.08) -> dict[str, Any]:
    strings = _walk_human_strings(graph)
    issues: list[dict[str, str]] = []
    for path, text in strings:
        if not _is_allowed_human_string(text):
            issues.append({"path": path, "text": text[:160]})
    total = max(1, len(strings))
    ratio = len(issues) / total
    passed = len(issues) <= max_english_only and ratio <= max_issue_ratio
    return {
        "passed": passed,
        "human_string_count": len(strings),
        "english_issue_count": len(issues),
        "english_issue_ratio": round(ratio, 4),
        "sample_issues": issues[:20],
    }


def assert_chinese_context(graph: dict[str, Any]) -> None:
    report = chinese_context_report(graph)
    if report.get("passed"):
        return
    samples = "; ".join(f"{x['path']}={x['text']}" for x in report.get("sample_issues", [])[:6])
    raise RuntimeError(
        "生成图不符合中文语境：检测到过多英文人类可读语义字段。"
        f" english_issue_count={report.get('english_issue_count')} "
        f"ratio={report.get('english_issue_ratio')}。示例：{samples}。"
        "请清空旧缓存后重跑，或检查五份 prompt 的中文语境约束。"
    )
