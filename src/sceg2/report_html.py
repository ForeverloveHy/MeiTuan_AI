from __future__ import annotations

import html
from collections import Counter, defaultdict
from statistics import mean
from typing import Any


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _short(value: Any, n: int = 180) -> str:
    s = str(value if value is not None else "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_score(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    try:
        return f"{float(value):.2f}"
    except Exception:
        return _esc(value)


def _badge(text: str) -> str:
    text = str(text or "未验收")
    cls = "ok" if text in {"本地通过", "仲裁通过", "通过"} else "warn" if text in {"待仲裁", "待确认灰区"} else "bad"
    return f"<span class='badge {cls}'>{_esc(text)}</span>"


def _score_from_record(rec: dict[str, Any], key: str) -> float:
    exp = rec.get("explanation") or {}
    scores = exp.get("score_summary") or (rec.get("evaluation") or {}).get("scores") or {}
    aliases = {
        "total": ["总分", "total"],
        "node_completion": ["节点完成分", "node_completion"],
        "relation_score": ["结构关系分", "relation_score"],
        "knowledge_score": ["知识正确分", "knowledge_score"],
        "constraint_score": ["限制合规分", "constraint_score"],
    }
    for k in aliases.get(key, [key]):
        if k in scores:
            try:
                return float(scores[k])
            except Exception:
                return 0.0
    return 0.0


def _avg(records: list[dict[str, Any]], key: str) -> float:
    return mean([_score_from_record(r, key) for r in records]) if records else 0.0


def _accept_result(rec: dict[str, Any]) -> str:
    acc = rec.get("acceptance") or {}
    exp = rec.get("explanation") or {}
    plain = exp.get("plain_summary") or {}
    accept = plain.get("样本验收") or exp.get("acceptance_summary") or acc
    return str(accept.get("验收结果") or acc.get("result") or "未验收")


def _usage_total(run_info: dict[str, Any] | None) -> dict[str, Any]:
    usage = ((run_info or {}).get("token_usage") or {})
    return usage.get("total") or {}


def _result_summary(records: list[dict[str, Any]], run_info: dict[str, Any] | None = None) -> str:
    total = len(records)
    result_counts = Counter(_accept_result(r) for r in records)
    type_counts = Counter(str(r.get("sample_type") or "未标注") for r in records)
    sent_to_llm = sum(len((r.get("llm_verifier") or {}).get("items") or []) for r in records)
    token_total = _usage_total(run_info).get("total_tokens", 0)
    cards = [
        ("样本总数", total),
        ("平均总分", f"{_avg(records, 'total'):.2f}"),
        ("平均节点分", f"{_avg(records, 'node_completion'):.2f}"),
        ("平均结构分", f"{_avg(records, 'relation_score'):.2f}"),
        ("平均知识分", f"{_avg(records, 'knowledge_score'):.2f}"),
        ("平均限制分", f"{_avg(records, 'constraint_score'):.2f}"),
        ("本地通过", result_counts.get("本地通过", 0)),
        ("仲裁通过", result_counts.get("仲裁通过", 0)),
        ("待仲裁", result_counts.get("待仲裁", 0)),
        ("不通过", result_counts.get("不通过", 0)),
        ("正向样本", type_counts.get("positive", 0) + type_counts.get("正包", 0)),
        ("负向样本", type_counts.get("negative", 0) + type_counts.get("负包", 0)),
        ("送审候选", sent_to_llm),
    ]
    if token_total:
        cards.append(("Token 总量", token_total))
    return "".join(f"<div class='summary-card'><div class='num'>{_esc(v)}</div><div>{_esc(k)}</div></div>" for k, v in cards)


def _global_breakdown(records: list[dict[str, Any]]) -> str:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        groups[(str(rec.get("domain") or "未标注"), str(rec.get("sample_type") or "未标注"))].append(rec)
    rows: list[str] = []
    for (domain, sample_type), items in sorted(groups.items()):
        rc = Counter(_accept_result(x) for x in items)
        rows.append(
            "<tr>"
            f"<td>{_esc(domain)}</td>"
            f"<td>{_esc(sample_type)}</td>"
            f"<td>{len(items)}</td>"
            f"<td>{rc.get('本地通过', 0)}</td>"
            f"<td>{rc.get('仲裁通过', 0)}</td>"
            f"<td>{rc.get('待仲裁', 0)}</td>"
            f"<td>{rc.get('不通过', 0)}</td>"
            f"<td>{_avg(items, 'total'):.2f}</td>"
            f"<td>{_avg(items, 'node_completion'):.2f}</td>"
            f"<td>{_avg(items, 'relation_score'):.2f}</td>"
            f"<td>{_avg(items, 'knowledge_score'):.2f}</td>"
            f"<td>{_avg(items, 'constraint_score'):.2f}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='12'>暂无样本。</td></tr>"


def _score_gap_panel(records: list[dict[str, Any]]) -> str:
    by_domain: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        domain = str(rec.get("domain") or "未标注")
        typ = str(rec.get("sample_type") or "未标注").lower()
        by_domain[domain][typ].append(rec)
    rows: list[str] = []
    notes: list[str] = []
    for domain, group in sorted(by_domain.items()):
        pos = group.get("positive", []) + group.get("正包", [])
        neg = group.get("negative", []) + group.get("负包", [])
        if not pos or not neg:
            continue
        pos_avg = _avg(pos, "total")
        neg_avg = _avg(neg, "total")
        gap = pos_avg - neg_avg
        verdict = "区分明显" if gap >= 30 else "偏弱，需要检查评分封顶或正负包纯度" if gap >= 18 else "区分不开，优先排查评分/报告/样本"
        rows.append(
            "<tr>"
            f"<td>{_esc(domain)}</td><td>{pos_avg:.2f}</td><td>{neg_avg:.2f}</td><td>{gap:.2f}</td><td>{_esc(verdict)}</td>"
            "</tr>"
        )
        if gap < 30:
            notes.append(f"{domain} 正负包平均总分差距 {gap:.2f}，建议看负包是否只验收通过但未同步强扣，或正包是否存在分支/终止场景被完整主线误扣。")
    if not rows:
        return "<p class='muted'>当前报告缺少成对正负包，无法计算区分度。</p>"
    note_html = "" if not notes else "<ul class='warnings'>" + "".join(f"<li>{_esc(x)}</li>" for x in notes) + "</ul>"
    return (
        "<table><thead><tr><th>Domain</th><th>正包平均总分</th><th>负包平均总分</th><th>差距</th><th>判断</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{note_html}"
    )




def _family_cn(family: Any) -> str:
    raw = str(family or "").lower()
    if raw in {"flow_missing", "process_missing", "流程缺失", "requirement"}:
        return "流程缺失"
    if raw in {"knowledge_violation", "knowledge_error", "fact_wrong", "faq_wrong", "知识错误"}:
        return "知识错误"
    if raw in {"constraint_violation", "boundary_violation", "限制违规"}:
        return "限制违规"
    if raw in {"context_violation"}:
        return "上下文错误"
    if raw in {"semantic_or_context", "semantic", "open_set"}:
        return "语义灰区"
    return str(family or "未标注")


def _matched_expected_items(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return list(((rec.get("acceptance") or {}).get("matched_expected") or []))


def _expected_issue_summary(rec: dict[str, Any]) -> str:
    items = _matched_expected_items(rec) or list(((rec.get("acceptance") or {}).get("missing_expected") or [])) or list(((rec.get("acceptance") or {}).get("oracle_expected") or []))
    if not items:
        return ""
    families = [_family_cn(x.get("error_family") or x.get("type") or x.get("target_kind")) for x in items]
    family_text = "、".join(dict.fromkeys(families))
    spans = [str(x.get("evidence_span") or x.get("wrong_statement") or "").strip() for x in items]
    spans = [x for x in spans if x]
    if spans:
        return f"{family_text}：{spans[0]}"
    target = items[0].get("requirement_id") or items[0].get("knowledge_id") or items[0].get("constraint_id") or items[0].get("node_id") or ""
    return f"{family_text}：{target}" if target else family_text




def _has_scenario_floor(rec: dict[str, Any]) -> bool:
    for cap in (rec.get("evaluation") or {}).get("caps") or []:
        if str(cap.get("score_adjustment") or "") == "floor" or "场景型正包" in str(cap.get("reason") or ""):
            return True
    return False


def _display_headline(rec: dict[str, Any]) -> str:
    result = _accept_result(rec)
    typ = str(rec.get("sample_type") or "").lower()
    if typ == "positive" and result in {"本地通过", "仲裁通过", "通过"} and _has_scenario_floor(rec):
        return "正包通过：该样本是分支/终止场景，按 coverage_targets 验收目标处理，不强制完整主线。"
    if typ == "negative" and _matched_expected_items(rec):
        return "负包通过：" + _expected_issue_summary(rec)
    exp = rec.get("explanation") or {}
    plain = exp.get("plain_summary") or {}
    return str(plain.get("一句话结论") or exp.get("headline") or "")

def _span_turns(eval_: dict[str, Any], text: Any) -> str:
    raw = "".join(str(text or "").split()).lower()
    if not raw:
        return ""
    hits: list[str] = []
    for unit in eval_.get("evidence_units") or []:
        utext = str(unit.get("text") or "")
        if raw in "".join(utext.split()).lower():
            idx = unit.get("turn_index")
            speaker = "客服" if unit.get("speaker") == "assistant" else "用户" if unit.get("speaker") == "user" else str(unit.get("speaker") or "")
            turn = f"第 {int(idx) + 1} 句" if idx is not None else "未定位句号"
            hits.append(f"{turn}（{speaker}）：{utext}")
    return "<br>".join(_esc(x) for x in hits[:3])


def _negative_issue_panel(records: list[dict[str, Any]]) -> str:
    neg = [r for r in records if str(r.get("sample_type") or "").lower() == "negative"]
    if not neg:
        return "<p class='muted'>暂无负向样本。</p>"
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in neg:
        matched = _matched_expected_items(rec)
        if not matched:
            groups["未命中预设"].append(rec)
            continue
        families = sorted({_family_cn(x.get("error_family") or x.get("type") or x.get("target_kind")) for x in matched})
        groups[" + ".join(families)].append(rec)
    rows: list[str] = []
    for family, items in sorted(groups.items(), key=lambda kv: (kv[0], len(kv[1]))):
        rows.append(
            "<tr>"
            f"<td>{_esc(family)}</td>"
            f"<td>{len(items)}</td>"
            f"<td>{_avg(items, 'total'):.2f}</td>"
            f"<td>{_avg(items, 'node_completion'):.2f}</td>"
            f"<td>{_avg(items, 'knowledge_score'):.2f}</td>"
            f"<td>{_avg(items, 'constraint_score'):.2f}</td>"
            f"<td>{_esc(_short(_expected_issue_summary(items[0]), 160))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>负包错误类型</th><th>数量</th><th>平均总分</th><th>平均节点分</th><th>平均知识分</th><th>平均限制分</th><th>示例追因</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )

def _token_rows(run_info: dict[str, Any] | None) -> str:
    usage = ((run_info or {}).get("token_usage") or {})
    if not usage:
        return "<tr><td colspan='5'>暂无 Token 统计。未开启模型调用或旧结果包未记录用量。</td></tr>"
    labels = {"total": "合计", "build_graph": "状态图生成", "llm_verifier": "二级判断"}
    rows: list[str] = []
    for key in ["total", "build_graph", "llm_verifier"]:
        block = usage.get(key) or {}
        total = block.get("total") if key != "total" else block
        total = total or {}
        if key != "total" and not total:
            continue
        rows.append(
            "<tr>"
            f"<td>{labels.get(key, key)}</td>"
            f"<td>{_esc(total.get('calls', 0))}</td>"
            f"<td>{_esc(total.get('prompt_tokens', 0))}</td>"
            f"<td>{_esc(total.get('completion_tokens', 0))}</td>"
            f"<td>{_esc(total.get('total_tokens', 0))}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='5'>暂无 Token 统计。</td></tr>"


def _load_info_html(run_info: dict[str, Any] | None) -> str:
    info = ((run_info or {}).get("filter_info") or {})
    if not info:
        return "<p class='muted'>旧结果包没有记录数据装载筛选信息。</p>"
    rows = [
        ("装载方法", info.get("method", "")),
        ("发现的 domain", "、".join(str(x) for x in info.get("domains_seen", []))),
        ("自动选择", info.get("selected_domain") or "未自动选择"),
        ("跳过数量", info.get("skipped", 0)),
        ("原始数量", info.get("count_before_pack_filter", "")),
        ("包筛选后", info.get("count_after_pack_filter", "")),
        ("domain 筛选后", info.get("count_after_domain_filter", "")),
        ("图-样本绑定", info.get("graph_dialogue_binding", "")),
        ("绑定后 domain", info.get("graph_domain_after_binding", "")),
    ]
    score_text = info.get("scores") or {}
    if score_text:
        rows.append(("兼容度", "；".join(f"{k}={v}" for k, v in score_text.items())))
    html_rows = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
    note = info.get("note") or ""
    note_html = f"<p class='muted'>{_esc(note)}</p>" if note else ""
    return f"<table><tbody>{html_rows}</tbody></table>{note_html}"


def _global_warnings(records: list[dict[str, Any]], run_info: dict[str, Any] | None = None) -> str:
    warnings: list[str] = []
    filter_info = ((run_info or {}).get("filter_info") or {})
    if filter_info.get("skipped"):
        warnings.append(f"本次自动跳过 {filter_info.get('skipped')} 条与当前状态图不匹配的对话；实际评估 {filter_info.get('count_after_domain_filter')} 条。")
    graph_ids = Counter(str((r.get("evaluation") or {}).get("graph_id") or "未知图") for r in records)
    domains = Counter(str(r.get("domain") or "未标注") for r in records)
    if len(graph_ids) == 1 and len([d for d in domains if d != "未标注"]) > 1:
        warnings.append("一次报告里只有一个状态图，但样本来自多个 domain。若这些 domain 代表不同任务，就属于同一张图评估多套任务，节点分接近 0 的样本多半是假失败。")
    negative_records = [r for r in records if str(r.get("sample_type") or "").lower() == "negative"]
    matched_expected = sum(len((r.get("acceptance") or {}).get("matched_expected") or []) for r in negative_records)
    missing_expected = sum(len((r.get("acceptance") or {}).get("missing_expected") or []) for r in negative_records)
    oracle_expected = sum(len((r.get("acceptance") or {}).get("oracle_expected") or []) for r in negative_records)
    if negative_records and matched_expected == 0 and (missing_expected or oracle_expected):
        warnings.append(
            "负向样本没有任何预设问题被本地命中，但存在大量未命中/待仲裁预设项。通常说明 LongCat 新生成的节点/知识/限制编号与正负包绑定编号不一致，或状态图证据组过宽导致预设错误无法定位。"
        )

    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_domain[str(rec.get("domain") or "未标注")].append(rec)
    for domain, items in by_domain.items():
        positives = [x for x in items if str(x.get("sample_type") or "").lower() == "positive"]
        if positives:
            pass_count = sum(_accept_result(x) in {"本地通过", "仲裁通过", "通过"} for x in positives)
            avg_node = _avg(positives, "node_completion")
            if pass_count == 0 and avg_node < 10:
                warnings.append(f"{domain} 正向样本全部未通过且平均节点分 {avg_node:.2f}，优先检查状态图与对话任务是否匹配。")
    if not warnings:
        return "<p class='muted'>未发现明显的数据装载或跨任务混用预警。</p>"
    return "<ul class='warnings'>" + "".join(f"<li>{_esc(x)}</li>" for x in warnings) + "</ul>"


def _trace_html(value: Any, depth: int = 0) -> str:
    if value in (None, "", {}, []):
        return ""
    if depth > 3:
        return _esc(_short(str(value), 500))
    if isinstance(value, dict):
        rows: list[str] = []
        for k, v in value.items():
            if v in (None, "", [], {}):
                continue
            rows.append(
                "<div class='trace-row'>"
                f"<b>{_esc(k)}</b>：{_trace_html(v, depth + 1)}"
                "</div>"
            )
        return "<div class='trace-box'>" + "".join(rows) + "</div>" if rows else ""
    if isinstance(value, list):
        if not value:
            return ""
        lis = "".join(f"<li>{_trace_html(x, depth + 1)}</li>" for x in value[:8])
        if len(value) > 8:
            lis += f"<li class='muted'>还有 {len(value) - 8} 项</li>"
        return "<ul class='trace-list'>" + lis + "</ul>"
    return _esc(_short(str(value), 800))


def _loss_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<li>暂无明显失分点。</li>"
    rows: list[str] = []
    for x in items[:5]:
        typ = str(x.get("类型", ""))
        cls = "neutral" if typ in {"无明显失分"} else "warn" if typ == "待确认灰区" else "bad"
        tech_raw = x.get("技术追踪", "")
        tech = _trace_html(tech_raw)
        tech_html = f"<details><summary>技术追踪：定位到节点/证据组/原话轮次</summary>{tech}</details>" if tech else ""
        type_html = _badge(typ) if typ in {"待确认灰区"} else f"<span class='tag {cls}'>{_esc(typ)}</span>"
        rows.append(
            "<li class='loss-item'>"
            f"<div>{type_html}</div>"
            f"<p><b>{_esc(_short(x.get('直白说明',''), 260))}</b></p>"
            f"<p><span class='label'>相关原话：</span>{_esc(_short(x.get('证据原话',''), 260))}</p>"
            f"<p><span class='label'>为什么失分：</span>{_esc(_short(x.get('为什么失分',''), 260))}</p>"
            f"<p><span class='label'>建议：</span>{_esc(_short(x.get('建议',''), 260))}</p>"
            f"{tech_html}</li>"
        )
    if len(items) > 5:
        rows.append(f"<li class='muted'>还有 {len(items)-5} 条失分说明已省略；完整技术细节请看该样本 JSON。</li>")
    return "".join(rows)


def _hit_text(hit: dict[str, Any]) -> str:
    turn = hit.get("turn_index")
    text = hit.get("text") or hit.get("原话") or hit.get("source") or ""
    prefix = f"第 {int(turn) + 1} 句：" if turn is not None else ""
    return prefix + str(text)


def _node_counts(node: dict[str, Any]) -> tuple[int, int]:
    total = 0
    matched = 0
    for req in node.get("requirements") or []:
        for group in req.get("groups") or []:
            if group.get("required", True):
                total += 1
                if group.get("matched"):
                    matched += 1
    return matched, total


def _node_overview(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "<p class='muted'>暂无节点概览。</p>"
    rows = []
    for n in nodes:
        matched, total = _node_counts(n)
        rows.append(
            "<tr>"
            f"<td>{_esc(n.get('node_id'))}</td>"
            f"<td>{_esc(n.get('name'))}</td>"
            f"<td>{_esc(n.get('status'))}</td>"
            f"<td>{_fmt_score(n.get('score'))}</td>"
            f"<td>{matched}/{total}</td>"
            f"<td>{_esc(n.get('first_hit_turn') if n.get('first_hit_turn') is not None else '无')}</td>"
            "</tr>"
        )
    return "<table><thead><tr><th>节点编号</th><th>节点</th><th>状态</th><th>节点分</th><th>证据组命中</th><th>首次命中轮次</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _node_trace(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "<p class='muted'>暂无节点追踪。</p>"
    blocks: list[str] = []
    for node in nodes:
        status = str(node.get("status") or "")
        status_cls = "ok" if status == "已完成" else "neutral" if status == "不适用" else "bad"
        first_hit = node.get("first_hit_turn")
        matched, total = _node_counts(node)
        summary = (
            f"<span class='tag {status_cls}'>{_esc(status)}</span> "
            f"{_esc(node.get('name'))}｜节点分 {_fmt_score(node.get('score'))}"
            f"｜证据组 {matched}/{total}｜首次命中：{_esc(first_hit if first_hit is not None else '无')}"
        )
        req_rows: list[str] = []
        for req in node.get("requirements") or []:
            req_rows.append(
                "<tr class='req-row'>"
                f"<td>{_esc(req.get('requirement_id'))}</td>"
                f"<td>{_esc(_short(req.get('text'), 220))}</td>"
                f"<td>{'是' if req.get('matched') else '否'}</td>"
                f"<td>{_fmt_score(req.get('score'))}</td>"
                "</tr>"
            )
            for group in req.get("groups") or []:
                hits = group.get("hits") or []
                if hits:
                    evidence = "<br>".join(_esc(_short(_hit_text(h), 260)) for h in hits[:4])
                    if len(hits) > 4:
                        evidence += f"<br><span class='muted'>还有 {len(hits)-4} 条命中</span>"
                else:
                    evidence = "<span class='muted'>未找到可追溯原话。</span>"
                expected = group.get("expected_patterns") or []
                expected_html = ""
                if expected:
                    expected_html = "<details><summary>期望证据表达</summary>" + _trace_html(expected[:6]) + "</details>"
                req_rows.append(
                    "<tr class='group-row'>"
                    f"<td>└ {_esc(group.get('group_id'))}</td>"
                    f"<td>{_esc(_short(group.get('description'), 260))}{expected_html}</td>"
                    f"<td>{'是' if group.get('matched') else '否'}</td>"
                    f"<td>{_fmt_score(group.get('score'))}<br>{evidence}</td>"
                    "</tr>"
                )
        if not req_rows:
            req_rows.append("<tr><td colspan='4'>暂无履约小任务明细。</td></tr>")
        alias_text = "；".join(str(x) for x in (node.get("aliases") or [])[:8])
        tech = (
            f"节点编号：{_esc(node.get('node_id'))}；active={_esc(node.get('active'))}；"
            f"不适用原因：{_esc(node.get('inactive_reason'))}<br>别名/锚点：{_esc(alias_text)}"
        )
        blocks.append(
            f"<details class='node-trace'><summary>{summary}</summary>"
            f"<p class='techline'>{tech}</p>"
            "<table><thead><tr><th>编号</th><th>要求/证据组</th><th>命中</th><th>分数与原话</th></tr></thead>"
            f"<tbody>{''.join(req_rows)}</tbody></table></details>"
        )
    return "".join(blocks)


def _check_rows(checks: list[dict[str, Any]], kind: str, rec: dict[str, Any] | None = None) -> str:
    # 主报告只展示明确扣分事件。灰区/证据不足进入“证据账本”和“仲裁队列”，
    # 避免知识表、限制表被大量“证据不足”刷屏。
    visible = [x for x in checks if x.get("结论") in {"冲突", "违规"}]
    rows = []
    # 如果 formal 负包的预设知识/限制错误已被验收层确认，即使 LongCat schema 没把它归到
    # knowledge_events / constraint_events，也要在对应核验表中清楚展示出来。
    if rec:
        eval_ = rec.get("evaluation") or {}
        for item in _matched_expected_items(rec):
            family = _family_cn(item.get("error_family") or item.get("type") or item.get("target_kind"))
            if kind == "知识核验" and family != "知识错误":
                continue
            if kind == "限制核验" and family != "限制违规":
                continue
            evidence = item.get("evidence_span") or item.get("wrong_statement") or item.get("violation_statement") or ""
            item_id = item.get("knowledge_id") if kind == "知识核验" else item.get("constraint_id")
            rows.append(
                "<tr>"
                f"<td>{'冲突' if kind == '知识核验' else '违规'}</td>"
                f"<td>{_esc(item_id or item.get('node_id') or '预设错误')}</td>"
                f"<td>{_esc(_short(evidence or '流程缺失类错误无单句错话', 260))}<br><span class='muted'>{_span_turns(eval_, evidence)}</span></td>"
                f"<td>负包预设错误已命中，分数已按该类型强扣。<details><summary>技术追踪</summary><p>{_esc(_expected_issue_summary(rec))}</p></details></td>"
                "</tr>"
            )
    for x in visible[:10]:
        name_key = "知识项" if kind == "知识核验" else "限制项"
        id_key = "知识编号" if kind == "知识核验" else "限制编号"
        rows.append(
            "<tr>"
            f"<td>{_esc(x.get('结论'))}</td>"
            f"<td>{_esc(x.get(name_key))}</td>"
            f"<td>{_esc(_short(x.get('证据原话'), 260))}<br><span class='muted'>{_esc('第' + str(int(x.get('轮次')) + 1) + '句' if x.get('轮次') is not None else '')}</span></td>"
            f"<td>{_esc(_short(x.get('说明'), 260))}<details><summary>技术追踪</summary><p>{_esc(id_key)}={_esc(x.get(id_key))}；轮次={_esc(x.get('轮次'))}</p></details></td>"
            "</tr>"
        )
    if not rows:
        return f"<tr><td colspan='4'>暂无明确扣分的{kind}事件；支持/灰区明细请看证据账本。</td></tr>"
    return "".join(rows)


def _arbitration_rows(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<tr><td colspan='4'>暂无待仲裁候选。</td></tr>"
    rows = []
    for x in items[:10]:
        tech = x.get("技术追踪") or {}
        tech_text = "；".join(f"{k}={v}" for k, v in tech.items() if v)
        rows.append(
            "<tr>"
            f"<td>{_esc(x.get('候选类型'))}</td>"
            f"<td>{_esc(x.get('绑定位置'))}</td>"
            f"<td>{_esc(_short(x.get('局部问题'), 260))}</td>"
            f"<td>{_esc(_short(x.get('证据'), 260))}<details><summary>技术追踪</summary><p>{_esc(tech_text)}</p></details></td>"
            "</tr>"
        )
    return "".join(rows)


def _llm_rows(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<tr><td colspan='5'>本次未开启大模型二级判断，或没有可送审候选。</td></tr>"
    rows = []
    label = {"confirmed_issue": "问题成立", "no_issue": "问题不成立", "uncertain": "证据不足"}
    for x in items[:10]:
        rows.append(
            "<tr>"
            f"<td>{_esc(label.get(str(x.get('verdict')), x.get('verdict')))}</td>"
            f"<td>{_esc(x.get('confidence'))}</td>"
            f"<td>{_esc(x.get('candidate_kind'))}</td>"
            f"<td>{_esc(_short(x.get('question'), 240))}</td>"
            f"<td>{_esc(_short(x.get('reason'), 260))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _relation_rows(items: list[dict[str, Any]]) -> str:
    focus = [x for x in items if float(x.get("扣分") or 0) > 0]
    if not focus:
        return "<tr><td colspan='4'>暂无结构扣分事件。</td></tr>"
    rows = []
    for x in focus[:10]:
        rel = f"{x.get('起点','')} → {x.get('终点','')}"
        rows.append(
            "<tr>"
            f"<td>{_esc(x.get('状态'))}</td>"
            f"<td>{_esc(_short(rel, 220))}</td>"
            f"<td>{_esc(_short(x.get('说明'), 220))}</td>"
            f"<td>{_fmt_score(x.get('扣分'))}<details><summary>技术追踪</summary>{_trace_html(x.get('技术追踪'))}</details></td>"
            "</tr>"
        )
    return "".join(rows)


def _context_rows(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<tr><td colspan='3'>暂无上下文终止或转场事件。</td></tr>"
    rows = []
    for x in items[:8]:
        rows.append(
            "<tr>"
            f"<td>{_esc(x.get('状态'))}</td>"
            f"<td>{_esc(_short(x.get('说明'), 260))}</td>"
            f"<td><details><summary>技术追踪</summary><p>策略={_esc(x.get('策略编号'))}；触发轮次={_esc(x.get('触发轮次'))}；处理轮次={_esc(x.get('处理轮次'))}</p></details></td>"
            "</tr>"
        )
    return "".join(rows)


def _schema_linter_html(run_info: dict[str, Any] | None, *, detail: bool = False) -> str:
    lint = ((run_info or {}).get("schema_linter") or {})
    if not lint:
        return "<p class='muted'>暂无 schema 质检记录。</p>"
    counts = lint.get("counts") or {}
    issue_count = int(lint.get("issue_count") or 0)
    cards = [f"<span class='tag neutral'>{_esc(k)}：{_esc(v)}</span>" for k, v in counts.items()]
    head = f"<p>发现 {issue_count} 个 schema 质检项。{' '.join(cards) if cards else ''}</p>"
    if not detail:
        return head + "<p class='muted'>详细报告中可查看自动修复和预警明细。</p>"
    rows = []
    for x in (lint.get("issues") or [])[:80]:
        rows.append(
            "<tr>"
            f"<td>{_esc(x.get('level'))}</td>"
            f"<td>{_esc(x.get('type'))}</td>"
            f"<td>{_esc(x.get('path'))}</td>"
            f"<td>{_esc(_short(x.get('message'), 260))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4'>没有需要展示的 schema 质检项。</td></tr>")
    return head + "<details open><summary>schema 质检明细</summary><table><thead><tr><th>级别</th><th>类型</th><th>位置</th><th>说明</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></details>"


def _ledger_rows(rows: list[dict[str, Any]], cols: list[str], limit: int = 120) -> str:
    if not rows:
        return f"<tr><td colspan='{len(cols)}'>暂无记录。</td></tr>"
    html_rows = []
    for item in rows[:limit]:
        html_rows.append("<tr>" + "".join(f"<td>{_trace_html(item.get(c))}</td>" for c in cols) + "</tr>")
    if len(rows) > limit:
        html_rows.append(f"<tr><td colspan='{len(cols)}' class='muted'>还有 {len(rows) - limit} 条在 JSON 中。</td></tr>")
    return "".join(html_rows)


def _evidence_ledger_html(ledger: dict[str, Any]) -> str:
    if not ledger:
        return "<p class='muted'>暂无证据账本。</p>"
    node_cols = ["节点名称", "小任务文本", "证据组编号", "判定", "判定原因", "分数", "期望证据", "命中原话", "未命中时附近原话"]
    check_cols = ["名称", "结论", "证据原话", "判定原因", "说明"]
    relation_cols = ["关系", "起点", "终点", "状态", "扣分", "说明"]
    transcript = ledger.get("对话原文") or []
    transcript_html = ""
    if transcript:
        transcript_html = "<details open><summary>对话原文：带句号编号，可直接回查</summary><ol class='transcript'>" + "".join(f"<li>{_esc(x)}</li>" for x in transcript[:160]) + "</ol></details>"
    return (
        f"<p class='muted'>{_esc(ledger.get('说明') or '')}</p>"
        f"{transcript_html}"
        "<details open><summary>节点履约账本</summary><table><thead><tr>"
        + "".join(f"<th>{_esc(c)}</th>" for c in node_cols)
        + "</tr></thead><tbody>" + _ledger_rows(ledger.get("节点履约") or [], node_cols) + "</tbody></table></details>"
        "<details><summary>知识核验账本</summary><table><thead><tr>"
        + "".join(f"<th>{_esc(c)}</th>" for c in check_cols)
        + "</tr></thead><tbody>" + _ledger_rows(ledger.get("知识核验") or [], check_cols) + "</tbody></table></details>"
        "<details><summary>限制核验账本</summary><table><thead><tr>"
        + "".join(f"<th>{_esc(c)}</th>" for c in check_cols)
        + "</tr></thead><tbody>" + _ledger_rows(ledger.get("限制核验") or [], check_cols) + "</tbody></table></details>"
        "<details><summary>结构关系账本</summary><table><thead><tr>"
        + "".join(f"<th>{_esc(c)}</th>" for c in relation_cols)
        + "</tr></thead><tbody>" + _ledger_rows(ledger.get("结构关系") or [], relation_cols) + "</tbody></table></details>"
    )


def _case_card(rec: dict[str, Any]) -> str:
    eval_ = rec["evaluation"]
    acc = rec.get("acceptance", {})
    exp = rec.get("explanation", {})
    plain = exp.get("plain_summary") or {}
    result = _accept_result(rec)
    losses = exp.get("losses", [])
    knowledge_rows = _check_rows((exp.get("knowledge_summary") or {}).get("核验明细", []), "知识核验", rec)
    constraint_rows = _check_rows((exp.get("constraint_summary") or {}).get("核验明细", []), "限制核验", rec)
    arb_rows = _arbitration_rows(exp.get("arbitration_summary", []))
    llm_rows = _llm_rows((rec.get("llm_verifier") or {}).get("items") or exp.get("llm_verifier_summary", []))
    rel_rows = _relation_rows((exp.get("structure_summary") or {}).get("事件", []))
    ctx_rows = _context_rows((exp.get("context_summary") or {}).get("事件", []))
    node_overview = _node_overview(eval_.get("node_results", []))
    node_trace = _node_trace(eval_.get("node_results", []))
    reasons = acc.get("reasons") or (exp.get("acceptance_summary") or {}).get("说明") or []
    top_issue = _display_headline(rec) or "暂无一句话结论"
    total = _score_from_record(rec, "total")
    node_score = _score_from_record(rec, "node_completion")
    sample_type = rec.get("sample_type") or "未标注"
    domain = rec.get("domain") or "未标注"
    summary = (
        f"{_badge(result)} "
        f"<b>{_esc(eval_.get('dialogue_id'))}</b> "
        f"<span class='muted'>｜{_esc(domain)} / {_esc(sample_type)}｜总分 {total:.2f}｜节点 {node_score:.2f}｜{_esc(_short(top_issue, 90))}</span>"
    )
    return (
        "<details class='case-card'>"
        f"<summary>{summary}</summary>"
        "<div class='case-inner'>"
        "<div class='score-grid small'>"
        f"<div><b>{total:.2f}</b><span>总分</span></div>"
        f"<div><b>{node_score:.2f}</b><span>节点完成</span></div>"
        f"<div><b>{_score_from_record(rec, 'relation_score'):.2f}</b><span>结构关系</span></div>"
        f"<div><b>{_score_from_record(rec, 'knowledge_score'):.2f}</b><span>知识正确</span></div>"
        f"<div><b>{_score_from_record(rec, 'constraint_score'):.2f}</b><span>限制合规</span></div>"
        "</div>"
        "<details open><summary>结论、原因和建议</summary>"
        f"<p><b>一句话结论：</b>{_esc(_display_headline(rec) or plain.get('一句话结论',''))}</p>"
        f"<p><b>关键原话：</b>{_esc(_short(plain.get('关键原话',''), 360))}</p>"
        f"<p><b>原因解释：</b>{_esc(_short(plain.get('原因解释',''), 360))}</p>"
        f"<p><b>建议处理：</b>{_esc(_short(plain.get('建议处理',''), 360))}</p>"
        f"<p><b>验收说明：</b>{_esc('；'.join(str(x) for x in reasons))}</p>"
        "</details>"
        f"<details><summary>失分与待确认事项</summary><ul>{_loss_items(losses)}</ul></details>"
        f"<details><summary>证据账本：期望证据 → 实际原话 → 判定</summary>{_evidence_ledger_html(exp.get('evidence_ledger') or {})}</details>"
        f"<details><summary>节点概览</summary>{node_overview}</details>"
        f"<details><summary>节点追踪：节点 → 小任务 → 证据组 → 原话轮次</summary>{node_trace}</details>"
        f"<details><summary>知识核验</summary><table><thead><tr><th>结论</th><th>知识项</th><th>证据原话</th><th>说明</th></tr></thead><tbody>{knowledge_rows}</tbody></table></details>"
        f"<details><summary>限制核验</summary><table><thead><tr><th>结论</th><th>限制项</th><th>证据原话</th><th>说明</th></tr></thead><tbody>{constraint_rows}</tbody></table></details>"
        f"<details><summary>仲裁队列</summary><table><thead><tr><th>类型</th><th>绑定位置</th><th>局部问题</th><th>证据</th></tr></thead><tbody>{arb_rows}</tbody></table></details>"
        f"<details><summary>大模型二级判断</summary><table><thead><tr><th>结论</th><th>置信度</th><th>类型</th><th>问题</th><th>原因</th></tr></thead><tbody>{llm_rows}</tbody></table></details>"
        f"<details><summary>状态图结构关系</summary><table><thead><tr><th>状态</th><th>关系</th><th>说明</th><th>扣分</th></tr></thead><tbody>{rel_rows}</tbody></table></details>"
        f"<details><summary>上下文触发与终止/转场</summary><table><thead><tr><th>状态</th><th>说明</th><th>追踪</th></tr></thead><tbody>{ctx_rows}</tbody></table></details>"
        "</div></details>"
    )


def _case_summary_rows(records: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for rec in records:
        eval_ = rec.get("evaluation") or {}
        exp = rec.get("explanation") or {}
        plain = (exp.get("plain_summary") or {})
        href = rec.get("_detail_href") or rec.get("detail_href") or ""
        link = f"<a href='{_esc(href)}'>查看详情</a>" if href else "<span class='muted'>未生成详情页</span>"
        rows.append(
            "<tr>"
            f"<td>{_esc(eval_.get('dialogue_id'))}</td>"
            f"<td>{_esc(rec.get('domain') or '未标注')}</td>"
            f"<td>{_esc(rec.get('sample_type') or '未标注')}</td>"
            f"<td>{_badge(_accept_result(rec))}</td>"
            f"<td>{_score_from_record(rec, 'total'):.2f}</td>"
            f"<td>{_score_from_record(rec, 'node_completion'):.2f}</td>"
            f"<td>{_score_from_record(rec, 'knowledge_score'):.2f}</td>"
            f"<td>{_score_from_record(rec, 'constraint_score'):.2f}</td>"
            f"<td>{_esc(_short(_expected_issue_summary(rec), 160))}</td>"
            f"<td>{_esc(_short(_display_headline(rec), 160))}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='11'>暂无样本。</td></tr>"


_BASE_CASE_STYLE = """
body{font-family:Arial,'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:24px;color:#222}.top{max-width:1280px;margin:auto}.panel,.case-card{background:white;border-radius:16px;padding:18px;margin:0 auto 16px;box-shadow:0 2px 14px #00000012;max-width:1280px}.case-card{padding:0}.case-card>summary{list-style:none;cursor:pointer;padding:16px 18px;line-height:1.7}.case-card>summary::-webkit-details-marker{display:none}.case-inner{border-top:1px solid #edf0f3;padding:14px 18px 18px}.score-grid{display:grid;grid-template-columns:repeat(5,minmax(90px,1fr));gap:10px;margin:12px 0}.score-grid div{background:#f8f8f8;border-radius:12px;padding:10px;text-align:center}.score-grid.small div{padding:8px}.score-grid b{display:block;font-size:22px}.score-grid span{font-size:12px;color:#666}.badge{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700}.badge.ok{background:#e8f7ed;color:#16833a}.badge.warn{background:#fff3d8;color:#956300}.badge.bad{background:#ffe8e8;color:#ad2c2c}.tag{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px}.tag.bad{background:#ffe8e8;color:#ad2c2c}.tag.warn{background:#fff3d8;color:#956300}.tag.ok{background:#e8f7ed;color:#16833a}.tag.neutral{background:#eef2f6;color:#475569}.label{color:#666;font-weight:700}.muted{color:#64748b}li.loss-item{margin:10px 0;padding:10px 12px;border:1px solid #eee;border-radius:12px;line-height:1.6}details{margin:10px 0}summary{cursor:pointer;font-weight:700;color:#334155}.node-trace{background:#fbfdff;border:1px solid #e5edf7;border-radius:12px;padding:8px 10px}.node-trace>summary{line-height:1.8}.techline{font-size:12px;color:#64748b;background:#f8fafc;border-radius:8px;padding:8px 10px}.trace-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:8px 10px;line-height:1.7}.trace-row{margin:4px 0}.trace-list{margin:4px 0 4px 18px;padding:0}.trace-list li{margin:3px 0}ol.transcript{line-height:1.8;background:#fbfdff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 10px 10px 34px}table{border-collapse:collapse;width:100%;margin:10px 0 16px;font-size:13px}td,th{border:1px solid #ddd;padding:7px;text-align:left;vertical-align:top}th{background:#fafafa}.req-row{background:#fff}.group-row{background:#fbfbfb}@media(max-width:900px){.score-grid{grid-template-columns:repeat(2,1fr)}}
"""


def render_case_html(rec: dict[str, Any], mode: str = "detail") -> str:
    title = str((rec.get("evaluation") or {}).get("dialogue_id") or "样本详情")
    body = _case_card(rec).replace("<details class='case-card'>", "<details class='case-card' open>", 1)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)} - 样本详情</title><style>{_BASE_CASE_STYLE}</style></head>"
        f"<body><div class='top'><p><a href='../report_detail.html'>← 返回详细报告</a> ｜ <a href='../report_simple.html'>返回简洁报告</a></p><h1>样本详情：{_esc(title)}</h1></div>{body}</body></html>"
    )



def _runtime_version_html(records: list[dict[str, Any]], run_info: dict[str, Any] | None = None) -> str:
    info = ((run_info or {}).get("runtime_version") or {})
    versions = [str(((r.get("runtime_version") or {}).get("core_version") or "").strip()) for r in records]
    versions = [v for v in versions if v]
    if not info and versions:
        info = {"core_version": Counter(versions).most_common(1)[0][0]}
    core = str(info.get("core_version") or "未记录")
    note = str(info.get("core_version_note") or "")
    module = str(info.get("version_module") or "")
    version_counts = Counter(versions)
    warn = ""
    if core == "未记录":
        warn = "<p class='warnings'>本结果包没有记录本地评估内核版本，无法判断是否来自最新代码。若仍出现 fix67/fix68 已修过的正包误杀，请先确认没有运行旧目录、旧快捷方式或旧解释器缓存。</p>"
    elif version_counts and (len(version_counts) > 1 or any(v != core for v in version_counts)):
        warn = f"<p class='warnings'>样本级版本与报告版本不一致：{_esc(dict(version_counts))}。建议清空旧 runs 并重新运行。</p>"
    return (
        "<table><tbody>"
        f"<tr><th>本地评估内核版本</th><td>{_esc(core)}</td></tr>"
        f"<tr><th>版本说明</th><td>{_esc(note)}</td></tr>"
        f"<tr><th>实际导入模块</th><td>{_esc(module)}</td></tr>"
        "</tbody></table>" + warn
    )

def render_html(records: list[dict[str, Any]], run_info: dict[str, Any] | None = None, mode: str = "simple") -> str:
    mode = "detail" if str(mode or "simple") == "detail" else "simple"
    detail = mode == "detail"
    graph_ids = Counter(str((r.get("evaluation") or {}).get("graph_id") or "未知图") for r in records)
    graph_text = "；".join(f"{k}×{v}" for k, v in graph_ids.items())
    title = "复杂指令对话评估报告（详细）" if detail else "复杂指令对话评估报告（简洁）"
    mode_links = "<p><a href='report_simple.html'>简洁版</a> ｜ <a href='report_detail.html'>审计版</a></p>"
    detail_note = "点击样本详情页查看证据账本、节点追踪和本地二次筛选。" if detail else "本页只保留全局结果、主要分数和样本索引；审计版包含 schema 质检和详细追踪入口。"
    schema_panel = _schema_linter_html(run_info, detail=detail)
    extra_panel = f"<section class='panel'><h2>四、Schema 质检{'明细' if detail else '概览'}</h2>{schema_panel}</section>"
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title>"
        "<style>"
        "body{font-family:Arial,'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:24px;color:#222}.top{max-width:1280px;margin:auto}.summary{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px;margin:14px 0 22px}.summary-card{background:#fff;border-radius:14px;padding:14px;box-shadow:0 2px 10px #0000000f}.summary-card .num{font-size:26px;font-weight:700;margin-bottom:4px}.panel{background:white;border-radius:16px;padding:18px;margin:0 auto 16px;box-shadow:0 2px 14px #00000012;max-width:1280px}.badge{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:700}.badge.ok{background:#e8f7ed;color:#16833a}.badge.warn{background:#fff3d8;color:#956300}.badge.bad{background:#ffe8e8;color:#ad2c2c}.tag{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px}.tag.neutral{background:#eef2f6;color:#475569}.muted{color:#64748b}.warnings{background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;padding:12px 16px 12px 32px;line-height:1.7}table{border-collapse:collapse;width:100%;margin:10px 0 16px;font-size:13px;background:#fff}td,th{border:1px solid #ddd;padding:7px;text-align:left;vertical-align:top}th{background:#fafafa}a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}details{margin:10px 0}summary{cursor:pointer;font-weight:700;color:#334155}@media(max-width:900px){.summary{grid-template-columns:repeat(2,1fr)}}"
        "</style></head><body>"
        f"<div class='top'><h1>{_esc(title)}</h1>{mode_links}<p class='muted'>状态图：{_esc(graph_text)}。{_esc(detail_note)}</p><div class='summary'>{_result_summary(records, run_info)}</div></div>"
        "<section class='panel'><h2>一、全局结果</h2>"
        "<table><thead><tr><th>Domain</th><th>样本类型</th><th>数量</th><th>本地通过</th><th>仲裁通过</th><th>待仲裁</th><th>不通过</th><th>平均总分</th><th>平均节点分</th><th>平均结构分</th><th>平均知识分</th><th>平均限制分</th></tr></thead>"
        f"<tbody>{_global_breakdown(records)}</tbody></table>"
        f"<h3>正负包区分度</h3>{_score_gap_panel(records)}<h3>负包错误分布与扣分追因</h3>{_negative_issue_panel(records)}</section>"
        f"<section class='panel'><h2>二、模型调用与 Token 用量</h2><table><thead><tr><th>用途</th><th>调用次数</th><th>输入 tokens</th><th>输出 tokens</th><th>合计 tokens</th></tr></thead><tbody>{_token_rows(run_info)}</tbody></table></section>"
        f"<section class='panel'><h2>三、数据装载检查</h2><h3>运行版本</h3>{_runtime_version_html(records, run_info)}{_global_warnings(records, run_info)}{_load_info_html(run_info)}</section>"
        f"{extra_panel}"
        "<section class='panel'><h2>五、样本索引</h2><p class='muted'>点击“查看详情”进入单样本证据账本和节点追踪。</p>"
        "<table><thead><tr><th>样本</th><th>Domain</th><th>类型</th><th>验收</th><th>总分</th><th>节点分</th><th>知识分</th><th>限制分</th><th>预设/主要错误</th><th>一句话结论</th><th>详情</th></tr></thead>"
        f"<tbody>{_case_summary_rows(records)}</tbody></table></section>"
        "</body></html>"
    )
