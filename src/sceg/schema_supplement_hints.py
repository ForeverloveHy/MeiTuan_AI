from __future__ import annotations

"""Generic local gap hints for second-pass schema supplementation.

The hints in this module are deliberately task-agnostic.  They describe common
customer-service dialogue functions, fact-slot shapes, and boundary patterns so
LLM can repair an incomplete first pass without Python injecting any task
answer, domain noun, sample label, or expected-error text.
"""

import re
from typing import Any


_CALL_CUES = ("致电", "电话", "通话", "来电", "联系", "回电")

_CORE_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family_id": "cs_open_identity_purpose",
        "family_name": "开场身份与来意确认",
        "instruction_cues": ("致电", "电话", "通话", "来电", "联系", "你好", "我是", "请问是"),
        "graph_cues": ("开场", "身份", "来意", "目的", "确认", "本人"),
        "repair_hint": "若指令是通话任务，主图通常应包含开场、身份确认、来意说明；不要把具体业务事实放成 fact。",
    },
    {
        "family_id": "cs_convenience_and_turn_taking",
        "family_name": "确认可沟通与回应机会",
        "instruction_cues": ("方便", "可以说", "回应", "提问", "暂停", "等待", "机会", "继续"),
        "graph_cues": ("方便", "回应", "等待", "暂停", "提问", "继续", "可沟通"),
        "repair_hint": "若指令要求给对方回应机会，应在主图或软质量中体现；主图只补改变推进节奏的动作。",
    },
    {
        "family_id": "cs_main_notice",
        "family_name": "主线通知与提醒",
        "instruction_cues": ("通知", "提醒", "告知", "说明", "介绍", "同步", "更新"),
        "graph_cues": ("通知", "提醒", "告知", "说明", "介绍", "同步", "更新"),
        "repair_hint": "主线通知应拆成少量必达 atom；其中具体数值、时间、范围仍由知识表核验。",
    },
    {
        "family_id": "cs_required_question",
        "family_name": "必要询问与状态判断",
        "instruction_cues": ("询问", "确认", "核实", "了解", "是否", "有没有", "能否", "需要问"),
        "graph_cues": ("询问", "确认", "核实", "了解", "是否", "能否", "判断"),
        "repair_hint": "若指令要求先问状态或选择，应补询问节点或判断 atom，再接有限分支。",
    },
    {
        "family_id": "cs_condition_branch",
        "family_name": "条件分支与回流",
        "instruction_cues": ("如果", "若", "当", "遇到", "如", "则", "否则", "分情况", "根据"),
        "graph_cues": ("分支", "条件", "如果", "若", "回流", "根据", "则", "否则"),
        "repair_hint": "只有改变后续动作、回流、终止或抑制时才补 branch；单纯事实差异不要补成节点。",
    },
    {
        "family_id": "cs_faq_followup",
        "family_name": "用户追问与简短作答",
        "instruction_cues": ("疑问", "问题", "追问", "问到", "为什么", "怎么", "哪里", "原因", "细节"),
        "graph_cues": ("追问", "疑问", "问题", "作答", "回答", "解释", "原因", "细节"),
        "repair_hint": "用户触发的问题族应聚合为 faq，不要把每个事实值都做成主线节点。",
    },
    {
        "family_id": "cs_guidance_operation",
        "family_name": "引导操作与路径说明",
        "instruction_cues": ("引导", "操作", "入口", "路径", "页面", "按钮", "选择", "设置", "配置", "添加", "勾选", "保存"),
        "graph_cues": ("引导", "操作", "入口", "路径", "页面", "按钮", "选择", "设置", "配置", "添加", "保存"),
        "repair_hint": "若指令要求引导操作，主图补动作步骤，具体入口名或路径细节由知识表核验。",
    },
    {
        "family_id": "cs_refusal_objection",
        "family_name": "拒绝、质疑与挽回处理",
        "instruction_cues": ("拒绝", "不愿", "不想", "不同意", "质疑", "担心", "不认可", "坚持"),
        "graph_cues": ("拒绝", "不愿", "不同意", "质疑", "担心", "安抚", "挽回", "坚持"),
        "repair_hint": "若指令要求处理拒绝或质疑，应补条件分支；是否继续推进要看指令是否给出终止条件。",
    },
    {
        "family_id": "cs_busy_or_stop",
        "family_name": "忙碌、不便与终止抑制",
        "instruction_cues": ("忙", "没空", "不方便", "稍后", "挂断", "结束", "回头", "打扰"),
        "graph_cues": ("忙", "没空", "不方便", "稍后", "挂断", "结束", "终止", "抑制"),
        "repair_hint": "明确不便或结束场景应补 terminal_policies 或 terminal_after，避免继续强推主线。",
    },
    {
        "family_id": "cs_out_of_scope",
        "family_name": "越界诉求与职责边界",
        "instruction_cues": ("不能", "不要", "禁止", "不得", "不允许", "无法", "越权", "承诺", "保证", "一定", "代", "替"),
        "graph_cues": ("越界", "职责", "不能", "不允许", "无法", "承诺", "保证", "代", "替", "边界"),
        "repair_hint": "若指令包含越界或禁止边界，主图只补越界处理路径；具体违规扫描仍交给限制表。",
    },
    {
        "family_id": "cs_safety_or_risk",
        "family_name": "安全、风险与立即停止",
        "instruction_cues": ("安全", "风险", "危险", "异常", "开车", "事故", "受伤", "隐私", "敏感"),
        "graph_cues": ("安全", "风险", "危险", "异常", "停止", "稍后", "终止", "隐私", "敏感"),
        "repair_hint": "安全或敏感状态通常应形成终止、暂停或转安全话术，不应继续推进普通主线。",
    },
    {
        "family_id": "cs_summary_close",
        "family_name": "确认无疑问与自然结束",
        "instruction_cues": ("还有问题", "无问题", "结束", "感谢", "祝", "再见", "不打扰"),
        "graph_cues": ("问题", "确认", "结束", "感谢", "祝", "再见", "不打扰"),
        "repair_hint": "若指令要求完成后收尾，应补自然结束或确认无疑问；礼貌祝福不要拆成大量必达节点。",
    },
)

_KNOWLEDGE_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family_id": "fact_numeric_or_range",
        "family_name": "数值、次数、比例、区间",
        "patterns": (r"\d+(?:\.\d+)?\s*(?:秒|分钟|小时|天|次|个|%|元)", r"至少", r"不低于", r"不超过", r"以内", r"以上", r"区间"),
        "repair_hint": "若指令含数值、次数、比例或区间，应进入知识表并使用 value_check，wrong_groups 保持空数组。",
    },
    {
        "family_id": "fact_time_or_deadline",
        "family_name": "时间、生效、截止与先后",
        "patterns": (r"今天|当日|明天|昨日|当天|之前|之后|截止|生效|到期|连续|每[天日周月]",),
        "repair_hint": "若指令含时间或生效状态，应形成事实 atom；否定词要 scope-sensitive。",
    },
    {
        "family_id": "fact_path_channel_operation",
        "family_name": "入口、页面、路径、渠道、操作位置",
        "patterns": (r"入口|页面|路径|按钮|选项|设置|配置|添加|勾选|保存|渠道|位置",),
        "repair_hint": "操作位置和入口名属于事实核验，不要只放进主图动作。",
    },
    {
        "family_id": "fact_condition_result",
        "family_name": "条件结果与适用范围",
        "patterns": (r"如果|若|当|则|否则|仅|只|适用|不影响|影响|根据|分情况",),
        "repair_hint": "条件和结果方向应同组自足，selector/correct 不要拆成孤立词。",
    },
)

_CONSTRAINT_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family_id": "hard_out_of_scope_no_fabrication",
        "family_name": "超出职责范围时禁止擅自编造或越权解答",
        "patterns": (r"超出职责范围|职责范围|范围外|不属于.*职责|同事确认|回电",),
        "repair_hint": "若指令规定超出职责范围时的回应方式，应补 hard：negative 同时包含越界问题对象与擅自解答/编造/承诺处理动作，safe 复用同一对象并表达向同事确认、稍后回电或只回答当前能回答内容。",
    },
    {
        "family_id": "hard_no_unfounded_promise",
        "family_name": "禁止无依据承诺结果",
        "patterns": (r"不能|不要|禁止|不得|不允许", r"保证|承诺|一定|确保"),
        "repair_hint": "若指令含不能承诺或保证，应补 hard，negative 需同时有受限对象和违规动作，safe 需表达不能保证或以规则为准。",
    },
    {
        "family_id": "hard_no_delegate_or_override",
        "family_name": "禁止代操作或越权处理",
        "patterns": (r"代|替|帮.*操作|越权|后台|人工.*处理|直接.*处理",),
        "repair_hint": "代操作或越权属于 hard；主图只做边界回应，限制表负责扫描违规表达。",
    },
    {
        "family_id": "hard_stop_for_safety_or_sensitive",
        "family_name": "安全或敏感状态下停止推进",
        "patterns": (r"(?:安全|危险|风险|异常|开车|驾驶|事故|受伤|隐私|敏感|不方便).{0,30}(?:停止|暂停|稍后|不要继续|不能继续|挂断|结束|保护)",),
        "repair_hint": "只有原指令要求在安全、敏感或明显不便状态下停止/暂停/稍后处理时才补 hard；单纯提醒注意安全不是 hard。",
    },
    {
        "family_id": "soft_quality",
        "family_name": "软质量：自然、简洁、互动、不重复",
        "patterns": (r"礼貌|自然|简短|简洁|重复|回应|提问|暂停|等待|机会|字数|语气",),
        "repair_hint": "这些通常属于 soft，不要混入 hard negative_groups。",
    },
)


def _compact_text(value: Any) -> str:
    parts: list[str] = []

    def walk(x: Any) -> None:
        if x is None:
            return
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple, set)):
            for v in x:
                walk(v)
        else:
            s = str(x).strip()
            if s:
                parts.append(s)

    walk(value)
    return "\n".join(parts)


def _snippets(text: str, cues: tuple[str, ...] | list[str], *, limit: int = 5, radius: int = 18) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for cue in cues:
        if not cue:
            continue
        for m in re.finditer(re.escape(str(cue)), text):
            start = max(0, m.start() - radius)
            end = min(len(text), m.end() + radius)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            if snippet and snippet not in seen:
                seen.add(snippet)
                out.append(snippet)
            if len(out) >= limit:
                return out
    return out


def _regex_snippets(text: str, patterns: tuple[str, ...], *, limit: int = 5, radius: int = 18) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        try:
            matches = list(re.finditer(pattern, text))
        except re.error:
            continue
        for m in matches:
            start = max(0, m.start() - radius)
            end = min(len(text), m.end() + radius)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            if snippet and snippet not in seen:
                seen.add(snippet)
                out.append(snippet)
            if len(out) >= limit:
                return out
    return out


def _contains_any(text: str, cues: tuple[str, ...] | list[str]) -> bool:
    return any(str(cue) in text for cue in cues if str(cue))


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            continue
    return False




def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("node_id") or node.get("id") or "").strip()


def _node_kind(node: dict[str, Any]) -> str:
    return str(node.get("node_type") or node.get("type") or "").strip()


def _activation_mode(node: dict[str, Any]) -> str:
    act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    return str(act.get("mode") or "").strip()


def _trigger_hint(node: dict[str, Any]) -> str:
    act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    return str(act.get("trigger_hint") or "").strip()


def _question_families(text: str) -> list[str]:
    """Generic question-object families used only for graph repair hints."""
    patterns: tuple[tuple[str, str], ...] = (
        ("exit_or_cancel", r"取消|终止|不参加|不继续"),
        ("reward_or_benefit", r"激励|补贴|金额|收费|元|优惠|折扣|券"),
        ("quantity_or_days", r"单量|几单|多少单|\d+\s*单|\d+\s*天|连续|每天|每日"),
        ("quota_or_rank", r"资源|排序|准入|入选|申请"),
        ("path_or_entry", r"入口|路径|页面|App|APP|系统|控制台|设置|配置|勾选|保存"),
        ("visibility_or_status", r"显示|看不到|可见|开放|未开放|生效|状态"),
        ("contact_or_verify", r"外部联系|联系方式|手机号|号码|验证|添加"),
        ("scope_or_authority", r"职责|范围|权限|越权|同事确认|回电"),
    )
    out: list[str] = []
    for name, pat in patterns:
        try:
            if re.search(pat, text):
                out.append(name)
        except re.error:
            continue
    return out


def _abstract_trigger_hint(hint: str) -> bool:
    if not hint:
        return True
    abstract = ("其他问题", "用户追问", "有疑问", "有问题", "条件满足", "主线必达", "根据情况", "相关问题", "问题族")
    return any(x in hint for x in abstract) and len(hint) <= 16


def _hard_cluster(item: Any) -> str:
    if isinstance(item, dict):
        # Source quotes often include the whole constraint block and can pollute
        # duplicate clustering.  Cluster only the row's own executable fields.
        item = {k: v for k, v in item.items() if k not in {"source_quote", "instruction_evidence_snippets"}}
    blob = _compact_text(item)
    if re.search(r"禁用词|禁用表达|禁用语气|语气词|不要说|不能说|不得说|禁止说|好的|哈哈|嘿嘿|嘻嘻", blob):
        return "lexical_ban"
    if re.search(r"折扣券|优惠券|优惠|权益|福利", blob) and re.search(r"承诺|保证|确保|一定|肯定", blob):
        return "promise_benefit"
    if re.search(r"开车|驾驶|安全状态|不安全", blob) and re.search(r"继续|推进|稍后|挂断|停止", blob):
        return "safety_stop"
    if re.search(r"职责范围|范围外|超出职责|越权|同事确认|回电", blob):
        return "out_of_scope"
    if re.search(r"代操作|代为|帮.*操作|后台处理|人工修改|越权处理", blob):
        return "delegate_or_override"
    return ""


def instruction_hard_constraint_requirement(instruction: str) -> dict[str, Any]:
    """Detect whether the instruction itself requires at least one hard constraint.

    This is a build-quality signal, not a scoring rule.  It deliberately uses
    generic customer-service boundary patterns and avoids task nouns.  Plain
    business facts such as "生效状态/申请排序/页面入口/数量周期" must not force a hard table.
    """
    text = str(instruction or "")
    signals: list[dict[str, Any]] = []

    patterns: tuple[tuple[str, str], ...] = (
        ("out_of_scope_boundary", r"超出职责范围|职责范围外|范围外|不属于.*职责|向同事确认|确认后再回电|现在能回答的先回答"),
        ("explicit_forbidden_action", r"(?:不要|不能|不得|禁止|严禁|不允许|不可|避免).{0,24}(?:承诺|保证|确保|代.*操作|代.*完成|替.*操作|越权|后台|人工.*改|继续推进|透露|索要|收费|收钱|强迫|辱骂|威胁|诱导)"),
        ("no_unfounded_promise", r"(?:不要|不能|不得|禁止|不允许|不可).{0,18}(?:承诺|保证|确保|一定|肯定)"),
        ("delegate_or_override_boundary", r"(?:不要|不能|不得|禁止|不允许|不可).{0,24}(?:代办|代做|代操作|替.*完成|后台处理|人工处理|人工修改|越权处理)"),
        ("safety_stop_boundary", r"(?:安全|危险|风险|开车|驾驶|事故|受伤|隐私|敏感).{0,30}(?:停止|暂停|稍后|不要继续|不能继续|挂断|结束|保护)"),
        ("specific_banned_word_or_phrase", r"(?:禁说|不要说|不能说|不得说|禁止说|严禁说|固定回复|回复[：:])"),
    )
    for signal_id, pattern in patterns:
        try:
            for m in re.finditer(pattern, text):
                start = max(0, m.start() - 18)
                end = min(len(text), m.end() + 18)
                snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                signals.append({"signal_id": signal_id, "evidence": snippet})
                break
        except re.error:
            continue
    # De-duplicate while preserving order.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in signals:
        key = (str(item.get("signal_id")), str(item.get("evidence")))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {
        "required": bool(unique),
        "signals": unique[:8],
        "policy": "Only explicit negative-object or boundary instructions require hard constraints; ordinary business facts do not.",
    }


def build_core_supplement_hints(instruction: str, current_graph_core: dict[str, Any]) -> dict[str, Any]:
    """Return task-agnostic gap hints for the core-graph supplement pass."""
    ins = str(instruction or "")
    graph_text = _compact_text(current_graph_core)
    families: list[dict[str, Any]] = []
    call_like = _contains_any(ins, _CALL_CUES)
    for fam in _CORE_FAMILIES:
        cues = tuple(fam.get("instruction_cues") or ())
        graph_cues = tuple(fam.get("graph_cues") or cues)
        hit_instruction = _contains_any(ins, cues)
        if fam["family_id"] in {"cs_open_identity_purpose", "cs_summary_close"} and call_like:
            hit_instruction = True
        hit_graph = _contains_any(graph_text, graph_cues)
        if not hit_instruction and not hit_graph:
            continue
        families.append({
            "family_id": fam["family_id"],
            "family_name": fam["family_name"],
            "instruction_signal": bool(hit_instruction),
            "current_graph_signal": bool(hit_graph),
            "gap_type": "candidate_missing" if hit_instruction and not hit_graph else "covered_or_weak",
            "instruction_evidence_snippets": _snippets(ins, cues),
            "repair_hint": fam["repair_hint"],
        })
    nodes = current_graph_core.get("nodes") if isinstance(current_graph_core, dict) else []
    edges = current_graph_core.get("edges") if isinstance(current_graph_core, dict) else []
    relation_groups = current_graph_core.get("relation_groups") if isinstance(current_graph_core, dict) else []
    terminal = current_graph_core.get("terminal_policies") if isinstance(current_graph_core, dict) else []
    structural_gaps: list[dict[str, str]] = []
    if not isinstance(nodes, list) or not nodes:
        structural_gaps.append({"gap_type": "nodes_empty", "repair_hint": "输出完整主图节点，至少覆盖开场、主线动作、条件路径与结束。"})
    else:
        condition_nodes = [n for n in nodes if isinstance(n, dict) and str((n.get("activation") or {}).get("mode") or n.get("node_type") or "") in {"condition", "branch"}]
        if any(f.get("family_id") == "cs_condition_branch" and f.get("instruction_signal") for f in families) and not condition_nodes:
            structural_gaps.append({"gap_type": "condition_branch_missing", "repair_hint": "指令存在条件语义但当前主图没有条件节点，应补 branch 或 condition_on。"})
        terminal_text = _compact_text(terminal)
        if any(f.get("family_id") in {"cs_busy_or_stop", "cs_safety_or_risk"} and f.get("instruction_signal") for f in families) and not terminal_text:
            structural_gaps.append({"gap_type": "terminal_policy_missing", "repair_hint": "指令存在暂停、结束或安全停止语义，应补 terminal_policies 或 terminal_after。"})
    if isinstance(nodes, list) and len(nodes) > 18:
        structural_gaps.append({"gap_type": "node_count_high", "repair_hint": "补图应合并重复分支，不要继续扩写节点。"})
    if isinstance(nodes, list):
        id_to_node = {_node_id(n): n for n in nodes if isinstance(n, dict)}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = _node_kind(node)
            mode = _activation_mode(node)
            node_text = _compact_text(node)
            atom_count = len(node.get("atoms") or []) if isinstance(node.get("atoms"), list) else 0
            families_hit = _question_families(node_text)
            if kind in {"faq", "out_of_scope"} or mode == "user_triggered":
                if atom_count >= 4 or len(families_hit) >= 3:
                    structural_gaps.append({
                        "gap_type": "faq_overpacked",
                        "node_id": _node_id(node),
                        "node_name": str(node.get("name") or ""),
                        "repair_hint": "该用户触发节点疑似承载多个互不等价问题对象；补图应按问题对象拆成多个 faq/out_of_scope 节点，每个节点只回答一个用户问题。",
                    })
            if kind == "main" and atom_count >= 3 and _matches_any(ins, (r"常见问题|FAQ|问及|追问|有疑问|问题解答|如果.*问",)) and len(families_hit) >= 2:
                structural_gaps.append({
                    "gap_type": "faq_fact_maybe_in_required_main",
                    "node_id": _node_id(node),
                    "node_name": str(node.get("name") or ""),
                    "repair_hint": "该主线节点疑似混入只应在用户追问时回答的事实；补图应核对原指令，若属于常见问题则移到 faq 节点，避免正包默认必答误杀。",
                })
            if kind in {"branch", "faq", "out_of_scope", "terminal"} or mode in {"condition", "user_triggered"}:
                if _abstract_trigger_hint(_trigger_hint(node)):
                    structural_gaps.append({
                        "gap_type": "abstract_trigger_hint",
                        "node_id": _node_id(node),
                        "node_name": str(node.get("name") or ""),
                        "repair_hint": "该条件/用户触发节点的 trigger_hint 过抽象；补图应改成具体用户状态或问题对象，供第五步生成 likely_user_texts。",
                    })
                act = node.get("activation") if isinstance(node.get("activation"), dict) else {}
                has_source_text = any(isinstance(g, dict) and str(g.get("source_text") or "").strip() for g in (act.get("trigger_groups") or []))
                has_trigger_groups = any(isinstance(g, dict) and g.get("elements") for g in (act.get("trigger_groups") or []))
                if kind == "terminal" and mode in {"optional", ""} and not has_trigger_groups:
                    structural_gaps.append({
                        "gap_type": "terminal_missing_user_trigger",
                        "node_id": _node_id(node),
                        "node_name": str(node.get("name") or ""),
                        "repair_hint": "终止节点若由用户状态触发，不能 optional 空触发；必须改成 condition/user_triggered 并补多条 source_text trigger_groups。",
                    })
                if mode in {"condition", "user_triggered"} and not has_source_text:
                    structural_gaps.append({
                        "gap_type": "condition_missing_source_text",
                        "node_id": _node_id(node),
                        "node_name": str(node.get("name") or ""),
                        "repair_hint": "该条件节点缺少用户自然话 source_text；第五步应按用户可能说法生成多组 OR trigger_groups。",
                    })
            if kind == "main" and mode in {"condition", "user_triggered"}:
                structural_gaps.append({
                    "gap_type": "main_node_wrongly_user_triggered",
                    "node_id": _node_id(node),
                    "node_name": str(node.get("name") or ""),
                    "repair_hint": "主线核心告知节点不应 user_triggered；若它承载核心说明，应改 activation.mode=always。",
                })
            if kind == "main" and re.search(r"询问|确认|核实|了解|是否|有没有|能否", node_text) and "已提供" not in node_text:
                structural_gaps.append({
                    "gap_type": "info_request_should_accept_user_provided_state",
                    "node_id": _node_id(node),
                    "node_name": str(node.get("name") or ""),
                    "repair_hint": "信息获取类主线节点应写成确认或根据用户已提供信息获取目标状态，避免强制客服重复追问。",
                })
        if isinstance(relation_groups, list):
            for rg in relation_groups:
                if not isinstance(rg, dict):
                    continue
                rg_type = str(rg.get("type") or rg.get("relation") or "")
                if rg_type != "sequential" or not bool(rg.get("required")):
                    continue
                bad_nodes = []
                for nid in rg.get("nodes") or []:
                    n = id_to_node.get(str(nid))
                    if not isinstance(n, dict):
                        continue
                    if _node_kind(n) in {"branch", "faq", "out_of_scope", "terminal"} or _activation_mode(n) in {"condition", "user_triggered", "optional"}:
                        bad_nodes.append(str(nid))
                if bad_nodes:
                    structural_gaps.append({
                        "gap_type": "conditional_node_in_required_sequential",
                        "relation_group_id": str(rg.get("group_id") or rg.get("id") or ""),
                        "node_ids": ",".join(bad_nodes[:8]),
                        "repair_hint": "required sequential 只能放默认必达主线节点；这些条件节点应移出 required sequential，改用 condition_on、exclusive_branch、any_of 或 optional_parallel。",
                    })
    if isinstance(edges, list):
        id_to_node = {_node_id(n): n for n in nodes if isinstance(n, dict)} if isinstance(nodes, list) else {}
        for e in edges:
            if not isinstance(e, dict):
                continue
            src = id_to_node.get(str(e.get("source") or ""))
            tgt = id_to_node.get(str(e.get("target") or ""))
            if not src or not tgt:
                continue
            if str(e.get("type") or e.get("relation") or "") == "suppress_after" and re.search(r"忙|没空|不方便|没时间", _compact_text(src)) and _node_kind(tgt) == "main":
                structural_gaps.append({
                    "gap_type": "busy_node_wrongly_suppresses_main",
                    "edge_id": str(e.get("id") or ""),
                    "repair_hint": "用户忙但仍允许简短说明时，忙碌处理后应回流主线；不要用 suppress_after 压制核心说明。",
                })
    if not isinstance(edges, list) or not edges:
        structural_gaps.append({"gap_type": "edges_missing", "repair_hint": "补图必须给出 source/target/type/relation 完整边。"})
    if not isinstance(relation_groups, list) or not relation_groups:
        structural_gaps.append({"gap_type": "relation_groups_missing", "repair_hint": "补图应补 sequential、exclusive_branch、optional_parallel 或 all_of 关系组。"})
    return {
        "hint_source": "local_generic_dictionary_and_structure_audit",
        "dictionary_scope": "task_agnostic_customer_service_only; no business facts are injected",
        "allowed_use": "Use these hints only to identify possible missing graph actions, branches, FAQ families, terminal policies, or relation structures.",
        "forbidden_use": "Do not copy a hint as a node if the original instruction does not support it. Do not create knowledge facts or hard/soft constraints in core graph supplement.",
        "coverage_families": families[:12],
        "structural_gaps": structural_gaps[:10],
    }


def build_knowledge_supplement_hints(instruction: str, current_knowledge_table: Any) -> dict[str, Any]:
    ins = str(instruction or "")
    table_text = _compact_text(current_knowledge_table)
    families: list[dict[str, Any]] = []
    for fam in _KNOWLEDGE_FAMILIES:
        patterns = tuple(fam.get("patterns") or ())
        hit_instruction = _matches_any(ins, patterns)
        hit_table = _matches_any(table_text, patterns)
        if not hit_instruction and not hit_table:
            continue
        families.append({
            "family_id": fam["family_id"],
            "family_name": fam["family_name"],
            "instruction_signal": bool(hit_instruction),
            "current_table_signal": bool(hit_table),
            "gap_type": "candidate_missing" if hit_instruction and not hit_table else "covered_or_weak",
            "instruction_evidence_snippets": _regex_snippets(ins, patterns),
            "repair_hint": fam["repair_hint"],
        })
    return {
        "hint_source": "local_generic_fact_slot_audit",
        "dictionary_scope": "task_agnostic_fact_shapes_only; facts must still be copied from original instruction",
        "allowed_use": "Use these hints to check whether obvious fact slots are absent or structurally weak.",
        "forbidden_use": "Do not invent values. Do not add courtesy, flow actions, or boundaries into knowledge_table.",
        "coverage_families": families[:8],
    }


def build_constraint_supplement_hints(instruction: str, current_hard: Any, current_soft: Any) -> dict[str, Any]:
    ins = str(instruction or "")
    hard_text = _compact_text(current_hard)
    soft_text = _compact_text(current_soft)
    table_text = f"{hard_text}\n{soft_text}"
    hard_requirement = instruction_hard_constraint_requirement(ins)
    families: list[dict[str, Any]] = []
    for fam in _CONSTRAINT_FAMILIES:
        patterns = tuple(fam.get("patterns") or ())
        hit_instruction = _matches_any(ins, patterns)
        hit_table = _matches_any(table_text, patterns)
        if not hit_instruction and not hit_table:
            continue
        families.append({
            "family_id": fam["family_id"],
            "family_name": fam["family_name"],
            "instruction_signal": bool(hit_instruction),
            "current_table_signal": bool(hit_table),
            "gap_type": "candidate_missing" if hit_instruction and not hit_table else "covered_or_weak",
            "instruction_evidence_snippets": _regex_snippets(ins, patterns),
            "repair_hint": fam["repair_hint"],
        })
    if hard_requirement.get("required") and not any(isinstance(x, dict) for x in (current_hard or [])):
        families.insert(0, {
            "family_id": "hard_required_but_current_empty",
            "family_name": "原指令存在明确硬边界但当前 hard 为空",
            "instruction_signal": True,
            "current_table_signal": False,
            "gap_type": "must_review_empty_hard",
            "instruction_evidence_snippets": [x.get("evidence") for x in hard_requirement.get("signals", []) if isinstance(x, dict) and x.get("evidence")][:5],
            "repair_hint": "必须先从证据片段中抽出受限对象和违规动作；若只能抽出质量要求，则不要补 hard。",
        })
    duplicate_hard_hints: list[dict[str, Any]] = []
    if isinstance(current_hard, list):
        clusters: dict[str, list[str]] = {}
        for item in current_hard:
            if not isinstance(item, dict):
                continue
            cluster = _hard_cluster(item)
            if not cluster:
                continue
            clusters.setdefault(cluster, []).append(str(item.get("id") or item.get("constraint_id") or item.get("name") or ""))
        for cluster, ids in clusters.items():
            clean_ids = [x for x in ids if x]
            if len(clean_ids) > 1:
                duplicate_hard_hints.append({
                    "gap_type": "duplicate_hard_cluster",
                    "cluster": cluster,
                    "constraint_ids": clean_ids[:8],
                    "repair_hint": "这些 hard 语义重复或过度泛化；补表不要新增同类 hard，必要时用 remove_constraint_ids 删除较弱或过宽项。",
                })
    return {
        "hint_source": "local_generic_boundary_quality_audit",
        "dictionary_scope": "task_agnostic_boundary_and_quality_shapes_only",
        "allowed_use": "Use these hints to decide whether patch-only additions are needed.",
        "forbidden_use": "Do not exceed max_new_hard. Do not turn soft quality into hard semantic objects.",
        "hard_constraint_required_by_instruction": hard_requirement,
        "empty_hard_policy": "If hard_constraint_required_by_instruction.required is false, an empty hard table is allowed; do not invent hard constraints from ordinary facts.",
        "repair_required_action": "If required is true, the supplement pass must provide hard_candidate_decisions for each signal. Empty add_hard_constraint_table is valid only when every signal is rejected with a grounded reason.",
        "coverage_families": families[:10],
        "duplicate_hard_hints": duplicate_hard_hints[:8],
    }
