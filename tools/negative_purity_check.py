from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _assistant_text(dialogue: dict[str, Any]) -> str:
    parts: list[str] = []
    for turn in dialogue.get("turns") or dialogue.get("messages") or dialogue.get("dialogue") or []:
        if not isinstance(turn, dict):
            continue
        if str(turn.get("speaker") or "").lower() in {"assistant", "agent", "客服"}:
            parts.append(str(turn.get("text") or turn.get("content") or ""))
    return "\n".join(parts)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def check_negative_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive cli branch
        return [f"{path}: JSON读取失败：{exc}"]
    if str(data.get("sample_type") or "").lower() not in {"negative", "负包"}:
        return []
    errors = data.get("injected_errors") or []
    issues: list[str] = []
    if not errors:
        issues.append(f"{path}: 负包缺少 injected_errors")
        return issues
    text = _norm(_assistant_text(data))
    for idx, err in enumerate(errors):
        if not isinstance(err, dict):
            issues.append(f"{path}: injected_errors[{idx}] 不是对象")
            continue
        family = str(err.get("error_family") or "").strip()
        span = str(err.get("evidence_span") or err.get("wrong_statement") or err.get("violation_statement") or "").strip()
        evaluability = str(err.get("evaluability") or "").lower()
        detector = str(err.get("expected_detector") or "").lower()
        grey_allowed = bool(data.get("allow_oracle_grey_zone") or err.get("oracle_grey_zone"))
        if family == "semantic_or_context" or evaluability in {"semantic", "open_set"} or detector in {"semantic_node_coverage", "audit_only"}:
            # A small, explicitly marked grey-zone subset is allowed so the
            # dataset can exercise LongCat arbitration.  It still must provide
            # a target id and a traceable assistant span; otherwise it would be
            # an uncheckable negative sample rather than a controlled review case.
            if not grey_allowed:
                issues.append(f"{path}: injected_errors[{idx}] 仍是语义灰区，正式负包应优先改成可回查错句或明确本地目标")
        if family in {"knowledge_violation", "knowledge_error", "faq_wrong", "fact_wrong", "知识错误"}:
            if not (err.get("knowledge_id") or err.get("target_knowledge_id")):
                issues.append(f"{path}: injected_errors[{idx}] 知识错误缺少 knowledge_id")
            if not span:
                issues.append(f"{path}: injected_errors[{idx}] 缺少 evidence_span")
            elif _norm(span) not in text:
                issues.append(f"{path}: injected_errors[{idx}] evidence_span 未出现在客服原话中：{span}")
        if family in {"constraint_violation", "boundary_violation", "限制违规"}:
            if not (err.get("constraint_id") or err.get("target_constraint_id")):
                issues.append(f"{path}: injected_errors[{idx}] 限制违规缺少 constraint_id")
            if not span:
                issues.append(f"{path}: injected_errors[{idx}] 缺少 evidence_span")
            elif _norm(span) not in text:
                issues.append(f"{path}: injected_errors[{idx}] evidence_span 未出现在客服原话中：{span}")
        if family in {"flow_missing", "process_missing", "流程缺失"}:
            if not (err.get("node_id") or err.get("target_node_id")):
                issues.append(f"{path}: injected_errors[{idx}] 流程缺失缺少 node_id")
            if not (err.get("requirement_id") or err.get("target_core") or err.get("target_group_id")):
                issues.append(f"{path}: injected_errors[{idx}] 流程缺失缺少 requirement_id")
    return issues


def scan(root: Path) -> list[str]:
    files = sorted(root.rglob("*.json"))
    issues: list[str] = []
    negative_total = 0
    grey_total = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if str(data.get("sample_type") or "").lower() in {"negative", "负包"}:
            negative_total += 1
            if data.get("allow_oracle_grey_zone"):
                grey_total += 1
        issues.extend(check_negative_file(path))
    # Keep the arbitration subset controlled: enough for review, not enough to
    # hide weak negative data behind LLM arbitration.
    if negative_total and grey_total / negative_total > 0.12:
        issues.append(f"{root}: 语义灰区负包比例过高：{grey_total}/{negative_total}，应控制在12%以内")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="检查正式负包是否存在语义灰区或不可回查错句。")
    parser.add_argument("root", nargs="?", default="data/dialogues/negative_pack", help="负包根目录")
    args = parser.parse_args()
    issues = scan(Path(args.root))
    if issues:
        print("负包纯度检查未通过：")
        print("\n".join(issues))
        raise SystemExit(1)
    print("negative purity check passed")


if __name__ == "__main__":
    main()
