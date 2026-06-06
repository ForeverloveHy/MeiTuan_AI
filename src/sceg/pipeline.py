from __future__ import annotations

from pathlib import Path
from typing import Any

from .dataset_interface import DatasetInterface
from .dialogue_loader import load_dialogues
from .evidence_extractor import EvidenceExtractor
from .graph_evaluator import GraphEvaluator
from .io_utils import read_json, write_json, write_text
from .oracle_router import OracleRouter
from .report_explainer import ReportExplainer
from .report_html import render_html
from .schema import StateGraph
from .schema_compiler import compile_state_graph
from .schema_linter import lint_and_repair_schema
from .score_adjuster import apply_dataset_score_adjustments
from .version import runtime_version_info


def run_pipeline(graph_path: str | Path, dialogue_root: str | Path, runtime_path: str | Path, out_path: str | Path, html_path: str | Path | None = None) -> list[dict[str, Any]]:
    raw_graph = read_json(graph_path)
    graph_data = compile_state_graph(raw_graph)
    graph_data, _ = lint_and_repair_schema(graph_data)
    graph = StateGraph.from_dict(graph_data)
    runtime = read_json(runtime_path)
    extractor = EvidenceExtractor()
    evaluator = GraphEvaluator(graph, runtime, extractor)
    accepter = DatasetInterface(runtime)
    explainer = ReportExplainer()
    oracle_router = OracleRouter(runtime)
    records: list[dict[str, Any]] = []
    for dialogue in load_dialogues(dialogue_root):
        evaluation = evaluator.evaluate(dialogue)
        acceptance = accepter.accept(dialogue, evaluation)
        apply_dataset_score_adjustments(dialogue, evaluation, acceptance, runtime)
        oracle_candidates = oracle_router.build_candidates(evaluation, acceptance)
        explanation = explainer.explain(evaluation, acceptance, oracle_candidates)
        records.append({
            "dialogue_id": evaluation.dialogue_id,
            "domain": dialogue.get("domain"),
            "sample_type": dialogue.get("sample_type"),
            "evaluation": evaluation.to_dict(),
            "acceptance": acceptance.to_dict(),
            "oracle_candidates": [x.to_dict() for x in oracle_candidates],
            "explanation": explanation,
            "runtime_version": runtime_version_info(),
        })
    write_json(out_path, records)
    if html_path:
        write_text(html_path, render_html(records, {"runtime_version": runtime_version_info()}))
    return records
