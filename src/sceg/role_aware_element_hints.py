"""Minimal role-aware element derivation hints for SCEG atom transport.

The transport layer should not become a second prompt or a hidden business
knowledge base.  It only tells the element stages which side of the dialogue the
atom belongs to and which derivation route should be used.
"""
from __future__ import annotations

from typing import Any
import re


def _clean(text: Any, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def infer_derivation_mode(atom_source: str, requested_slots: list[str] | None = None) -> str:
    """Return a compact derivation mode.

    assistant_element_first means: split elements from the expected assistant
    response or expected assistant fact answer.
    user_text_variants_then_elementize means: do not rely on one trigger text;
    the secondary expansion stage should first generate possible user utterance
    texts, then elementize each utterance text into an OR trigger group.
    """
    source = str(atom_source or "")
    if source == "activation":
        return "user_text_variants_then_elementize"
    if source == "hard_constraint":
        return "assistant_violation_safe_contrast"
    return "assistant_element_first"


def build_role_aware_element_hints(atom_source: str, atom_text: Any, requested_slots: list[str] | None = None) -> dict[str, Any]:
    """Build tiny, domain-neutral derivation hints.

    No likely utterance list is generated here.  User utterance variation is
    intentionally left to the secondary expansion prompt because user wording is
    open-set and should be expanded as text before elementization.
    """
    mode = infer_derivation_mode(atom_source, requested_slots)
    if mode == "user_text_variants_then_elementize":
        return {
            "derivation_mode": mode,
            "speaker_side": "user_trigger",
            "primary_stage_rule": "only create a compact trigger seed group from the trigger meaning",
            "secondary_stage_rule": "generate many likely user utterance texts first, then elementize each text into one OR trigger_group",
            "atom_text_hint": _clean(atom_text),
        }
    if mode == "assistant_violation_safe_contrast":
        return {
            "derivation_mode": mode,
            "speaker_side": "assistant_response",
            "primary_stage_rule": "split negative_groups from likely violating assistant wording and safe_groups from likely safe assistant wording",
            "secondary_stage_rule": "expand existing assistant-side elements strictly by equivalent wording; do not create new facts",
            "atom_text_hint": _clean(atom_text),
        }
    return {
        "derivation_mode": mode,
        "speaker_side": "assistant_response",
        "primary_stage_rule": "derive elements from the most likely assistant response, not from an abstract task label",
        "secondary_stage_rule": "expand each existing assistant-side element.pool with strict equivalent wording",
        "atom_text_hint": _clean(atom_text),
    }
