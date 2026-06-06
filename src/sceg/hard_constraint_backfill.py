from __future__ import annotations

"""Generic hard-constraint candidate extraction and repair.

This module is intentionally domain-neutral.  It reads only the original complex
instruction plus the model's constraint tables.  It never reads dialogue labels,
negative-pack annotations, evidence spans, wrong statements, graph nodes, or
knowledge rows.
"""

import copy
import hashlib
import re
from typing import Any


_NEG_PREFIX_RE = re.compile(r"(不能|不得|不许|不允许|禁止|严禁|不可|不要|不应|别)")
_PROMISE_ACTION_RE = re.compile(r"(承诺|保证|确保|包|一定|肯定|必然|给出明确结果)")
_AUTH_ACTION_RE = re.compile(r"(代为|代替|帮.*操作|人工.*(改|调|处理)|越权|擅自|编造|私下|绕过)")
_STOP_STATE_RE = re.compile(r"(开车|驾驶|骑行|路上|危险|不方便.*接|不方便.*听|无法.*沟通|安全)")
_STOP_SAFE_RE = re.compile(r"(稍后再打|稍后联系|晚点.*打|挂断|先不打扰|安全.*要紧|方便.*再)")
_SCOPE_RE = re.compile(r"(超出职责范围|职责范围外|超出.*范围|不属于.*范围|无法.*确认|不能.*确认)")
_SCOPE_SAFE_RE = re.compile(r"(同事确认|确认后.*回|核实后.*回|能回答.*先回答|稍后.*回复)")
_SPECIFIC_PHRASE_BAN_RE = re.compile(r"(?:不能|不得|不许|不允许|禁止|严禁|不可|不要|不说|别说|禁用)(?:使用|说|输出|回复|包含)?(?P<body>[^。；;\n]{0,80})(?:等)?(?:词|话术|表达|语气词|口头禅|字样)")
_QUOTED_RE = re.compile(r"[\"'“”‘’「」『』《》](.{1,16}?)[\"'“”‘’「」『』《》]")
_INLINE_LIST_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8})(?:、|/|，|,)")
_BENEFIT_RE = re.compile(r"(权益|优待|减免|返还|补偿|赠送|补贴|券|礼包|福利|额度|资源)")
_SYSTEM_RESULT_RE = re.compile(r"(系统|平台|页面|入口|配置|权限|账号|结果|状态|记录|审核|展示|开通|生效|通过|可用|成功)")
_MANUAL_AUTH_RE = re.compile(r"(人工|客服|工作人员|运营|上级|后台).{0,12}(干预|决定|改|调|处理|操作|控制)")
_NOT_MANUAL_AUTH_RE = re.compile(r"(并非|不是|不能|无法|不由|不会).{0,16}(人工|客服|工作人员|运营|上级|后台).{0,16}(干预|决定|改|调|处理|操作|控制)")

_GENERIC_OBJECT_WORDS = ("结果", "状态", "权限", "配置", "入口", "记录", "权益", "资源", "安全状态", "职责范围外问题", "禁用表达")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return ""


def _snippet(text: str, start: int, end: int, pad: int = 36) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return text[lo:hi].strip()


def _stable_id(prefix: str, seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def _elements(*values: tuple[str, bool, bool]) -> dict[str, Any]:
    return {"elements": [{"value": v, "main": main, "fact": fact, "pool": []} for v, main, fact in values if v]}


def _candidate(
    *,
    kind: str,
    quote: str,
    restricted_object: str,
    forbidden_action: str,
    safe_action: str,
    severity: str = "high",
    must_be_hard: bool = True,
) -> dict[str, Any]:
    return {
        "candidate_id": _stable_id("hcand", kind + quote + restricted_object + forbidden_action + safe_action),
        "source_quote": quote,
        "boundary_kind": kind,
        "restricted_object": restricted_object,
        "forbidden_action": forbidden_action,
        "safe_action": safe_action,
        "severity": severity,
        "must_be_hard": must_be_hard,
    }


def _specific_promise_object(window: str) -> str:
    text = str(window or "")
    phrase_pairs = (
        ("折扣券", "优惠券", "折扣券或优惠券"),
        ("优惠券", "折扣券", "折扣券或优惠券"),
    )
    for a, b, out in phrase_pairs:
        if a in text and b in text:
            return out
    for key in ("折扣券", "优惠券", "代金券", "抵用券", "补偿", "补贴", "福利", "权益", "优惠", "收费"):
        if key in text:
            return key
    if _BENEFIT_RE.search(text) and _SYSTEM_RESULT_RE.search(text):
        return "权益或系统结果"
    if _BENEFIT_RE.search(text):
        return "权益"
    if _SYSTEM_RESULT_RE.search(text):
        return "系统结果"
    return "受限结果"


def _specific_stop_object(window: str) -> str:
    text = str(window or "")
    if "开车" in text or "驾驶" in text:
        return "开车状态"
    if "骑行" in text:
        return "骑行状态"
    if "不方便" in text:
        return "不方便接听状态"
    return "安全状态"


def _extract_banned_phrases(body: str) -> list[str]:
    phrases: list[str] = []
    for m in _QUOTED_RE.finditer(body):
        val = m.group(1).strip()
        if 0 < len(val) <= 12 and val not in phrases:
            phrases.append(val)
    # Fallback for unquoted short enumerations.  Keep it conservative so style
    # sentences do not become lexical hard rules by accident.
    if not phrases and re.search(r"[、,，/]", body):
        parts = re.split(r"[、,，/\s]+", body)
        for part in parts:
            part = re.sub(r"^(比如|例如|如|含|包括|以及|和|或)", "", part).strip()
            if 1 <= len(part) <= 8 and not re.search(r"(自然|礼貌|简洁|清晰|正式|冗长|重复)", part) and part not in phrases:
                phrases.append(part)
    return phrases[:8]


def extract_hard_candidate_table(instruction: str, current_soft: Any | None = None) -> list[dict[str, Any]]:
    """Extract a conservative candidate table from explicit negative language.

    The output is not the final executable table.  It is a small, inspectable
    bridge: source_quote + boundary_kind + restricted_object + forbidden_action
    + safe_action.  A later deterministic step converts it into the formal hard
    table shape.
    """
    text = str(instruction or "")
    candidates: list[dict[str, Any]] = []

    for m in _SPECIFIC_PHRASE_BAN_RE.finditer(text):
        quote = _snippet(text, m.start(), m.end())
        phrases = _extract_banned_phrases(m.group("body") or quote)
        if phrases:
            candidates.append(_candidate(
                kind="forbidden_phrase",
                quote=quote,
                restricted_object="禁用表达",
                forbidden_action="使用明确禁用表达：" + "、".join(phrases),
                safe_action="不使用这些表达，直接说明必要内容",
                severity="high",
            ))

    # Explicit lexical bans sometimes appear only in a soft row because the model
    # misclassified them.  Promote via the same candidate shape.
    soft_text = _text(current_soft or "")
    for m in _SPECIFIC_PHRASE_BAN_RE.finditer(soft_text):
        quote = _snippet(soft_text, m.start(), m.end())
        phrases = _extract_banned_phrases(m.group("body") or quote)
        if phrases:
            candidates.append(_candidate(
                kind="forbidden_phrase_from_soft",
                quote=quote,
                restricted_object="禁用表达",
                forbidden_action="使用明确禁用表达：" + "、".join(phrases),
                safe_action="不使用这些表达，直接说明必要内容",
                severity="high",
            ))

    for m in _SCOPE_RE.finditer(text):
        window = _snippet(text, m.start(), m.end(), pad=80)
        safe = "向相关人员确认后再回复"
        sm = _SCOPE_SAFE_RE.search(window)
        if sm:
            safe = sm.group(0)
        candidates.append(_candidate(
            kind="out_of_scope_boundary",
            quote=window,
            restricted_object="职责范围外问题",
            forbidden_action="擅自解答、编造结论或越权承诺",
            safe_action=safe,
            severity="high",
        ))

    for m in _STOP_STATE_RE.finditer(text):
        window = _snippet(text, m.start(), m.end(), pad=80)
        if _STOP_SAFE_RE.search(window):
            safe = _STOP_SAFE_RE.search(window).group(0)
            candidates.append(_candidate(
                kind="safety_stop_boundary",
                quote=window,
                restricted_object=_specific_stop_object(window),
                forbidden_action="继续推进说明、追问或施压",
                safe_action=safe,
                severity="critical",
            ))

    # No-unfounded-promise patterns.  Only fire when a negative prefix and a
    # promise-like action appear near a restricted object class.
    for m in _NEG_PREFIX_RE.finditer(text):
        window = _snippet(text, m.start(), m.end(), pad=42)
        if _PROMISE_ACTION_RE.search(window) and (_BENEFIT_RE.search(window) or _SYSTEM_RESULT_RE.search(window)):
            obj = _specific_promise_object(window)
            candidates.append(_candidate(
                kind="no_unfounded_promise",
                quote=window,
                restricted_object=obj,
                forbidden_action="承诺、保证或确保",
                safe_action="不作承诺，以实际规则和可确认结果为准",
                severity="critical",
            ))
        if _AUTH_ACTION_RE.search(window) and _SYSTEM_RESULT_RE.search(window):
            candidates.append(_candidate(
                kind="no_unauthorized_operation",
                quote=window,
                restricted_object="系统状态或操作结果",
                forbidden_action="代操作、人工改动或越权处理",
                safe_action="说明可支持范围，引导按正规路径处理",
                severity="critical",
            ))

    for m in _NOT_MANUAL_AUTH_RE.finditer(text):
        window = _snippet(text, m.start(), m.end(), pad=48)
        candidates.append(_candidate(
            kind="no_manual_intervention_claim",
            quote=window,
            restricted_object="人工权限边界",
            forbidden_action="声称可人工干预、人工决定或后台调整",
            safe_action="说明按规则或系统机制处理，不能人工干预",
            severity="high",
        ))

    return _dedupe_candidates(candidates)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in candidates:
        key = "|".join(str(cand.get(k) or "") for k in ("boundary_kind", "restricted_object", "forbidden_action", "safe_action"))
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out[:10]


def candidate_to_hard_constraint(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    kind = str(candidate.get("boundary_kind") or "hard_boundary")
    cid = f"hard_backfill_{index:02d}_{_stable_id('', kind + str(candidate.get('source_quote') or ''))[-8:]}"
    obj = str(candidate.get("restricted_object") or "受限对象")
    bad = str(candidate.get("forbidden_action") or "违规动作")
    safe = str(candidate.get("safe_action") or "安全处理")
    sev = str(candidate.get("severity") or "high")
    name_map = {
        "forbidden_phrase": "禁止使用明确禁用表达",
        "forbidden_phrase_from_soft": "禁止使用明确禁用表达",
        "out_of_scope_boundary": "职责范围外不得擅自解答",
        "safety_stop_boundary": "安全状态下不得继续推进",
        "no_unfounded_promise": "禁止无依据承诺",
        "no_unauthorized_operation": "禁止越权代操作或人工改动",
        "no_manual_intervention_claim": "禁止声称可人工干预",
    }
    name = name_map.get(kind, "明确硬边界限制")
    return {
        "constraint_id": cid,
        "id": cid,
        "name": name,
        "enforcement": "hard",
        "constraint_kind": "semantic_object",
        "severity": sev,
        "source_quote": candidate.get("source_quote") or "",
        "atoms": [
            {
                "atom_id": cid + "_a1",
                "id": cid + "_a1",
                "name": name,
                "text": f"围绕{obj}，不得{bad}；应{safe}。",
                "severity": sev,
                "trigger_groups": [],
                "negative_groups": [_elements((obj, True, False), (bad, False, True))],
                "safe_groups": [_elements((obj, True, False), (safe, False, False))],
            }
        ],
    }


def build_hard_constraints_from_candidates(candidates: list[dict[str, Any]], max_new: int = 5) -> list[dict[str, Any]]:
    hard: list[dict[str, Any]] = []
    for cand in candidates:
        if not cand.get("must_be_hard", True):
            continue
        hard.append(candidate_to_hard_constraint(cand, len(hard) + 1))
        if len(hard) >= max_new:
            break
    return hard


def soft_rows_containing_explicit_hard_bans(soft: Any) -> tuple[list[dict[str, Any]], list[str]]:
    rows = [copy.deepcopy(x) for x in soft if isinstance(x, dict)] if isinstance(soft, list) else []
    promoted: list[dict[str, Any]] = []
    remove_ids: list[str] = []
    for row in rows:
        candidates = extract_hard_candidate_table("", [row])
        hard_rows = build_hard_constraints_from_candidates(candidates, max_new=3)
        if hard_rows:
            promoted.extend(hard_rows)
            rid = str(row.get("id") or row.get("constraint_id") or "")
            if rid:
                remove_ids.append(rid)
    return promoted, remove_ids


def ensure_hard_constraints_when_required(raw: dict[str, Any], instruction: str, max_new: int = 5) -> dict[str, Any]:
    """Return raw constraints with a deterministic hard backfill when needed.

    Backfill only occurs from explicit negative/boundary language in the original
    instruction or from soft rows that explicitly contain lexical bans.  This is
    not a fallback dictionary of business facts.
    """
    if not isinstance(raw, dict):
        raw = {"hard_constraint_table": [], "soft_constraint_table": []}
    out = copy.deepcopy(raw)
    hard = [copy.deepcopy(x) for x in out.get("hard_constraint_table") or [] if isinstance(x, dict)]
    soft = [copy.deepcopy(x) for x in out.get("soft_constraint_table") or [] if isinstance(x, dict)]

    promoted, remove_ids = soft_rows_containing_explicit_hard_bans(soft)
    if remove_ids:
        soft = [x for x in soft if str(x.get("id") or x.get("constraint_id") or "") not in set(remove_ids)]

    candidates = extract_hard_candidate_table(instruction, soft)
    backfilled = build_hard_constraints_from_candidates(candidates, max_new=max_new)

    existing_blob = _text(hard)
    additions: list[dict[str, Any]] = []
    for item in promoted + backfilled:
        blob = _text(item)
        obj = _text(item.get("atoms", [{}])[0].get("negative_groups", [])) if item.get("atoms") else blob
        # Do not duplicate a model-produced hard row covering the same generic boundary.
        if obj and obj in existing_blob:
            continue
        additions.append(item)

    if additions:
        hard.extend(additions)
    out["hard_constraint_table"] = hard
    out["soft_constraint_table"] = soft
    meta = out.setdefault("metadata", {}) if isinstance(out.get("metadata"), dict) else {}
    out["metadata"] = meta
    meta["hard_candidate_backfill"] = {
        "candidate_total": len(candidates),
        "candidates": candidates[:10],
        "promoted_soft_ids": remove_ids,
        "added_hard_total": len(additions),
        "policy": "explicit negative or boundary language only; no dialogue labels or task-specific hardcoding",
    }
    return out
