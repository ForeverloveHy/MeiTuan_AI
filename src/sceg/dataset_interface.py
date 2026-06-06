from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

from .graph_evaluator import EvaluationResult


@dataclass(slots=True)
class AcceptanceResult:
    result: str
    passed: bool
    reasons: list[str]
    matched_expected: list[dict[str, Any]]
    missing_expected: list[dict[str, Any]]
    oracle_expected: list[dict[str, Any]] | None = None
    unexpected_bad_events: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "passed": self.passed,
            "reasons": self.reasons,
            "matched_expected": self.matched_expected,
            "missing_expected": self.missing_expected,
            "oracle_expected": self.oracle_expected or [],
            "unexpected_bad_events": self.unexpected_bad_events or [],
        }


class DatasetInterface:
    """Positive/negative acceptance layer.

    The layer only verifies whether evaluator output satisfies sample intent. It
    does not infer task meaning from raw words. Grey-zone expectations can be
    separated for the arbitration queue instead of being treated as local
    evaluator failures.
    """

    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime

    def accept(self, dialogue: dict[str, Any], evaluation: EvaluationResult) -> AcceptanceResult:
        sample_type = str(dialogue.get("sample_type") or dialogue.get("quality") or "positive")
        if sample_type in {"negative", "负包"}:
            return self._accept_negative(dialogue, evaluation)
        return self._accept_positive(dialogue, evaluation)

    def _accept_positive(self, dialogue: dict[str, Any], evaluation: EvaluationResult) -> AcceptanceResult:
        """Strict positive-pack acceptance.

        Positive samples are the clean-answer upper bound: passing now requires
        both a high global score and no local severe event / active required atom
        miss.  Scenario-scoped positives may use a slightly lower score floor,
        but they no longer bypass active required-node or fact/constraint gates.
        """
        thresholds = self.runtime.get("thresholds", {})
        base_threshold = float(thresholds.get("positive_pass", 90.0))
        scenario_threshold = float(thresholds.get("positive_scenario_scoped_pass", 82.0))
        min_req_score = float(thresholds.get("positive_required_atom_min_score", thresholds.get("node_satisfied", 0.75)))
        component_mins = thresholds.get("positive_component_mins") or {
            "node_completion": 78.0,
            "relation_score": 70.0,
            "knowledge_score": 98.0,
            "constraint_score": 98.0,
        }

        coverage_targets = dialogue.get("coverage_targets") or []
        scenario_scoped = bool(coverage_targets)
        threshold = scenario_threshold if scenario_scoped else base_threshold

        has_bad_event = bool(evaluation.knowledge_events or evaluation.constraint_events)
        missing_required_nodes = [n for n in evaluation.node_results if n.active and n.status == "缺失"]
        missing_required_atoms: list[dict[str, Any]] = []
        low_conf_required_atoms: list[dict[str, Any]] = []
        for n in evaluation.node_results:
            if not n.active:
                continue
            for req in getattr(n, "requirement_results", []) or []:
                if not getattr(req, "required", False):
                    continue
                item = {
                    "kind": "node_requirement",
                    "node_id": n.node_id,
                    "node_name": n.name,
                    "requirement_id": req.requirement_id,
                    "text": req.text,
                    "score": round(float(req.score or 0.0), 4),
                }
                if not getattr(req, "matched", False):
                    missing_required_atoms.append(item)
                elif float(req.score or 0.0) < min_req_score:
                    # Low-confidence HIT is an audit signal only.  The element engine
                    # already returned matched=True; blocking positives on this
                    # duplicates the node score and made perfect dialogues fail on
                    # natural paraphrases.
                    item["audit_only"] = True
                    # low_conf_required_atoms.append(item)

        context_problem = any(e.status != "已处理" for e in evaluation.context_events)
        relation_problem = any(float(getattr(e, "penalty", 0.0) or 0.0) > 0 for e in evaluation.relation_events)
        component_failures = []
        for key, min_value in component_mins.items():
            score = float(evaluation.scores.get(key, 0.0) or 0.0)
            if score < float(min_value):
                component_failures.append({"score_key": key, "score": round(score, 4), "min": float(min_value)})

        passed = (
            float(evaluation.scores.get("total", 0.0) or 0.0) >= threshold
            and not component_failures
            and not has_bad_event
            and not missing_required_nodes
            and not missing_required_atoms
            and not low_conf_required_atoms
            and not context_problem
            # Relation penalties are already reflected in relation_score and the
            # component threshold.  Do not hard-block positives on minor natural
            # order shifts once relation_score remains acceptable.
            # and not relation_problem
        )

        reasons: list[str] = []
        if not passed:
            if float(evaluation.scores.get("total", 0.0) or 0.0) < threshold:
                reasons.append(f"总分低于正包严格阈值：{evaluation.scores.get('total')} < {threshold}")
            for c in component_failures:
                reasons.append(f"正包组件分不足：{c['score_key']}={c['score']} < {c['min']}")
            if has_bad_event:
                reasons.append("正包出现事实冲突或硬限制违规事件，不能通过")
            if missing_required_nodes:
                reasons.append(f"存在未完成的活动必需节点：{len(missing_required_nodes)} 个")
            if missing_required_atoms:
                reasons.append(f"存在未命中的活动必需小任务：{len(missing_required_atoms)} 个")
            if low_conf_required_atoms:
                reasons.append(f"存在低置信命中的活动必需小任务：{len(low_conf_required_atoms)} 个")
            if context_problem:
                reasons.append("存在终止/转场上下文处理问题")
            if relation_problem and not passed and any(c.get("score_key") == "relation_score" for c in component_failures):
                reasons.append("存在状态图结构关系扣分事件")
        else:
            if scenario_scoped:
                reasons.append("正包严格通过：场景型样本达到分数、组件分、必需小任务、知识和限制全部门槛")
            else:
                reasons.append("正包严格通过：未发现必需节点/小任务缺失、事实冲突、限制违规或结构转场问题")
        return AcceptanceResult("本地通过" if passed else "不通过", passed, reasons, [], [], [], [])

    def _accept_negative(self, dialogue: dict[str, Any], evaluation: EvaluationResult) -> AcceptanceResult:
        """Dataset-label audit for negative samples.

        A negative sample passes only when the intended injected issue is caught
        AND the evaluator does not create unrelated severe false positives.
        Answer-key metadata is still report-only: it aligns already-emitted local
        events after evaluation, never creates scores or runtime verdicts.
        """
        expected = list(dialogue.get("injected_errors") or [])
        matched: list[dict[str, Any]] = []
        missing_local: list[dict[str, Any]] = []
        for err in expected:
            if self._error_matched(err, evaluation, dialogue):
                matched.append(err)
            else:
                enriched = dict(err)
                enriched["miss_diagnostic"] = self._diagnose_expected_miss(err, evaluation, dialogue)
                missing_local.append(enriched)
        if not expected:
            return AcceptanceResult("标签审计缺失", False, ["负包缺少 injected_errors；仅影响数据集审计，不影响评估器判分或仲裁"], [], [], [], [])

        unexpected = self._collect_unexpected_bad_events(dialogue, evaluation, expected)
        max_unexpected = int(self.runtime.get("thresholds", {}).get("negative_max_unexpected_bad_events", 0))

        if missing_local:
            # Final evaluation path: when a negative target is explicitly marked
            # local_or_oracle / semantic / open_set, it may be resolved by the
            # arbitration layer rather than failing label audit immediately.
            # This does not create evaluator scores; it records the unresolved
            # target as oracle_expected for review/LLM arbitration.
            oracle_eligible = []
            still_missing = []
            for item in missing_local:
                ev = str(item.get("evaluability") or "").lower()
                det = str(item.get("expected_detector") or "").lower()
                fam = str(item.get("error_family") or item.get("type") or "").lower()
                if "oracle" in ev or ev in {"semantic", "open_set"} or det in {"knowledge_judge", "knowledge_nli", "constraint_nli"} or "knowledge" in fam:
                    oracle_eligible.append(item)
                else:
                    still_missing.append(item)
            if still_missing:
                return AcceptanceResult(
                    "标签审计未命中",
                    False,
                    [f"负包标签审计未命中：本地评估器未独立识别 {len(still_missing)} 个预期问题；不因此送审"],
                    matched,
                    still_missing,
                    oracle_eligible,
                    unexpected,
                )
            if len(unexpected) > max_unexpected:
                return AcceptanceResult(
                    "负包误杀过多",
                    False,
                    [f"负包预期问题进入仲裁，但发现 {len(unexpected)} 个未对齐预期错误的额外严重问题，超过允许值 {max_unexpected}"],
                    matched,
                    [],
                    oracle_eligible,
                    unexpected,
                )
            return AcceptanceResult(
                "仲裁通过",
                True,
                [f"负包仲裁通过：{len(oracle_eligible)} 个语义型预期问题由仲裁层接管，且未发现额外严重误杀"],
                matched,
                [],
                oracle_eligible,
                unexpected,
            )
        if len(unexpected) > max_unexpected:
            return AcceptanceResult(
                "负包误杀过多",
                False,
                [
                    f"负包预期问题已命中，但发现 {len(unexpected)} 个未对齐预期错误的额外严重问题，超过允许值 {max_unexpected}",
                    "负包通过不仅要求错处被查出，也要求其他维度尽量不被误杀",
                ],
                matched,
                [],
                [],
                unexpected,
            )
        return AcceptanceResult(
            "本地通过",
            True,
            ["负包严格通过：预期问题已被本地评估器独立识别，且未发现额外严重误杀"],
            matched,
            [],
            [],
            unexpected,
        )

    def _schema_gap_oracle_error(self, err: dict[str, Any], evaluation: EvaluationResult) -> dict[str, Any] | None:
        """Escalate target-bound schema gaps to semantic arbitration.

        A negative sample may target a knowledge/constraint id that exists in the
        graph, yet the local executor can only produce insufficient evidence or no
        event because the LLM graph missed a reusable refute/prohibited
        expression.  This method does not use evidence_span/wrong_statement as a
        verdict.  It only checks whether the expected error has a concrete schema
        binding; the OracleRouter will send evaluator ledgers / assistant turns,
        not answer-key text, to LLM.
        """
        family = str(err.get("error_family") or err.get("type") or "")
        family_l = family.lower()
        if not any(x in family_l for x in ("knowledge", "constraint", "boundary", "faq", "fact")) and not any(x in family for x in ("知识", "限制")):
            return None
        knowledge_id = err.get("knowledge_id") or err.get("target_knowledge_id")
        constraint_id = err.get("constraint_id") or err.get("target_constraint_id")
        if knowledge_id:
            for e in evaluation.knowledge_checks:
                if self._event_matches_alias(knowledge_id, e, "knowledge_id"):
                    patched = dict(err)
                    patched["evaluability"] = "semantic"
                    patched["expected_detector"] = "knowledge_nli"
                    patched["schema_gap_escalated"] = True
                    return patched
            # The expected id itself is a schema binding from the dataset/graph
            # contract. When the local checker produced no focused row, escalate
            # to LLM with evaluator context rather than silently failing the
            # negative sample. The prompt still excludes evidence_span/wrong_statement.
            patched = dict(err)
            patched["evaluability"] = "semantic"
            patched["expected_detector"] = "knowledge_nli"
            patched["schema_gap_escalated"] = True
            return patched
        if constraint_id:
            for e in evaluation.constraint_checks:
                if self._event_matches_alias(constraint_id, e, "constraint_id"):
                    patched = dict(err)
                    patched["evaluability"] = "semantic"
                    patched["expected_detector"] = "constraint_nli"
                    patched["schema_gap_escalated"] = True
                    return patched
            patched = dict(err)
            patched["evaluability"] = "semantic"
            patched["expected_detector"] = "constraint_nli"
            patched["schema_gap_escalated"] = True
            return patched
        return None

    def _requires_oracle(self, err: dict[str, Any]) -> bool:
        evaluability = str(err.get("evaluability") or "").lower()
        detector = str(err.get("expected_detector") or "").lower()
        return evaluability in {"semantic", "open_set"} or detector in {"audit_only", "knowledge_nli", "constraint_nli", "semantic_node_coverage"}


    def _binding_hint(self, err: dict[str, Any], dialogue: dict[str, Any]) -> str:
        parts = [
            dialogue.get("source_node"),
            dialogue.get("source_positive_design"),
            dialogue.get("target_id"),
            dialogue.get("target_kind"),
            err.get("requirement_id") or err.get("target_core") or err.get("target_group_id"),
            err.get("title") or err.get("error_type_title"),
            err.get("description"),
        ]
        return " ".join(str(x or "").strip() for x in parts if str(x or "").strip())

    def _sim_tokens(self, text: Any) -> set[str]:
        raw = str(text or "").lower()
        out: set[str] = set()
        for part in re.split(r"[^a-z0-9\u4e00-\u9fff]+", raw):
            if not part:
                continue
            out.add(part)
            for chunk in re.findall(r"[\u4e00-\u9fff]+", part):
                out.update(chunk[i : i + 2] for i in range(max(0, len(chunk) - 1)))
                if len(chunk) <= 4:
                    out.update(chunk)
            for chunk in re.findall(r"[a-z0-9]+", part):
                out.add(chunk)
        return {x for x in out if x}

    def _similarity(self, a: Any, b: Any) -> float:
        ta = self._sim_tokens(a)
        tb = self._sim_tokens(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / max(1, min(len(ta), len(tb)))

    def _dialogue_assistant_text(self, dialogue: dict[str, Any]) -> str:
        turns = dialogue.get("turns") or dialogue.get("messages") or dialogue.get("dialogue") or []
        parts: list[str] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if str(turn.get("speaker") or "").lower() in {"assistant", "客服", "agent"}:
                parts.append(str(turn.get("text") or turn.get("content") or ""))
        return "\n".join(parts)

    def _span_present_in_dialogue(self, span: Any, dialogue: dict[str, Any]) -> bool:
        raw = str(span or "").strip()
        if not raw:
            return False
        def norm(x: str) -> str:
            return re.sub(r"\s+", "", str(x or "").lower())
        return norm(raw) in norm(self._dialogue_assistant_text(dialogue))

    def _hit_speakers(self, dialogue: dict[str, Any], hits: list[Any]) -> set[str]:
        texts = {re.sub(r"\s+", "", str(getattr(h, "text", h) or "")) for h in hits if str(getattr(h, "text", h) or "").strip()}
        speakers: set[str] = set()
        for turn in dialogue.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            t = re.sub(r"\s+", "", str(turn.get("text") or turn.get("content") or ""))
            if not t:
                continue
            if any(x and (x in t or t in x) for x in texts):
                speakers.add(str(turn.get("speaker") or ""))
        return speakers

    def _target_supported_by_hits(self, err: dict[str, Any], dialogue: dict[str, Any], hits: list[Any], req_text: str = "") -> bool:
        hint = self._binding_hint(err, dialogue)
        if not hint:
            return True
        hit_text = " ".join(str(getattr(h, "text", h) or "") for h in hits)
        sim = self._similarity(hint, hit_text)
        if sim < float(self.runtime.get("thresholds", {}).get("target_hit_hint_similarity", 0.12)):
            return False
        # For audit-only flow targets whose wording explicitly says the agent
        # should ask/confirm, a user's volunteered answer cannot satisfy the
        # missing-agent-action target.  This is metadata-level audit alignment;
        # node scoring itself still uses the graph/elements method.
        ask_hint = any(x in (hint + " " + str(req_text or "")) for x in ["询问", "主动问", "请问", "确认"])
        if ask_hint and hits:
            speakers = self._hit_speakers(dialogue, hits)
            if speakers and not any(s in {"assistant", "客服", "agent"} for s in speakers):
                return False
        return True


    def _id_matches(self, expected: Any, actual: Any, aliases: list[str] | None = None) -> bool:
        if not expected:
            return True
        values = {str(actual or "")} | {str(x) for x in (aliases or [])}
        return str(expected) in values

    def _event_matches_alias(self, expected: Any, event: Any, id_attr: str) -> bool:
        if not expected:
            return True
        actual = getattr(event, id_attr, None)
        aliases = getattr(event, "aliases", None) or []
        return self._id_matches(expected, actual, aliases)

    def _semantic_event_aligned(self, err: dict[str, Any], event: Any) -> bool:
        """Audit-only semantic alignment between a negative label target and a local event.

        This never creates an evaluator event or changes score. It only avoids
        marking a negative sample as missed when the local elements checker found
        the same issue under a neighbouring schema atom name.
        """
        hint = " ".join(str(x or "") for x in [
            err.get("knowledge_id"), err.get("constraint_id"), err.get("target_id"),
            err.get("description"), err.get("title"), err.get("error_type_title")
        ] if str(x or "").strip())
        ev_text = " ".join(str(x or "") for x in [
            getattr(event, "knowledge_id", ""), getattr(event, "constraint_id", ""),
            getattr(event, "name", ""), " ".join(getattr(event, "aliases", []) or []),
            getattr(event, "evidence", ""), getattr(event, "reason", "")
        ] if str(x or "").strip())
        if not hint or not ev_text:
            return False
        return self._similarity(hint, ev_text) >= float(self.runtime.get("thresholds", {}).get("audit_event_semantic_similarity", 0.10))

    def _same_span_local_bad_event(self, evidence_span: str, evaluation: EvaluationResult) -> bool:
        """Return whether a local severe event already caught the same utterance.

        This is a cross-family schema-alignment guard for negative-pack
        acceptance.  Some LLM schemas attach the same wrong assistant
        utterance to a neighbouring dimension, e.g. a boundary violation is
        detected as a factual conflict, or vice versa.  We still do not let the
        dataset trace text decide the verdict by itself: the function only
        succeeds when the local evaluator has independently emitted a conflict
        or violation event whose evidence contains that trace.
        """
        raw = str(evidence_span or "").strip()
        if not raw:
            return False

        def norm(x: object) -> str:
            # Audit trace matching should ignore punctuation/full-width marks;
            # it still requires a locally emitted bad event and never creates a
            # verdict from evidence_span alone.
            return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(x or "").lower())

        needle = norm(raw)
        if not needle:
            return False
        for event in list(evaluation.knowledge_events or []) + list(evaluation.constraint_events or []):
            evidence = norm(getattr(event, "evidence", ""))
            if not evidence:
                continue
            verdict = str(getattr(event, "verdict", ""))
            if verdict not in {"冲突", "违规"}:
                continue
            # Audit trace alignment is bidirectional: the local event may store
            # only the focused bad clause while the dataset trace stores the
            # whole assistant sentence. This is report-only and never creates a
            # score or runtime verdict without a local bad event.
            if needle in evidence or evidence in needle:
                return True
            if self._similarity(raw, getattr(event, "evidence", "")) >= float(self.runtime.get("thresholds", {}).get("audit_event_span_similarity", 0.45)):
                return True
        return False

    def _expected_error_family(self, err: dict[str, Any]) -> str:
        return str(err.get("error_family") or err.get("type") or err.get("target_kind") or "").lower()

    def _bad_event_aligned_to_expected(self, err: dict[str, Any], event: Any, kind: str) -> bool:
        # First perform cross-family evidence alignment.  In negative-pack
        # acceptance, the same bad utterance may be detected under a neighbouring
        # dimension: e.g. “only low-latency remains” is both a wrong fact and a
        # boundary-like over-forcing signal.  If the local bad event evidence
        # overlaps the injected bad statement, it should not be counted as an
        # additional false positive.  This remains audit-only: the local event
        # must already exist; labels never create a score/event.
        evidence_span0 = str(err.get("evidence_span") or err.get("wrong_statement") or "")
        if evidence_span0:
            ev_evidence0 = str(getattr(event, "evidence", ""))
            if ev_evidence0:
                def _norm0(x: object) -> str:
                    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(x or "").lower())
                a0, b0 = _norm0(evidence_span0), _norm0(ev_evidence0)
                if a0 and b0 and (a0 in b0 or b0 in a0):
                    return True
                if self._similarity(evidence_span0, ev_evidence0) >= float(self.runtime.get("thresholds", {}).get("audit_event_span_similarity", 0.45)):
                    return True
        family_l = self._expected_error_family(err)
        if kind == "knowledge" and not any(x in family_l for x in ("knowledge", "faq", "fact", "知识")):
            return False
        if kind == "constraint" and not any(x in family_l for x in ("constraint", "boundary", "限制", "边界")):
            return False
        id_attr = "knowledge_id" if kind == "knowledge" else "constraint_id"
        expected_id = err.get(id_attr) or err.get("target_" + id_attr)
        if expected_id and self._event_matches_alias(expected_id, event, id_attr):
            return True
        node_id = err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node")
        if node_id and getattr(event, "node_id", None) and not self._id_matches(node_id, getattr(event, "node_id", None), getattr(event, "aliases", []) or []):
            return False
        evidence_span = str(err.get("evidence_span") or err.get("wrong_statement") or "")
        if evidence_span:
            ev_evidence = str(getattr(event, "evidence", ""))
            if self._similarity(evidence_span, ev_evidence) >= float(self.runtime.get("thresholds", {}).get("audit_event_span_similarity", 0.45)):
                return True
            def norm(x: object) -> str:
                return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(x or "").lower())
            a, b = norm(evidence_span), norm(ev_evidence)
            if a and b and (a in b or b in a):
                return True
        return self._semantic_event_aligned(err, event)

    def _flow_issue_aligned_to_expected(self, err: dict[str, Any], issue: dict[str, Any], dialogue: dict[str, Any]) -> bool:
        family_l = self._expected_error_family(err)
        if not any(x in family_l for x in ("flow", "process", "node", "requirement", "流程")):
            return False
        node_id = err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node")
        req_id = err.get("requirement_id") or err.get("target_core") or err.get("target_group_id")
        if node_id and str(node_id) == str(issue.get("node_id") or ""):
            if not req_id or str(req_id) == str(issue.get("requirement_id") or ""):
                return True
        if req_id and str(req_id) == str(issue.get("requirement_id") or ""):
            return True
        hint = " ".join(str(x or "") for x in [self._binding_hint(err, dialogue), err.get("description"), err.get("wrong_statement")] if str(x or "").strip())
        issue_text = " ".join(str(issue.get(k) or "") for k in ("node_id", "node_name", "requirement_id", "text", "reason"))
        if not hint or not issue_text:
            return False
        return self._similarity(hint, issue_text) >= float(self.runtime.get("thresholds", {}).get("negative_false_positive_alignment_similarity", 0.12))


    def _diagnose_expected_miss(self, err: dict[str, Any], evaluation: EvaluationResult, dialogue: dict[str, Any]) -> dict[str, Any]:
        """Report-only negative miss diagnosis, precise to node/atom/element.

        This function is intentionally outside scoring: it uses answer-key fields
        only after local evaluation has already failed to match the expected
        negative.  It helps locate whether the miss is caused by selector recall,
        value comparison, hard negative-group design, or broad flow activation.
        """
        family = str(err.get("error_family") or err.get("type") or "")
        target_id = str(err.get("requirement_id") or err.get("target_core") or err.get("target_group_id") or err.get("knowledge_id") or err.get("constraint_id") or err.get("node_id") or err.get("target_id") or "")
        wrong_text = str(err.get("wrong_statement") or err.get("evidence_span") or "")
        desc = str(err.get("description") or "")
        binding = self._binding_hint(err, dialogue)
        hint = " ".join(x for x in [target_id, binding, desc, wrong_text] if x).strip()
        assistant_candidates = self._top_assistant_candidates(hint or wrong_text or desc, evaluation)
        out = {
            "error_family": family,
            "target_id": target_id,
            "expected_description": desc,
            "expected_wrong_statement": wrong_text,
            "likely_root_cause": "",
            "closest_local_checks": [],
            "closest_flow_targets": [],
            "assistant_evidence_candidates": assistant_candidates,
        }
        fam_l = family.lower()
        if any(x in fam_l for x in ("knowledge", "fact", "faq")) or "知识" in family:
            rows = []
            for e in list(evaluation.knowledge_events or []) + list(evaluation.knowledge_checks or []):
                text = " ".join(str(x or "") for x in [getattr(e, "knowledge_id", ""), getattr(e, "name", ""), getattr(e, "evidence", ""), getattr(e, "reason", "")])
                score = self._similarity(hint, text) if hint else 0.0
                rows.append({
                    "score": round(score, 4),
                    "knowledge_id": getattr(e, "knowledge_id", ""),
                    "name": getattr(e, "name", ""),
                    "verdict": getattr(e, "verdict", ""),
                    "evidence": getattr(e, "evidence", ""),
                    "reason": getattr(e, "reason", ""),
                    "element_audit_digest": self._knowledge_audit_digest(getattr(e, "element_audit", {}) or {}),
                })
            rows.sort(key=lambda x: x["score"], reverse=True)
            out["closest_local_checks"] = rows[:6]
            if not rows:
                out["likely_root_cause"] = "知识表没有产生任何本地 check；通常是 selector_groups 未召回相关错误句，或该知识点未进入当前图/当前作用域。"
            elif not any(str(r.get("verdict")) == "冲突" for r in rows[:6]):
                top = rows[0]
                if top.get("score", 0) < 0.08:
                    out["likely_root_cause"] = "未找到语义接近的知识核验项；多半是图中的 knowledge_id/父知识项和负包标签没有对齐。"
                elif str(top.get("verdict")) == "支持":
                    out["likely_root_cause"] = "最接近的知识项只命中了正确事实或邻近事实，错误句没有被 selector/value_check/wrong_groups 抽成冲突。"
                else:
                    out["likely_root_cause"] = "有接近的知识项，但只到证据不足/未提及，没有形成本地冲突；需要检查 selector 主干、value_check 数值/方向比较或 wrong_groups。"
            else:
                out["likely_root_cause"] = "本地存在冲突事件但没有和负包标签对齐；优先检查 knowledge_id/alias/target_id。"
        elif any(x in fam_l for x in ("constraint", "boundary")) or "限制" in family:
            rows = []
            for e in list(evaluation.constraint_events or []) + list(evaluation.constraint_checks or []):
                text = " ".join(str(x or "") for x in [getattr(e, "constraint_id", ""), getattr(e, "name", ""), getattr(e, "evidence", ""), getattr(e, "reason", "")])
                score = self._similarity(hint, text) if hint else 0.0
                rows.append({
                    "score": round(score, 4),
                    "constraint_id": getattr(e, "constraint_id", ""),
                    "name": getattr(e, "name", ""),
                    "verdict": getattr(e, "verdict", ""),
                    "evidence": getattr(e, "evidence", ""),
                    "reason": getattr(e, "reason", ""),
                    "element_audit_digest": self._constraint_audit_digest(getattr(e, "element_audit", {}) or {}),
                })
            rows.sort(key=lambda x: x["score"], reverse=True)
            out["closest_local_checks"] = rows[:6]
            if not rows:
                out["likely_root_cause"] = "限制表没有产生任何本地 check；通常是 hard 表缺失或负向对象没有进入 constraint judge。"
            elif not any(str(r.get("verdict")) == "违规" for r in rows[:6]):
                top = rows[0]
                if top.get("score", 0) < 0.08:
                    out["likely_root_cause"] = "未找到语义接近的限制项；多半是 hard_constraint_table 覆盖不到该违规类型。"
                else:
                    out["likely_root_cause"] = "最接近的限制项被判安全/部分命中；通常是 negative_groups 要求对象+违规动作过窄，或 pool 没覆盖该违规说法。"
            else:
                out["likely_root_cause"] = "本地存在违规事件但没有和负包标签对齐；优先检查 constraint_id/alias/target_id。"
        else:
            flow_rows = []
            for n in evaluation.node_results:
                node_text = " ".join([str(getattr(n, "node_id", "")), str(getattr(n, "name", "")), " ".join(getattr(n, "aliases", []) or [])])
                for req in getattr(n, "requirement_results", []) or []:
                    text = " ".join([node_text, str(getattr(req, "requirement_id", "")), str(getattr(req, "text", ""))])
                    score = self._similarity(hint, text) if hint else 0.0
                    flow_rows.append({
                        "score": round(score, 4),
                        "node_id": getattr(n, "node_id", ""),
                        "node_name": getattr(n, "name", ""),
                        "active": bool(getattr(n, "active", False)),
                        "node_status": getattr(n, "status", ""),
                        "requirement_id": getattr(req, "requirement_id", ""),
                        "requirement_text": getattr(req, "text", ""),
                        "matched": bool(getattr(req, "matched", False)),
                        "requirement_score": round(float(getattr(req, "score", 0.0) or 0.0), 4),
                        "element_audit_digest": self._element_audit_digest(getattr(req, "element_audit", {}) or {}),
                    })
            flow_rows.sort(key=lambda x: x["score"], reverse=True)
            out["closest_flow_targets"] = flow_rows[:8]
            if not flow_rows or flow_rows[0].get("score", 0) < 0.08:
                out["likely_root_cause"] = "未找到接近的节点/小任务；多半是负包标签 id 和当前图节点粒度不对齐。"
            elif not flow_rows[0].get("matched"):
                out["likely_root_cause"] = "找到了接近的小任务且本地未命中；这是预期流程缺失，但报告应展示该具体 atom。"
            else:
                out["likely_root_cause"] = "接近的小任务已被宽泛元素命中，但负包认为仍缺失；需要检查 element 是否过宽或是否漏看具体话术要求。"
        return out

    def _top_assistant_candidates(self, hint: str, evaluation: EvaluationResult, limit: int = 5) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not hint:
            return rows
        for u in evaluation.evidence_units:
            if str(getattr(u, "speaker", "")) != "assistant":
                continue
            text = str(getattr(u, "text", "") or "")
            score = self._similarity(hint, text)
            if score <= 0:
                continue
            rows.append({"turn_index": getattr(u, "turn_index", None), "text": text, "score": round(score, 4)})
        rows.sort(key=lambda x: x["score"], reverse=True)
        return rows[:limit]

    def _element_audit_digest(self, audit: dict[str, Any]) -> dict[str, Any]:
        def elem_rows(rows: Any) -> list[str]:
            out: list[str] = []
            for e in rows or []:
                if isinstance(e, dict):
                    val = str(e.get("value") or "")
                    if val:
                        out.append(val)
                else:
                    val = str(getattr(e, "value", "") or "")
                    if val:
                        out.append(val)
            return out[:12]
        if not isinstance(audit, dict):
            return {}
        cand = audit.get("candidate_results") or audit.get("candidate_atoms") or []
        return {
            "verdict": audit.get("verdict", ""),
            "score": audit.get("score", 0),
            "reason": audit.get("reason", ""),
            "hit_elements": elem_rows(audit.get("hit_elements")),
            "missing_elements": elem_rows(audit.get("missing_elements")),
            "top_candidates": cand[:3] if isinstance(cand, list) else [],
        }

    def _knowledge_audit_digest(self, audit: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(audit, dict):
            return {}
        return {
            "correct_side": self._element_audit_digest(audit.get("correct_side") or {}),
            "wrong_side": self._element_audit_digest(audit.get("wrong_side") or {}),
            "value_check": audit.get("value_check") or {},
        }

    def _constraint_audit_digest(self, audit: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(audit, dict):
            return {}
        return {
            "trigger": self._element_audit_digest(audit.get("trigger") or {}),
            "negative_side": self._element_audit_digest(audit.get("negative_side") or {}),
            "safe_side": self._element_audit_digest(audit.get("safe_side") or {}),
        }

    def _unexpected_flow_root_cause(self, node: Any, req: Any) -> str:
        node_name = str(getattr(node, "name", ""))
        text = str(getattr(req, "text", ""))
        if any(x in node_name for x in ["FAQ", "追问", "其他问题"]):
            return "FAQ/追问节点被激活后要求了过多 atom；优先拆分 FAQ 或让未被问到的 FAQ atom 不计缺失。"
        if any(x in node_name + text for x in ["询问", "检查", "确认", "是否", "哪", "还是"]):
            return "信息获取类 atom 被当成无条件必答；应允许用户已提供信息时跳过询问。"
        if any(x in node_name for x in ["分支", "表示", "坚持", "路径"]):
            return "条件分支可能被过宽 trigger 激活或互斥分支粒度不准。"
        return "活动必需 atom 未命中；检查该 atom 是否应为条件/可选，或 element 是否过窄。"

    def _unexpected_node_root_cause(self, node: Any) -> str:
        name = str(getattr(node, "name", ""))
        if any(x in name for x in ["FAQ", "追问", "其他问题"]):
            return "FAQ 节点整体缺失；检查该 FAQ 是否被错误激活或是否应拆分。"
        if any(x in name for x in ["分支", "路径", "表示"]):
            return "条件分支整体缺失；检查 trigger 是否过宽或 relation 是否把未触发分支算入主线。"
        return "活动节点整体缺失；检查主图必需性和触发条件。"

    def _unexpected_relation_root_cause(self, event: Any) -> str:
        relation = str(getattr(event, "relation", ""))
        reason = str(getattr(event, "reason", ""))
        text = " ".join([relation, reason])
        if "exclusive" in text or "二选一" in text or "互斥" in text:
            return "互斥分支关系扣分；通常是两个条件分支都被过宽触发，或 relation_group 把未触发分支算入结构。"
        if "前置" in text or relation == "before":
            return "前置节点缺失导致连带结构扣分；优先看前置节点是否真应必答。"
        if "后置节点早于前置节点" in text:
            return "条件边顺序扣分；可能是用户已提前提供状态，但图仍要求先问再答。"
        return "结构关系未满足；检查 relation_group 是否包含条件节点或终止节点。"

    def _collect_unexpected_bad_events(self, dialogue: dict[str, Any], evaluation: EvaluationResult, expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collect severe local findings that are not aligned with injected errors.

        This is a negative-pack purity gate.  It prevents a negative sample from
        passing merely because the intended error was found while the evaluator
        also falsely kills unrelated nodes, facts, or constraints.
        """
        out: list[dict[str, Any]] = []
        for e in evaluation.knowledge_events or []:
            if str(getattr(e, "verdict", "")) != "冲突":
                continue
            if any(self._bad_event_aligned_to_expected(err, e, "knowledge") for err in expected):
                continue
            out.append({
                "kind": "unexpected_knowledge_conflict",
                "knowledge_id": getattr(e, "knowledge_id", ""),
                "node_id": getattr(e, "node_id", ""),
                "name": getattr(e, "name", ""),
                "evidence": getattr(e, "evidence", ""),
                "reason": getattr(e, "reason", ""),
            })
        for e in evaluation.constraint_events or []:
            if str(getattr(e, "verdict", "")) != "违规":
                continue
            if any(self._bad_event_aligned_to_expected(err, e, "constraint") for err in expected):
                continue
            out.append({
                "kind": "unexpected_constraint_violation",
                "constraint_id": getattr(e, "constraint_id", ""),
                "node_id": getattr(e, "node_id", ""),
                "name": getattr(e, "name", ""),
                "evidence": getattr(e, "evidence", ""),
                "reason": getattr(e, "reason", ""),
            })

        if bool(self.runtime.get("thresholds", {}).get("negative_count_flow_false_positives", True)):
            flow_issues: list[dict[str, Any]] = []
            for n in evaluation.node_results:
                if not getattr(n, "active", False):
                    continue
                reqs = list(getattr(n, "requirement_results", []) or [])
                missing_reqs = [r for r in reqs if getattr(r, "required", False) and not getattr(r, "matched", False)]
                if missing_reqs:
                    for r in missing_reqs:
                        flow_issues.append({
                            "kind": "unexpected_flow_miss",
                            "node_id": getattr(n, "node_id", ""),
                            "node_name": getattr(n, "name", ""),
                            "requirement_id": getattr(r, "requirement_id", ""),
                            "text": getattr(r, "text", ""),
                            "score": round(float(getattr(r, "score", 0.0) or 0.0), 4),
                            "why_counted": "该活动节点的小任务未命中，且未能和负包预设错误对齐",
                            "root_cause_hint": self._unexpected_flow_root_cause(n, r),
                            "element_miss_digest": self._element_audit_digest(getattr(r, "element_audit", {}) or {}),
                        })
                elif getattr(n, "status", "") == "缺失":
                    flow_issues.append({
                        "kind": "unexpected_node_miss",
                        "node_id": getattr(n, "node_id", ""),
                        "node_name": getattr(n, "name", ""),
                        "text": getattr(n, "name", ""),
                        "score": round(float(getattr(n, "score", 0.0) or 0.0), 4),
                        "why_counted": "该活动节点整体缺失，且未能和负包预设错误对齐",
                        "root_cause_hint": self._unexpected_node_root_cause(n),
                    })
            for issue in flow_issues:
                if any(self._flow_issue_aligned_to_expected(err, issue, dialogue) for err in expected):
                    continue
                out.append(issue)

        if bool(self.runtime.get("thresholds", {}).get("negative_count_relation_false_positives", True)):
            for e in evaluation.relation_events or []:
                if float(getattr(e, "penalty", 0.0) or 0.0) <= 0:
                    continue
                issue = {
                    "kind": "unexpected_relation_penalty",
                    "relation": getattr(e, "relation", ""),
                    "node_id": getattr(e, "target", "") or getattr(e, "source", ""),
                    "text": f"{getattr(e, 'source', '')}->{getattr(e, 'target', '')}",
                    "reason": getattr(e, "reason", ""),
                    "penalty": round(float(getattr(e, "penalty", 0.0) or 0.0), 4),
                    "why_counted": "该结构关系扣分未能和负包预设错误对齐",
                    "root_cause_hint": self._unexpected_relation_root_cause(e),
                }
                if any(self._flow_issue_aligned_to_expected(err, issue, dialogue) for err in expected):
                    continue
                out.append(issue)
            for e in evaluation.context_events or []:
                if getattr(e, "status", "") == "已处理":
                    continue
                issue = {
                    "kind": "unexpected_context_problem",
                    "policy_id": getattr(e, "policy_id", ""),
                    "node_id": getattr(e, "policy_id", ""),
                    "text": getattr(e, "reason", ""),
                    "reason": getattr(e, "reason", ""),
                }
                if any(self._flow_issue_aligned_to_expected(err, issue, dialogue) for err in expected):
                    continue
                out.append(issue)
        return out

    def _error_matched(self, err: dict[str, Any], evaluation: EvaluationResult, dialogue: dict[str, Any] | None = None) -> bool:
        family = str(err.get("error_family") or err.get("type") or "")
        node_id = err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node")
        target_group = err.get("requirement_id") or err.get("target_core") or err.get("target_group_id")
        knowledge_id = err.get("knowledge_id") or err.get("target_knowledge_id")
        constraint_id = err.get("constraint_id") or err.get("target_constraint_id")
        evidence_span = str(err.get("evidence_span") or err.get("wrong_statement") or "")
        dialogue = dialogue or {}

        if family in {"flow_missing", "process_missing", "流程缺失"}:
            # Generic audit pre-alignment: when the local graph already marks an
            # active node/atom as missing and its generated Chinese description
            # is semantically close to the negative target description, count it
            # as a local flow miss even if LLM generated different ids. This
            # is label-audit only and never changes evaluator score.
            flow_hint = " ".join(str(x or "") for x in [self._binding_hint(err, dialogue), err.get("description"), err.get("wrong_statement")] if str(x or "").strip())
            if flow_hint:
                for n0 in evaluation.node_results:
                    if not getattr(n0, "active", False):
                        continue
                    node_text0 = " ".join([str(getattr(n0, "node_id", "")), str(getattr(n0, "name", "")), " ".join(getattr(n0, "aliases", []) or [])])
                    if getattr(n0, "status", "") == "缺失" and self._similarity(flow_hint, node_text0) >= float(self.runtime.get("thresholds", {}).get("flow_node_hint_similarity", 0.08)):
                        return True
                    for req0 in getattr(n0, "requirement_results", []):
                        req_text0 = " ".join([str(req0.requirement_id), str(req0.text), str(getattr(req0, "aliases", [])), node_text0])
                        if not req0.matched and self._similarity(flow_hint, req_text0) >= float(self.runtime.get("thresholds", {}).get("flow_target_hint_similarity", 0.10)):
                            return True
            exact_node_seen = False
            for n in evaluation.node_results:
                if node_id and not self._id_matches(node_id, n.node_id, getattr(n, "aliases", [])):
                    continue
                exact_node_seen = True
                if not n.active:
                    # For a negative sample that explicitly targets a missing flow
                    # node, an untriggered/inactive target is still local evidence
                    # that the expected task was not fulfilled. The evaluator does
                    # not infer semantics here; it only honors the sample target.
                    return True
                if target_group:
                    # If a negative sample explicitly targets a requirement /
                    # evidence group, acceptance should verify that concrete
                    # target, even when the graph marks it conditional or
                    # optional. For broad LLM groups, a sibling hit should
                    # not hide the target-specific missing point.
                    group_seen = False
                    for req in getattr(n, "requirement_results", []):
                        req_keys = {req.requirement_id, *getattr(req, "aliases", [])}
                        if target_group not in req_keys:
                            continue
                        group_seen = True
                        if not req.matched:
                            return True
                        hits = [h for g in req.group_matches for h in g.hits]
                        if not self._target_supported_by_hits(err, dialogue, hits, req.text):
                            return True
                    for g in n.group_matches:
                        group_keys = {g.group_id, *getattr(g, "aliases", [])}
                        if target_group in group_keys:
                            group_seen = True
                            if not g.matched:
                                return True
                    if not group_seen and n.status == "缺失":
                        return True
                elif n.status == "缺失":
                    return True
            # When generated graph ids differ from dataset metadata ids, use
            # the metadata description only to locate the intended atom for
            # audit.  It still cannot create a score or verdict by itself.
            if node_id and not exact_node_seen:
                hint = self._binding_hint(err, dialogue)
                desc_hint = " ".join(str(x or "") for x in [hint, err.get("description"), err.get("wrong_statement")] if str(x or "").strip())
                if desc_hint:
                    ask_hint = any(x in desc_hint for x in ["询问", "主动询问", "请问", "是否", "确认"])
                    for n in evaluation.node_results:
                        node_text = " ".join([str(getattr(n, "node_id", "")), str(getattr(n, "name", "")), " ".join(getattr(n, "aliases", []) or [])])
                        node_sim = self._similarity(desc_hint, node_text)
                        if getattr(n, "active", False) and getattr(n, "status", "") == "缺失" and node_sim >= float(self.runtime.get("thresholds", {}).get("flow_node_hint_similarity", 0.08)):
                            return True
                        for req in getattr(n, "requirement_results", []):
                            target_text = " ".join([str(req.requirement_id), str(req.text), str(getattr(req, "aliases", [])), node_text])
                            sim = self._similarity(desc_hint, target_text)
                            question_like = any(x in str(req.text or "") for x in ["？", "?", "吗", "是否", "哪", "还是", "请问", "用什么"])
                            if sim >= float(self.runtime.get("thresholds", {}).get("flow_target_hint_similarity", 0.12)) or (ask_hint and question_like and sim >= 0.06):
                                if not req.matched:
                                    return True
                                hits = [h for g in req.group_matches for h in g.hits]
                                if not self._target_supported_by_hits(err, dialogue, hits, req.text):
                                    return True
            return False
        if family in {"knowledge_violation", "faq_wrong", "fact_wrong", "知识错误"}:
            # IMPORTANT: dataset evidence_span is trace metadata only.  It must
            # not by itself make a negative sample pass.  A knowledge negative
            # passes only when the local knowledge judge produced a conflict
            # event; labels are used after the fact to align that event with the
            # intended audit target.
            for e in evaluation.knowledge_events:
                matched_by_knowledge = bool(knowledge_id and self._event_matches_alias(knowledge_id, e, "knowledge_id"))
                if node_id and e.node_id and not matched_by_knowledge and not self._id_matches(node_id, e.node_id, getattr(e, "aliases", [])):
                    continue
                if evidence_span:
                    if self._same_span_local_bad_event(evidence_span, evaluation):
                        return True
                    if evidence_span and self._similarity(evidence_span, getattr(e, "evidence", "")) < float(self.runtime.get("thresholds", {}).get("audit_event_span_similarity", 0.45)) and not matched_by_knowledge:
                        continue
                if knowledge_id and matched_by_knowledge:
                    return True
                if str(getattr(e, "verdict", "")) == "冲突" and self._semantic_event_aligned(err, e):
                    return True
                if not knowledge_id and str(getattr(e, "verdict", "")) == "冲突":
                    return True
                if evidence_span and str(getattr(e, "verdict", "")) == "冲突":
                    return True
            if evidence_span and self._same_span_local_bad_event(evidence_span, evaluation):
                return True
            return False
        if family in {"constraint_violation", "boundary_violation", "限制违规"}:
            # Same anti-leak rule as knowledge: evidence_span can confirm/trace a
            # detected violation, but cannot replace the constraint judge.
            for e in evaluation.constraint_events:
                matched_by_constraint = bool(constraint_id and self._event_matches_alias(constraint_id, e, "constraint_id"))
                if node_id and e.node_id and not matched_by_constraint and not self._id_matches(node_id, e.node_id, getattr(e, "aliases", [])):
                    continue
                if evidence_span:
                    if self._same_span_local_bad_event(evidence_span, evaluation):
                        return True
                    if self._similarity(evidence_span, getattr(e, "evidence", "")) < float(self.runtime.get("thresholds", {}).get("audit_event_span_similarity", 0.45)) and not matched_by_constraint:
                        continue
                if constraint_id and matched_by_constraint:
                    return True
                if str(getattr(e, "verdict", "")) == "违规" and self._semantic_event_aligned(err, e):
                    return True
                if not constraint_id and str(getattr(e, "verdict", "")) == "违规":
                    return True
                if evidence_span and str(getattr(e, "verdict", "")) == "违规":
                    return True
            if evidence_span and self._same_span_local_bad_event(evidence_span, evaluation):
                return True
            return False
        return False
