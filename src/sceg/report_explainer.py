from __future__ import annotations

from typing import Any

from .dataset_interface import AcceptanceResult
from .graph_evaluator import EvaluationResult
from .oracle_router import OracleCandidate


class ReportExplainer:
    """Unified, human-readable explanation for the schema executor.

    The evaluation logic is not split by positive/negative samples.  The report
    first explains what the evaluator found in plain Chinese, then shows node,
    requirement, knowledge, constraint, structure and arbitration traces.
    Dataset acceptance is shown as a separate verification layer.
    """

    def explain(
        self,
        evaluation: EvaluationResult,
        acceptance: AcceptanceResult | None = None,
        oracle_candidates: list[OracleCandidate] | None = None,
    ) -> dict[str, Any]:
        node_names = {n.node_id: n.name for n in evaluation.node_results}
        losses = self._losses(evaluation, acceptance, node_names)
        arbitration = [self._candidate_to_report(x, node_names) for x in (oracle_candidates or [])]
        headline = self._headline(evaluation, losses, arbitration)
        return {
            "headline": headline,
            "plain_summary": self._plain_summary(evaluation, losses, arbitration, acceptance),
            "losses": losses,
            "score_summary": self._score_summary(evaluation),
            "node_requirement_summary": self._node_requirements(evaluation),
            "evidence_ledger": self._evidence_ledger(evaluation),
            "knowledge_summary": self._knowledge(evaluation),
            "constraint_summary": self._constraint(evaluation),
            "structure_summary": self._structure(evaluation, node_names),
            "context_summary": self._context(evaluation),
            "arbitration_summary": arbitration,
            "acceptance_summary": self._acceptance(acceptance),
        }

    def _headline(self, evaluation: EvaluationResult, losses: list[dict[str, str]], arbitration: list[dict[str, Any]]) -> str:
        hard_losses = [x for x in losses if x.get("类型") not in {"无明显失分", "待确认灰区"}]
        if hard_losses:
            first = hard_losses[0]
            evidence = first.get("证据原话") or "暂无直接原话"
            return f"本地评估发现明确失分点。最需要关注的是：{first.get('直白说明', '')} 相关原话：{evidence}。"
        if arbitration:
            return "本地评估没有形成明确扣分事件，但存在需要局部仲裁的灰区；建议只围绕下方列出的节点或小任务进行复核。"
        if evaluation.scores.get("total", 0) >= 85:
            return "本地评估未发现明确失分点；在当前状态图、履约小任务、知识表和限制表下，这段对话完成度较高。"
        return "本地评估总分偏低，但没有形成明确事件；建议优先检查状态图和证据组是否过窄。"

    def _plain_summary(
        self,
        evaluation: EvaluationResult,
        losses: list[dict[str, str]],
        arbitration: list[dict[str, Any]],
        acceptance: AcceptanceResult | None,
    ) -> dict[str, Any]:
        hard = [x for x in losses if x.get("类型") not in {"无明显失分", "待确认灰区"}]
        if hard:
            focus = hard[0]
            conclusion = f"主要失分点是：{focus.get('直白说明', '')}"
            quote = focus.get("证据原话") or focus.get("导致原因") or "没有找到直接证据。"
            why = focus.get("为什么失分") or focus.get("导致原因") or "该处没有满足状态图中的履约要求。"
            suggestion = focus.get("建议") or "回到对应节点和履约小任务检查证据。"
        elif arbitration:
            conclusion = "本地没有确认扣分，但存在需要仲裁的灰区。"
            quote = arbitration[0].get("证据") or "暂无直接证据。"
            why = arbitration[0].get("局部问题") or "本地证据不足以稳定判断。"
            suggestion = "只把该灰区送去仲裁，不要让大模型总评整段对话。"
        else:
            conclusion = "未发现明确失分点。"
            quote = "关键节点和履约小任务均有可用证据。"
            why = "本地评估没有发现事实冲突、限制违规、必需节点缺失或结构关系问题。"
            suggestion = "可进入样本验收或继续查看技术明细。"
        return {
            "一句话结论": conclusion,
            "关键原话": quote,
            "原因解释": why,
            "建议处理": suggestion,
            "样本验收": self._acceptance(acceptance),
        }

    def _turn_line(self, units_by_turn: dict[int, Any], idx: int | None) -> str:
        if idx is None:
            return "无直接轮次"
        unit = units_by_turn.get(idx)
        if not unit:
            return f"第{idx + 1}句：未在证据单元中找到原话"
        speaker = {"assistant": "客服", "user": "用户", "system": "系统"}.get(str(unit.speaker), str(unit.speaker))
        return f"第{idx + 1}句（{speaker}）：{unit.text}"

    def _compact(self, text: Any) -> str:
        return "".join(str(text or "").split()).lower()

    def _locate_text(self, evaluation: EvaluationResult, text: Any) -> list[str]:
        needle = self._compact(text)
        if not needle:
            return []
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        out: list[str] = []
        for u in evaluation.evidence_units:
            if needle in self._compact(u.text):
                out.append(self._turn_line(units_by_turn, u.turn_index))
        return out[:5]

    def _acceptance_target_trace(self, evaluation: EvaluationResult, item: dict[str, Any]) -> dict[str, Any]:
        node_id = item.get("node_id") or item.get("target_node_id") or item.get("target_node")
        req_id = item.get("requirement_id") or item.get("target_id")
        evidence_text = item.get("evidence_span") or item.get("wrong_statement") or item.get("violation_statement") or item.get("mutated_text")
        by_node = {n.node_id: n for n in evaluation.node_results}
        node = by_node.get(str(node_id)) if node_id else None
        req_trace: list[dict[str, Any]] = []
        if node:
            for req in node.requirement_results:
                if req_id and str(req.requirement_id) != str(req_id) and str(req_id) not in [str(x) for x in getattr(req, "aliases", [])]:
                    continue
                req_trace.append({
                    "小任务编号": req.requirement_id,
                    "小任务文本": req.text,
                    "是否命中": req.matched,
                    "小任务分": round(req.score, 4),
                    "证据组": [
                        {
                            "证据组编号": g.group_id,
                            "说明": g.description,
                            "判定": "命中" if g.matched else "缺失",
                            "期望证据": list(getattr(g, "expected_patterns", []) or []),
                            "命中原话": self._hit_lines(g.hits, {u.turn_index: u for u in evaluation.evidence_units}, limit=6),
                        }
                        for g in req.group_matches
                    ],
                })
                if req_id:
                    break
        return {
            "预设错误类型": item.get("error_family") or item.get("type") or item.get("target_kind"),
            "目标节点": node_id,
            "目标小任务/知识/限制": req_id or item.get("knowledge_id") or item.get("constraint_id"),
            "预设证据句": evidence_text or "流程缺失类错误通常没有单句错话，而是目标证据缺失。",
            "证据句定位": self._locate_text(evaluation, evidence_text),
            "节点当前状态": None if not node else {"节点名称": node.name, "状态": node.status, "节点分": round(node.score, 4), "首次命中": self._turn_line({u.turn_index: u for u in evaluation.evidence_units}, node.first_hit_turn)},
            "小任务追踪": req_trace[:3],
            "原始验收项": item,
        }

    def _hit_lines(self, hits: list[Any], units_by_turn: dict[int, Any], limit: int = 4) -> list[str]:
        lines: list[str] = []
        seen: set[tuple[int | None, str]] = set()
        for hit in hits:
            idx = getattr(hit, "turn_index", None)
            text = str(getattr(hit, "text", "") or "")
            key = (idx, text)
            if key in seen:
                continue
            seen.add(key)
            if idx is not None and idx in units_by_turn:
                lines.append(self._turn_line(units_by_turn, idx))
            else:
                prefix = f"第{idx + 1}句：" if isinstance(idx, int) else "未定位轮次："
                lines.append(prefix + text)
            if len(lines) >= limit:
                break
        return lines

    def _nearby_assistant_lines(self, evaluation: EvaluationResult, center: int | None = None, limit: int = 4) -> list[str]:
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        assistants = [u for u in evaluation.evidence_units if u.speaker == "assistant"]
        if center is not None:
            assistants.sort(key=lambda u: abs(u.turn_index - center))
        else:
            assistants = assistants[-limit:]
        picked = sorted(assistants[:limit], key=lambda u: u.turn_index)
        return [self._turn_line(units_by_turn, u.turn_index) for u in picked]

    def _missing_requirement_trace(self, evaluation: EvaluationResult, n: Any, r: Any) -> dict[str, Any]:
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        missing_groups = [g for g in r.group_matches if g.required and not g.matched]
        matched_hits = [h for req in n.requirement_results for g in req.group_matches for h in g.hits]
        first_hit = n.first_hit_turn
        return {
            "定位": {
                "节点编号": n.node_id,
                "节点名称": n.name,
                "小任务编号": r.requirement_id,
                "小任务文本": r.text,
                "节点状态": n.status,
                "节点分": round(n.score, 4),
                "小任务分": round(r.score, 4),
            },
            "缺失证据组": [
                {
                    "证据组编号": g.group_id,
                    "证据组说明": g.description,
                    "期望证据": list(getattr(g, "expected_patterns", []) or []),
                    "当前命中数": len(g.hits),
                }
                for g in missing_groups
            ],
            "同节点已命中原话": self._hit_lines(matched_hits, units_by_turn),
            "可回查附近客服原话": self._nearby_assistant_lines(evaluation, first_hit),
        }

    def _event_trace(self, evaluation: EvaluationResult, *, kind: str, item_id: str | None = None, node_id: str | None = None, turn_index: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        data: dict[str, Any] = {
            "事件类型": kind,
            "事件编号": item_id,
            "关联节点": node_id,
            "证据轮次": None if turn_index is None else turn_index + 1,
            "证据原话": self._turn_line(units_by_turn, turn_index),
        }
        if extra:
            data.update(extra)
        return data

    def _relation_trace(self, evaluation: EvaluationResult, event: Any, node_names: dict[str, str]) -> dict[str, Any]:
        by_id = {n.node_id: n for n in evaluation.node_results}
        source = by_id.get(event.source)
        target = by_id.get(event.target)
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        return {
            "关系类型": event.relation,
            "起点": {"节点编号": event.source, "节点名称": node_names.get(event.source, event.source), "首次命中": self._turn_line(units_by_turn, source.first_hit_turn if source else None), "状态": getattr(source, "status", "无")},
            "终点": {"节点编号": event.target, "节点名称": node_names.get(event.target, event.target), "首次命中": self._turn_line(units_by_turn, target.first_hit_turn if target else None), "状态": getattr(target, "status", "无")},
            "扣分": round(event.penalty, 4),
            "结构证据": event.evidence or event.status,
        }

    def _losses(self, evaluation: EvaluationResult, acceptance: AcceptanceResult | None, node_names: dict[str, str]) -> list[dict[str, Any]]:
        losses: list[dict[str, Any]] = []
        if acceptance:
            for item in acceptance.matched_expected or []:
                family = str(item.get("error_family") or item.get("type") or item.get("target_kind") or "预设问题")
                evidence_text = str(item.get("evidence_span") or item.get("wrong_statement") or item.get("violation_statement") or item.get("mutated_text") or "").strip()
                if evidence_text:
                    evidence = evidence_text
                    reason = "负包预设错误在客服原话中出现，且已被本地验收层确认。"
                else:
                    evidence = "流程缺失类错误：没有单句错话，问题在于目标履约证据缺失。"
                    reason = "负包预设流程缺失已经和节点/小任务状态对齐，目标证据没有完成。"
                losses.append(
                    {
                        "类型": "负包预设问题已命中",
                        "直白说明": f"样本标注的“{family}”已经被评估器识别。",
                        "证据原话": evidence,
                        "为什么失分": reason,
                        "建议": "复查下方验收追踪：先看预设证据句是否在对话中，再看目标节点/知识/限制如何被命中或缺失。",
                        "技术追踪": self._acceptance_target_trace(evaluation, item),
                    }
                )
        for n in evaluation.node_results:
            if n.active:
                missing_reqs = [r for r in n.requirement_results if r.required and not r.matched]
                for r in missing_reqs:
                    losses.append(
                        {
                            "类型": "履约小任务未完成",
                            "直白说明": f"“{n.name}”里的小任务“{r.text or '未命名小任务'}”没有找到足够证据。",
                            "证据原话": "没有命中可证明该小任务完成的客服原话。",
                            "为什么失分": "该小任务属于当前状态图节点的必需履约内容；没有证据时，节点完成度会下降。",
                            "建议": "先看技术追踪里的缺失证据组和附近原话；如果客服确实说到了，应补强建图/编译出的证据表达；如果没说到，就是对话漏讲。",
                            "技术追踪": self._missing_requirement_trace(evaluation, n, r),
                        }
                    )
        for e in evaluation.knowledge_events:
            losses.append(
                {
                    "类型": "知识事实冲突",
                    "直白说明": f"知识点“{e.name}”出现与知识表不一致的表述。",
                    "证据原话": e.evidence,
                    "为什么失分": "客服已经给出明确事实声明，且该声明被知识表中的反驳证据命中。",
                    "建议": "应按知识表中的正确事实重新表述；如果表达存在歧义，应进入局部仲裁。",
                    "技术追踪": self._event_trace(evaluation, kind="知识事实冲突", item_id=e.knowledge_id, node_id=e.node_id, turn_index=e.turn_index, extra={"知识项": e.name, "判定原因": e.reason}),
                }
            )
        for e in evaluation.constraint_events:
            losses.append(
                {
                    "类型": "限制边界违规",
                    "直白说明": f"限制项“{e.name}”被触发，存在越界或不合规风险。",
                    "证据原话": e.evidence,
                    "为什么失分": "客服原话命中了限制表中的违规证据，而不是安全回应证据。",
                    "建议": "应避免越界承诺，并改成合规边界说明或安全拒绝。",
                    "技术追踪": self._event_trace(evaluation, kind="限制边界违规", item_id=e.constraint_id, node_id=e.node_id, turn_index=e.turn_index, extra={"限制项": e.name, "判定原因": e.reason}),
                }
            )
        for r in evaluation.relation_events:
            if r.penalty > 0:
                src = node_names.get(r.source, r.source or "起点")
                tgt = node_names.get(r.target, r.target or "终点")
                losses.append(
                    {
                        "类型": "状态图结构问题",
                        "直白说明": r.reason,
                        "证据原话": r.evidence or r.status,
                        "为什么失分": f"状态图期望“{src}”和“{tgt}”之间满足指定关系，但当前对话没有满足。",
                        "建议": "检查技术追踪里的起点/终点首次命中轮次，看是前置缺失、后续缺失，还是顺序异常。",
                        "技术追踪": self._relation_trace(evaluation, r, node_names),
                    }
                )
        for c in evaluation.context_events:
            if c.status != "已处理":
                losses.append(
                    {
                        "类型": "上下文处理问题",
                        "直白说明": c.reason,
                        "证据原话": c.status,
                        "为什么失分": "上下文策略被触发，但没有找到对应的正确处理证据。",
                        "建议": "查看触发原话和处理原话；若用户只是允许继续简短说明，不应把该句误当作强制终止。",
                        "技术追踪": self._event_trace(evaluation, kind="上下文处理问题", item_id=c.policy_id, turn_index=c.trigger_turn, extra={"触发原话": self._turn_line({u.turn_index: u for u in evaluation.evidence_units}, c.trigger_turn), "处理原话": self._turn_line({u.turn_index: u for u in evaluation.evidence_units}, c.handling_turn), "被置为不适用的节点": c.suppressed_nodes, "状态": c.status}),
                    }
                )
        # “证据不足”不再作为失分项直接塞入主报告。
        # 它只进入仲裁队列或证据账本，避免知识表/限制表被大量灰区记录淹没。
        if acceptance:
            for item in acceptance.oracle_expected or []:
                target = item.get("requirement_id") or item.get("node_id") or item.get("target_node") or item.get("knowledge_id") or item.get("constraint_id") or "未绑定"
                losses.append(
                    {
                        "类型": "待确认灰区",
                        "直白说明": "样本预设问题属于本地未强判的灰区，需要局部仲裁。",
                        "证据原话": str(item.get("evidence_span") or item.get("wrong_statement") or "未提供具体证据句"),
                        "为什么失分": "本地评估器没有形成稳定结论，但该问题可能影响样本验收。",
                        "建议": "只对该节点或小任务做局部仲裁，不应把整段对话交给大模型总评。",
                        "技术追踪": {"验收目标": target, "预设项": item},
                    }
                )
            for item in acceptance.missing_expected:
                target = item.get("requirement_id") or item.get("node_id") or item.get("target_node") or item.get("knowledge_id") or item.get("constraint_id") or "未绑定"
                losses.append(
                    {
                        "类型": "样本验收失败",
                        "直白说明": "样本预设错误没有被当前评估结果识别出来。",
                        "证据原话": str(item.get("evidence_span") or item.get("wrong_statement") or "未提供具体证据句"),
                        "为什么失分": "对话或样本标注要求系统发现该问题，但当前节点、知识或限制证据没有命中。",
                        "建议": "优先检查样本绑定是否正确，以及 schema compiler 是否编译出了足够证据；不要在代码层补词典。",
                        "技术追踪": {"验收目标": target, "预设项": item},
                    }
                )
        if not losses:
            losses.append(
                {
                    "类型": "无明显失分",
                    "直白说明": "未发现明确失分点。",
                    "证据原话": "关键节点和履约小任务均有可用证据。",
                    "为什么失分": "无。",
                    "建议": "可以查看节点、知识、限制和结构关系明细。",
                    "技术追踪": {},
                }
            )
        return losses


    def _evidence_ledger(self, evaluation: EvaluationResult) -> dict[str, Any]:
        units_by_turn = {u.turn_index: u for u in evaluation.evidence_units}
        node_rows: list[dict[str, Any]] = []
        for n in evaluation.node_results:
            for r in n.requirement_results:
                for g in r.group_matches:
                    hit_lines = self._hit_lines(g.hits, units_by_turn, limit=6)
                    nearby = self._nearby_assistant_lines(evaluation, n.first_hit_turn, limit=3) if not hit_lines else []
                    if g.matched:
                        reason = "命中图中证据组，命中原话见右侧。"
                    else:
                        reason = "未在客服原话中命中该证据组要求；请对照期望证据和附近原话判断是对话漏讲还是 schema 表达过窄。"
                    node_rows.append({
                        "账本类型": "节点履约",
                        "节点编号": n.node_id,
                        "节点名称": n.name,
                        "小任务编号": r.requirement_id,
                        "小任务文本": r.text or "未命名小任务",
                        "证据组编号": g.group_id,
                        "期望证据": list(getattr(g, "expected_patterns", []) or []),
                        "判定": "命中" if g.matched else "缺失",
                        "判定原因": reason,
                        "分数": round(g.score, 4),
                        "命中原话": hit_lines,
                        "未命中时附近原话": nearby,
                    })
        knowledge_rows = [
            {
                "账本类型": "知识核验",
                "编号": x.knowledge_id,
                "名称": x.name,
                "结论": x.verdict,
                "证据原话": self._turn_line(units_by_turn, x.turn_index),
                "判定原因": x.reason,
                "说明": "冲突会扣知识分；支持用于证明事实正确；证据不足只进入灰区账本，不直接扣分。",
            }
            for x in evaluation.knowledge_checks if x.verdict in {"支持", "冲突", "证据不足"}
        ]
        constraint_rows = [
            {
                "账本类型": "限制核验",
                "编号": x.constraint_id,
                "名称": x.name,
                "结论": x.verdict,
                "证据原话": self._turn_line(units_by_turn, x.turn_index),
                "判定原因": x.reason,
                "说明": "违规会扣限制分；安全用于证明边界回应正确；证据不足只进入灰区账本，不直接扣分。",
            }
            for x in evaluation.constraint_checks if x.verdict in {"安全", "违规", "证据不足"}
        ]
        relation_rows = [
            {
                "账本类型": "结构关系",
                "关系": x.relation,
                "起点": x.source,
                "终点": x.target,
                "状态": x.status,
                "扣分": round(x.penalty, 4),
                "说明": x.reason,
                "技术追踪": self._relation_trace(evaluation, x, {n.node_id: n.name for n in evaluation.node_results}),
            }
            for x in evaluation.relation_events if x.penalty > 0
        ]
        transcript = [self._turn_line(units_by_turn, u.turn_index) for u in evaluation.evidence_units]
        return {
            "对话原文": transcript,
            "节点履约": node_rows,
            "知识核验": knowledge_rows,
            "限制核验": constraint_rows,
            "结构关系": relation_rows,
            "说明": "证据账本按 期望证据 → 实际原话 → 判定原因 组织，可回查到具体轮次。",
        }

    def _node_requirements(self, evaluation: EvaluationResult) -> list[dict[str, Any]]:
        return [
            {
                "节点编号": n.node_id,
                "节点名称": n.name,
                "节点状态": n.status,
                "节点分": round(n.score, 4),
                "是否触发": n.active,
                "不适用原因": n.inactive_reason,
                "履约小任务": [
                    {
                        "小任务编号": r.requirement_id,
                        "小任务文本": r.text or "未命名小任务",
                        "是否必需": r.required,
                        "小任务分": round(r.score, 4),
                        "是否命中": r.matched,
                        "证据组": [
                            {
                                "证据组编号": g.group_id,
                                "说明": g.description,
                                "是否必需": g.required,
                                "是否命中": g.matched,
                                "分数": round(g.score, 4),
                                "命中原话": [{"轮次": h.turn_index, "原话": h.text} for h in g.hits],
                            }
                            for g in r.group_matches
                        ],
                    }
                    for r in n.requirement_results
                ],
            }
            for n in evaluation.node_results
        ]

    def _knowledge(self, evaluation: EvaluationResult) -> dict[str, Any]:
        return {
            "核验明细": [
                {
                    "知识编号": x.knowledge_id,
                    "知识项": x.name,
                    "结论": x.verdict,
                    "证据原话": x.evidence,
                    "轮次": x.turn_index,
                    "说明": x.reason,
                    "严重程度": x.severity,
                }
                for x in evaluation.knowledge_checks
            ]
        }

    def _constraint(self, evaluation: EvaluationResult) -> dict[str, Any]:
        return {
            "核验明细": [
                {
                    "限制编号": x.constraint_id,
                    "限制项": x.name,
                    "结论": x.verdict,
                    "证据原话": x.evidence,
                    "轮次": x.turn_index,
                    "说明": x.reason,
                    "严重程度": x.severity,
                }
                for x in evaluation.constraint_checks
            ]
        }

    def _structure(self, evaluation: EvaluationResult, node_names: dict[str, str]) -> dict[str, Any]:
        return {
            "结构分": evaluation.scores.get("relation_score"),
            "事件": [
                {
                    "关系类型": x.relation,
                    "起点": node_names.get(x.source, x.source),
                    "终点": node_names.get(x.target, x.target),
                    "状态": x.status,
                    "扣分": round(x.penalty, 4),
                    "说明": x.reason,
                    "证据": x.evidence,
                    "技术追踪": self._relation_trace(evaluation, x, node_names),
                }
                for x in evaluation.relation_events
            ],
        }

    def _context(self, evaluation: EvaluationResult) -> dict[str, Any]:
        return {
            "事件": [
                {
                    "策略编号": x.policy_id,
                    "状态": x.status,
                    "触发轮次": x.trigger_turn,
                    "处理轮次": x.handling_turn,
                    "被置为不适用的节点": x.suppressed_nodes,
                    "说明": x.reason,
                }
                for x in evaluation.context_events
            ]
        }

    def _candidate_to_report(self, x: OracleCandidate, node_names: dict[str, str]) -> dict[str, Any]:
        location = node_names.get(x.node_id or "", x.node_id or x.knowledge_id or x.constraint_id or x.context_id or "未绑定")
        return {
            "候选类型": x.kind,
            "绑定位置": location,
            "局部问题": x.question,
            "证据": "；".join(str(e) for e in x.evidence) if x.evidence else "暂无直接证据",
            "必要性": round(x.need, 4),
            "证据强度": round(x.strength, 4),
            "技术追踪": {
                "候选编号": x.candidate_id,
                "节点编号": x.node_id,
                "小任务编号": x.requirement_id,
                "知识编号": x.knowledge_id,
                "限制编号": x.constraint_id,
                "上下文编号": x.context_id,
            },
        }

    def _acceptance(self, acceptance: AcceptanceResult | None) -> dict[str, Any] | None:
        if not acceptance:
            return None
        return {
            "验收结果": acceptance.result,
            "是否通过": acceptance.passed,
            "说明": acceptance.reasons,
            "已命中预设问题数": len(acceptance.matched_expected),
            "未命中预设问题数": len(acceptance.missing_expected),
            "待仲裁预设问题数": len(acceptance.oracle_expected or []),
        }

    def _score_summary(self, evaluation: EvaluationResult) -> dict[str, Any]:
        s = evaluation.scores
        return {
            "总分": s.get("total"),
            "节点完成分": s.get("node_completion"),
            "结构关系分": s.get("relation_score"),
            "知识正确分": s.get("knowledge_score"),
            "限制合规分": s.get("constraint_score"),
        }
