from __future__ import annotations

from typing import Any

from .dataset_interface import AcceptanceResult
from .graph_evaluator import EvaluationResult


def apply_dataset_score_adjustments(
    dialogue: dict[str, Any],
    evaluation: EvaluationResult,
    acceptance: AcceptanceResult,
    runtime: dict[str, Any] | None = None,
) -> None:
    """Formal no-op kept for backward-compatible imports.

    Earlier development builds could optionally adjust scores by reading dataset
    answer-key metadata such as injected_errors, wrong_statement or
    evidence_span.  That path is intentionally removed.  In the current SCEG
    runtime, scores are produced only from graph + dialogue evidence; negative
    labels are used only by DatasetInterface after evaluation for audit reporting.

    The function mutates nothing and deliberately has no environment-variable
    escape hatch, so accidentally enabling legacy label-based score fixes is no
    longer possible.
    """
    return None
