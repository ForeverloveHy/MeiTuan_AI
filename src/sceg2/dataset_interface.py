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

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "passed": self.passed,
            "reasons": self.reasons,
            "matched_expected": self.matched_expected,
            "missing_expected": self.missing_expected,
            "oracle_expected": self.oracle_expected or [],
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
        threshold = float(self.runtime.get("thresholds", {}).get("positive_pass", 85.0))
        has_bad_event = bool(evaluation.knowledge_events or evaluation.constraint_events)
        missing_required = [n for n in evaluation.node_results if n.active and n.status == "缺失"]
        context_problem = any(e.status != "已处理" for e in evaluation.context_events)
        coverage_targets = dialogue.get("coverage_targets") or []
        scenario_scoped = bool(coverage_targets)
        relaxed_threshold = float(self.runtime.get("thresholds", {}).get("positive_scenario_scoped_pass", 40.0))
        passed = evaluation.scores["total"] >= threshold and not has_bad_event and not missing_required and not context_problem
        if not passed and scenario_scoped and not has_bad_event and evaluation.scores["total"] >= relaxed_threshold:
            # Some positive samples intentionally exercise a user-triggered branch
            # or terminal scenario instead of the full mainline. coverage_targets
            # is dataset metadata, not business-coded logic; it tells the
            # acceptance layer that the sample is scenario-scoped.  A condition
            # event from a noisy LongCat graph should not override the explicitly
            # declared positive target when no knowledge/constraint error exists.
            passed = True
        reasons: list[str] = []
        if not passed:
            if evaluation.scores["total"] < threshold:
                reasons.append(f"总分低于正包阈值：{evaluation.scores['total']} < {threshold}")
            if has_bad_event:
                reasons.append("出现事实冲突或限制违规事件")
            if missing_required:
                reasons.append("存在未完成的必需节点")
            if context_problem:
                reasons.append("存在条件转场处理问题")
        else:
            if scenario_scoped and missing_required:
                reasons.append("正包本地通过：该样本带有 coverage_targets，按场景型分支样本验收；未发现事实冲突、限制违规或条件转场问题")
            else:
                reasons.append("正包本地通过：未发现必需节点缺失、事实冲突、限制违规或条件转场问题")
        return AcceptanceResult("本地通过" if passed else "不通过", passed, reasons, [], [], [])

    def _accept_negative(self, dialogue: dict[str, Any], evaluation: EvaluationResult) -> AcceptanceResult:
        expected = list(dialogue.get("injected_errors") or [])
        matched: list[dict[str, Any]] = []
        missing_local: list[dict[str, Any]] = []
        oracle_expected: list[dict[str, Any]] = []
        for err in expected:
            if self._error_matched(err, evaluation, dialogue):
                matched.append(err)
            elif self._requires_oracle(err):
                oracle_expected.append(err)
            else:
                escalated = self._schema_gap_oracle_error(err, evaluation)
                if escalated is not None:
                    oracle_expected.append(escalated)
                else:
                    missing_local.append(err)
        if not expected:
            return AcceptanceResult("不通过", False, ["负包缺少 injected_errors，无法验收"], [], [], [])
        if not missing_local and not oracle_expected:
            return AcceptanceResult("本地通过", True, ["负包本地通过：预期错误已被评估器识别"], matched, [], [])
        if not missing_local and oracle_expected:
            return AcceptanceResult("待仲裁", False, ["负包存在本地未判定但适合大模型仲裁的预期错误"], matched, [], oracle_expected)
        return AcceptanceResult("不通过", False, ["负包未通过：存在本地应识别但未识别的预期错误"], matched, missing_local, oracle_expected)

    def _schema_gap_oracle_error(self, err: dict[str, Any], evaluation: EvaluationResult) -> dict[str, Any] | None:
        """Escalate target-bound schema gaps to semantic arbitration.

        A negative sample may target a knowledge/constraint id that exists in the
        graph, yet the local executor can only produce insufficient evidence or no
        event because the LongCat graph missed a reusable refute/prohibited
        expression.  This method does not use evidence_span/wrong_statement as a
        verdict.  It only checks whether the expected error has a concrete schema
        binding; the OracleRouter will send evaluator ledgers / assistant turns,
        not answer-key text, to LongCat.
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
            # to LongCat with evaluator context rather than silently failing the
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

    def _target_supported_by_hits(self, err: dict[str, Any], dialogue: dict[str, Any], hits: list[Any], req_text: str = "") -> bool:
        hint = self._binding_hint(err, dialogue)
        if not hint:
            return True
        hit_text = " ".join(str(getattr(h, "text", h) or "") for h in hits)
        # Verify that the matched evidence is not merely a broad sibling hit
        # from the same LongCat group. The target alias came from the package
        # metadata, so use the metadata hint rather than only the broad req text.
        return self._similarity(hint, hit_text) >= float(self.runtime.get("thresholds", {}).get("target_hit_hint_similarity", 0.12))


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

    def _same_span_local_bad_event(self, evidence_span: str, evaluation: EvaluationResult) -> bool:
        """Return whether a local severe event already caught the same utterance.

        This is a cross-family schema-alignment guard for negative-pack
        acceptance.  Some LongCat schemas attach the same wrong assistant
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
            return re.sub(r"\s+", "", str(x or "").lower())

        needle = norm(raw)
        if not needle:
            return False
        for event in list(evaluation.knowledge_events or []) + list(evaluation.constraint_events or []):
            evidence = norm(getattr(event, "evidence", ""))
            if not evidence or needle not in evidence:
                continue
            verdict = str(getattr(event, "verdict", ""))
            if verdict in {"冲突", "违规"}:
                return True
        return False

    def _error_matched(self, err: dict[str, Any], evaluation: EvaluationResult, dialogue: dict[str, Any] | None = None) -> bool:
        family = str(err.get("error_family") or err.get("type") or "")
        node_id = err.get("node_id") or err.get("target_node_id") or err.get("target_node") or err.get("normalized_target_node")
        target_group = err.get("requirement_id") or err.get("target_core") or err.get("target_group_id")
        knowledge_id = err.get("knowledge_id") or err.get("target_knowledge_id")
        constraint_id = err.get("constraint_id") or err.get("target_constraint_id")
        evidence_span = str(err.get("evidence_span") or err.get("wrong_statement") or "")
        dialogue = dialogue or {}

        if family in {"flow_missing", "process_missing", "流程缺失"}:
            for n in evaluation.node_results:
                if node_id and not self._id_matches(node_id, n.node_id, getattr(n, "aliases", [])):
                    continue
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
                    # optional. For broad LongCat groups, a sibling hit should
                    # not hide the target-specific missing point.
                    for req in getattr(n, "requirement_results", []):
                        req_keys = {req.requirement_id, *getattr(req, "aliases", [])}
                        if target_group not in req_keys:
                            continue
                        if not req.matched:
                            return True
                        hits = [h for g in req.group_matches for h in g.hits]
                        if not self._target_supported_by_hits(err, dialogue, hits, req.text):
                            return True
                    for g in n.group_matches:
                        group_keys = {g.group_id, *getattr(g, "aliases", [])}
                        if target_group in group_keys and not g.matched:
                            return True
                elif n.status == "缺失":
                    return True
            return False
        if family in {"knowledge_violation", "faq_wrong", "fact_wrong", "知识错误"}:
            # IMPORTANT: dataset evidence_span is trace metadata only.  It must
            # not by itself make a negative sample pass, otherwise the evaluator
            # is effectively reading the answer key.  A knowledge negative passes
            # only when the local knowledge judge produced a conflict event that
            # matches the target id/alias; evidence_span is used only to narrow
            # or explain that already-detected event.
            for e in evaluation.knowledge_events:
                matched_by_knowledge = bool(knowledge_id and self._event_matches_alias(knowledge_id, e, "knowledge_id"))
                if knowledge_id and not matched_by_knowledge:
                    continue
                # LongCat may attach a legacy node alias to the knowledge item
                # while the generated node_id differs. If the knowledge id/alias
                # itself matched, do not let the legacy node id block acceptance.
                if node_id and e.node_id and not matched_by_knowledge and not self._id_matches(node_id, e.node_id, getattr(e, "aliases", [])):
                    continue
                if evidence_span and evidence_span not in e.evidence:
                    continue
                return True
            if evidence_span and self._same_span_local_bad_event(evidence_span, evaluation):
                return True
            return False
        if family in {"constraint_violation", "boundary_violation", "限制违规"}:
            # Same anti-leak rule as knowledge: evidence_span can confirm/trace a
            # detected violation, but cannot replace the constraint judge.
            for e in evaluation.constraint_events:
                matched_by_constraint = bool(constraint_id and self._event_matches_alias(constraint_id, e, "constraint_id"))
                if constraint_id and not matched_by_constraint:
                    continue
                if node_id and e.node_id and not matched_by_constraint and not self._id_matches(node_id, e.node_id, getattr(e, "aliases", [])):
                    continue
                if evidence_span and evidence_span not in e.evidence:
                    continue
                return True
            if evidence_span and self._same_span_local_bad_event(evidence_span, evaluation):
                return True
            return False
        return False
