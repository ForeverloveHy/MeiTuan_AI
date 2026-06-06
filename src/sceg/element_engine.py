from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .evidence_units import EvidenceUnit
from .normalizer import normalize_text

ROLE_TYPES = {"speaker_role", "discourse_role"}
NEGATION_WORDS = ("不", "不用", "不需要", "无需", "不必", "没有", "没", "不会", "不能", "无法", "没法", "不是", "并非", "不支持", "不包含", "不收取", "不产生", "不影响", "不生效", "不开放")
QUESTION_WORDS = ("吗", "呢", "么", "是否", "是不是", "有没有", "能不能", "可不可以", "方便", "对吧", "是吧", "？", "?")
GUARANTEE_WORDS = ("保证", "承诺", "确保", "一定", "肯定", "包", "绝对")

# Legacy semantic-map keys. New prompts should not need type/id fields, but we
# still support older maps by splitting each key into an independent element.
SEMANTIC_KEYS = {
    "speaker_role", "discourse_role", "intent", "target", "business_target", "attribute",
    "value", "quantity", "time", "date", "duration", "condition", "context_state",
    "polarity", "modality", "form", "relation", "action", "object", "subject", "unit",
}

WINDOW_FACTORS = {
    "same_atom": 1.00,
    "same_turn_adjacent_2": 0.92,
    "same_turn_adjacent_3": 0.82,
    # Node fulfillment may be naturally split across two consecutive assistant
    # turns.  This window is intentionally limited to node_positive so facts
    # and hard-constraint safety scopes do not leak across unrelated turns.
    "same_speaker_adjacent_turn_2": 0.96,
    "same_speaker_adjacent_turn_3": 0.90,
    "context_carry_over": 0.75,
}

THRESHOLD_PROFILES: dict[str, dict[str, float]] = {
    # Candidate recall is deliberately broad: only main=true elements and their
    # pool terms pull dialogue atoms into consideration.  After recall, the local
    # group score still combines main/non-main coverage.  These profiles are one
    # level looser than the previous strict setting; fact=true elements below
    # remain deterministic gates for knowledge and hard-constraint sides.
    "node_positive": {"hit_main": 0.50, "hit_group": 0.34, "review_main": 0.25, "review_group": 0.18},
    "node_trigger": {"hit_main": 0.50, "hit_group": 0.42, "review_main": 0.26, "review_group": 0.20},
    "knowledge_selector": {"hit_main": 0.50, "hit_group": 0.42, "review_main": 0.26, "review_group": 0.20},
    "knowledge_positive": {"hit_main": 0.52, "hit_group": 0.58, "review_main": 0.30, "review_group": 0.34},
    "knowledge_negative": {"hit_main": 0.52, "hit_group": 0.58, "review_main": 0.30, "review_group": 0.34},
    "knowledge_subject": {"hit_main": 0.50, "hit_group": 0.42, "review_main": 0.26, "review_group": 0.20},
    "constraint_trigger": {"hit_main": 0.50, "hit_group": 0.42, "review_main": 0.26, "review_group": 0.20},
    "constraint_negative": {"hit_main": 0.52, "hit_group": 0.58, "review_main": 0.30, "review_group": 0.34},
    "constraint_positive": {"hit_main": 0.52, "hit_group": 0.54, "review_main": 0.30, "review_group": 0.32},
    "soft_global": {"hit_main": 0.48, "hit_group": 0.30, "review_main": 0.24, "review_group": 0.18},
    "default": {"hit_main": 0.52, "hit_group": 0.46, "review_main": 0.28, "review_group": 0.22},
}


def compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("value", "text", "description", "label", "canonical", "v"):
            if isinstance(value.get(key), str) and value.get(key).strip():
                out.append(value.get(key).strip())
        for key in (
            "aliases", "surface_forms", "terms", "values", "any", "examples",
            "semantic_equivalents", "spoken_variants", "synonym_expressions",
            "phrase_patterns", "regex_templates", "secondary_pool", "secondary_pool_terms",
            "pool", "variants",
        ):
            out.extend(as_list(value.get(key)))
        return dedupe(out)
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(as_list(item))
        return dedupe(out)
    return []


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        key = compact(text)
        if text and key and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _drop_cn_modal_fillers(value: str) -> str:
    """Remove generic Chinese modal/filler bridges for surface matching.

    This is deliberately small and domain-agnostic.  It lets fact pools with
    short temporal values match natural utterances with modal bridges, while
    preserving negative markers such as “不/没/无/未”, so polarity facts still
    work as hard switches.
    """
    x = compact(value)
    for token in ("就会", "将会", "就能", "就可以", "可以", "能够", "会", "能", "将", "就", "了", "的"):
        x = x.replace(token, "")
    return x


def text_hit(term: str, text: str) -> bool:
    a = compact(term)
    b = compact(text)
    if not a or not b:
        return False
    if a in b:
        return True
    # Generic modal-bridge normalization, not task-specific.  Temporal/status
    # facts may be spoken with fillers, but negative forms must not be reduced
    # into positive forms.
    a2 = _drop_cn_modal_fillers(a)
    b2 = _drop_cn_modal_fillers(b)
    if a2 and b2 and a2 in b2:
        return True
    # Numeric/time/money/count terms are fact-sensitive.  They must be provided
    # as exact surface forms in value/pool, with only the modal bridge above
    # allowed; character-overlap fallback would turn “10天” into “10单”.
    if re.search(r"\d", a) or any(x in a for x in ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "元", "点", "天", "单", "秒", "分钟", "小时", "%", "％")):
        return False
    # Conservative fallback for longer Chinese strings; short elements must be
    # exact or explicitly supplied through pools to avoid false positives.
    if len(a) < 5:
        return False
    aset = set(a)
    common = len(aset & set(b))
    return common >= 4 and common / max(1, len(aset)) >= 0.90




def _is_precision_element(element: "SemanticElement") -> bool:
    """Return whether an element is a decisive local fact for node completion.

    Node atoms may be evaluated by softer semantic elements, but values such as
    exact numbers, times, dates, amounts and explicit status words must not be
    borrowed from a neighbouring FAQ/branch.  This guard is domain-neutral: it
    looks only at the element surface and its pool, not at rider/merchant words.
    """
    terms = [str(getattr(element, "value", "") or ""), *[str(x or "") for x in getattr(element, "secondary_pool", []) or []], *[str(x or "") for x in getattr(element, "aliases", []) or []]]
    joined = " ".join(terms)
    c = compact(joined)
    if not c:
        return False
    if re.search(r"\d", c):
        return True
    # Treat Chinese-number quantities as precise, but do not mark ordinary words
    # such as “天气/订单量/单量” as precision just because they contain 天/单.
    if re.search(r"[一二三四五六七八九十百千万两]+(?:点|时|秒|分钟|小时|天|日|单|元|块)", c):
        return True
    precise_tokens = (
        "今天", "今日", "当天", "当日", "明天", "次日", "第二天", "前一天", "昨日", "昨天",
        "上午", "下午", "晚上", "中午", "午餐", "晚餐", "高峰", "18点", "18:00",
        "分钟", "小时", "%", "％",
        "较低", "更低", "便宜", "略高", "稍高", "更高",
        "生效", "未生效", "已生效", "显示", "未显示", "已显示", "适用", "不适用",
    )
    return any(tok in c for tok in precise_tokens)


def _rule_has_precision_groups(rule: "ElementRule") -> bool:
    return any(_is_precision_element(e) for g in getattr(rule, "element_groups", []) or [] for e in getattr(g, "elements", []) or [])


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


_TRIGGER_GENERIC_PARTICIPANTS = {
    "我", "你", "您", "他", "她", "对方", "用户", "客户", "老板", "负责人", "客服",
}


def _trigger_key(value: str) -> str:
    return compact(value)


def _is_generic_trigger_participant(element: "SemanticElement") -> bool:
    value = compact(getattr(element, "value", ""))
    if value in {_trigger_key(x) for x in _TRIGGER_GENERIC_PARTICIPANTS}:
        return True
    # If a schema element can be replaced by first/second-person pronouns, it is
    # a dialogue participant anchor.  It should support a trigger group but must
    # not trigger a branch by itself.
    pools = [compact(x) for x in getattr(element, "secondary_pool", []) or []]
    return len(value) <= 4 and any(x in pools for x in ("我", "你", "您", "对方"))


def _trigger_main_clusters(elements: list["SemanticElement"]) -> list[list["SemanticElement"]]:
    """Cluster redundant trigger mains and drop pure participant anchors.

    User-trigger groups are OR groups, but inside one group the discriminative
    trigger state should be complete.  Broad participant anchors must not trigger
    a branch by themselves, and nested pairs such as “X说忙 + 忙” or “在开车 + 开车”
    represent one state, not two independent requirements.
    """
    mains = [e for e in elements if getattr(e, "required", False) and e.type not in ROLE_TYPES]
    discriminative = [e for e in mains if not _is_generic_trigger_participant(e)]
    if not discriminative:
        discriminative = mains
    clusters: list[list[SemanticElement]] = []
    used: set[int] = set()
    for i, e in enumerate(discriminative):
        if i in used:
            continue
        ek = compact(e.value)
        cluster = [e]
        used.add(i)
        for j, other in enumerate(discriminative):
            if j in used:
                continue
            ok = compact(other.value)
            if ek and ok and (ek in ok or ok in ek):
                cluster.append(other)
                used.add(j)
        clusters.append(cluster)
    return clusters


def _trigger_main_cluster_missing(rule_type: str, main_elements: list["SemanticElement"], hit_elements: list["SemanticElement"]) -> list["SemanticElement"]:
    if rule_type != "node_trigger" or not main_elements:
        return []
    missing: list[SemanticElement] = []
    for cluster in _trigger_main_clusters(main_elements):
        if not any(e in hit_elements for e in cluster):
            missing.extend(cluster[:1])
    return missing


@dataclass(slots=True)
class SemanticElement:
    # New prompts may omit type/id. Type is kept only as an internal compatibility
    # channel; matching is primarily value/pool based.
    type: str
    value: str
    aliases: list[str] = field(default_factory=list)
    weight: float = 1.0
    confidence: float = 1.0
    source: str = "schema"
    span: str = ""
    element_id: str = ""
    required: bool = False  # internal alias for main=true; not a prompt field
    fact: bool = False  # fact=true marks polarity/quantity/time/value gates
    secondary_pool: list[str] = field(default_factory=list)

    @classmethod
    def from_any(cls, raw: Any) -> "SemanticElement | None":
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            if "=" in text:
                t, v = text.split("=", 1)
                return cls(t.strip() or "surface", v.strip(), source="schema")
            return cls("surface", text, aliases=[text], source="schema")
        if not isinstance(raw, dict):
            return None
        typ = str(raw.get("type") or raw.get("element_type") or "surface").strip()
        val = str(raw.get("value") or raw.get("v") or raw.get("name") or raw.get("text") or raw.get("description") or raw.get("canonical") or "").strip()
        aliases = as_list(raw.get("aliases") or raw.get("surface_forms") or raw.get("terms") or raw.get("semantic_equivalents"))
        secondary = as_list(raw.get("secondary_pool") or raw.get("secondary_pool_terms") or raw.get("secondary_terms") or raw.get("variants") or raw.get("pool"))
        if not val and aliases:
            val = aliases[0]
        if not typ or not val:
            return None
        return cls(
            typ,
            val,
            aliases=dedupe(aliases),
            # LLM-provided weights are accepted for legacy graphs but ignored by
            # the grouped matcher, which recomputes schema-local weights.
            weight=_safe_float(raw.get("weight", 1.0), 1.0),
            confidence=_safe_float(raw.get("confidence", 1.0), 1.0),
            source=str(raw.get("source") or "schema"),
            span=str(raw.get("span") or ""),
            element_id=str(raw.get("element_id") or raw.get("id") or ""),
            required=bool(raw.get("main") or raw.get("is_main") or raw.get("required") or raw.get("is_required")),
            fact=bool(raw.get("fact") or raw.get("is_fact") or raw.get("fact_gate") or raw.get("decisive")),
            secondary_pool=dedupe(secondary),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.value)

    @property
    def can_recall_alone(self) -> bool:
        return self.type not in ROLE_TYPES


@dataclass(slots=True)
class ElementGroup:
    group_id: str
    name: str = ""
    role: str = "main"  # internal compatibility only; prompts use element main=true/false
    elements: list[SemanticElement] = field(default_factory=list)
    required: bool = False
    threshold: float = 0.0  # ignored by new local profiles; kept for JSON compatibility
    review_threshold: float = 0.0
    min_element_hits: int = 0
    require_all_main: bool = False
    locality: str = "same_atom_or_adjacent_in_same_turn"
    weight: float = 1.0
    description: str = ""


@dataclass(slots=True)
class EvidenceWindow:
    kind: str
    atoms: list["DialogueAtom"]
    factor: float


@dataclass(slots=True)
class GroupEval:
    group: ElementGroup
    verdict: str
    score: float
    hit_elements: list[SemanticElement] = field(default_factory=list)
    missing_elements: list[SemanticElement] = field(default_factory=list)
    atoms: list["DialogueAtom"] = field(default_factory=list)
    reason: str = ""
    main_coverage: float = 0.0
    group_coverage: float = 0.0
    window_kind: str = "same_atom"


@dataclass(slots=True)
class DialogueAtom:
    atom_id: str
    turn_index: int
    speaker: str
    text: str
    normalized: str
    span_type: str = "atom"
    elements: list[SemanticElement] = field(default_factory=list)

    def has_element(self, element: SemanticElement, secondary: dict[str, Any] | None = None) -> bool:
        return element_hit(element, self, secondary or {})


@dataclass(slots=True)
class ElementRule:
    rule_id: str
    rule_type: str
    primary_elements: list[SemanticElement] = field(default_factory=list)
    secondary_pools: dict[str, Any] = field(default_factory=dict)
    zero_level_elements: list[SemanticElement] = field(default_factory=list)
    positive_elements: list[SemanticElement] = field(default_factory=list)
    negative_elements: list[SemanticElement] = field(default_factory=list)
    match_policy: dict[str, Any] = field(default_factory=dict)
    element_groups: list[ElementGroup] = field(default_factory=list)


@dataclass(slots=True)
class ElementMatch:
    verdict: str  # hit / miss / review
    score: float = 0.0
    atom: DialogueAtom | None = None
    primary_hits: list[SemanticElement] = field(default_factory=list)
    secondary_hits: list[str] = field(default_factory=list)
    dangerous_hits: list[SemanticElement] = field(default_factory=list)
    missing: list[SemanticElement] = field(default_factory=list)
    reason: str = ""
    group_evals: list[GroupEval] = field(default_factory=list)
    candidate_atoms: list[DialogueAtom] = field(default_factory=list)
    candidate_results: list[dict[str, Any]] = field(default_factory=list)


class ElementEngine:
    """Atom/element executor with local windows and schema-local IDF.

    LLM output contract is intentionally minimal: atom -> element_groups ->
    elements(value, main, pool).  The executor, not the LLM, owns candidate
    selection direction, proximity windows, weights and thresholds.
    """

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._schema_atom_count: int = 0

    def configure_schema(self, graph: Any) -> None:
        """Compute schema-local IDF from all schema atoms/rules.

        Each atom/rule is treated as a small document. Elements that occur in
        many schema atoms become less decisive; rare values such as amounts,
        dates and product-specific terms become more decisive automatically.
        """
        docs: list[set[str]] = []

        def add_doc_from_groups(groups: Any) -> None:
            parsed = parse_element_groups(groups)
            terms: set[str] = set()
            for group in parsed:
                for e in group.elements:
                    for t in [e.value, *e.aliases, *e.secondary_pool]:
                        k = compact(t)
                        if k:
                            terms.add(k)
            if terms:
                docs.append(terms)

        for node in getattr(graph, "nodes", []) or []:
            for req in getattr(node, "requirements", []) or []:
                add_doc_from_groups(getattr(req, "element_groups", []) or [])
                if not getattr(req, "element_groups", None):
                    elems = parse_elements(getattr(req, "primary_elements", []) or [])
                    if elems:
                        add_doc_from_groups([{"elements": [_element_to_raw(e) for e in elems]}])
        for item in getattr(graph, "knowledge", []) or []:
            for attr in ("selector_element_groups", "correct_element_groups", "wrong_element_groups", "positive_element_groups", "negative_element_groups", "element_groups"):
                add_doc_from_groups(getattr(item, attr, []) or [])
        for rule in getattr(graph, "constraints", []) or []:
            for attr in ("trigger_element_groups", "negative_element_groups", "positive_element_groups", "element_groups"):
                add_doc_from_groups(getattr(rule, attr, []) or [])

        self._schema_atom_count = len(docs)
        df: dict[str, int] = {}
        for doc in docs:
            for term in doc:
                df[term] = df.get(term, 0) + 1
        n = max(1, len(docs))
        self._idf = {term: math.log((n + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}

    def build_atoms(self, units: list[EvidenceUnit]) -> list[DialogueAtom]:
        atoms: list[DialogueAtom] = []
        for unit in units:
            spans = self._spans(unit.text)
            seen: set[str] = set()
            for idx, span in enumerate(spans):
                key = compact(span)
                if not key or key in seen:
                    continue
                seen.add(key)
                # idx=0 is the full turn. If the turn has no punctuation, it is
                # still a valid atom; windows prefer sub-atoms when available.
                atom = DialogueAtom(
                    atom_id=f"d{unit.turn_index}_{idx}",
                    turn_index=unit.turn_index,
                    speaker=unit.speaker,
                    text=span,
                    normalized=normalize_text(span),
                    span_type="turn" if idx == 0 else "atom",
                )
                atom.elements = self._generic_elements(atom)
                atoms.append(atom)
        return atoms

    def make_rule(
        self,
        rule_id: str,
        rule_type: str,
        *,
        primary: Any = None,
        secondary: Any = None,
        zero: Any = None,
        positive: Any = None,
        negative: Any = None,
        policy: dict[str, Any] | None = None,
        element_groups: Any = None,
    ) -> ElementRule:
        return ElementRule(
            rule_id=rule_id,
            rule_type=rule_type,
            primary_elements=parse_elements(primary),
            secondary_pools=dict(secondary or {}),
            zero_level_elements=[],
            positive_elements=parse_elements(positive),
            negative_elements=parse_elements(negative),
            match_policy=dict(policy or {}),
            element_groups=parse_element_groups(element_groups, default_role=_default_group_role(rule_type)),
        )

    def match_rule(self, rule: ElementRule, atoms: list[DialogueAtom]) -> ElementMatch:
        if rule.element_groups:
            return self._match_grouped_rule(rule, atoms)
        candidates: list[ElementMatch] = []
        for atom in atoms:
            m = self.match_atom(rule, atom)
            if m.verdict != "miss":
                candidates.append(m)
        if not candidates:
            return ElementMatch("miss", reason="一级元素未召回候选")
        candidates.sort(key=_match_rank, reverse=True)
        return candidates[0]

    def match_atom(self, rule: ElementRule, atom: DialogueAtom) -> ElementMatch:
        # Legacy flat-element path. Grouped rules use the scientific local-IDF path.
        primary = rule.primary_elements
        secondary = rule.secondary_pools
        primary_hits = [e for e in primary if element_hit(e, atom, secondary)]
        non_role_hits = [e for e in primary_hits if e.type not in ROLE_TYPES]
        if primary and not non_role_hits:
            return ElementMatch("miss", atom=atom, missing=[e for e in primary if e not in primary_hits], reason="没有命中非角色一级元素")
        total_weight = sum(max(0.1, e.weight) for e in primary) or 1.0
        hit_weight = sum(max(0.1, e.weight) for e in primary_hits)
        score = hit_weight / total_weight
        secondary_hits = secondary_surface_hits(rule, atom)
        if secondary_hits:
            score = min(1.0, score + min(0.25, 0.05 * len(secondary_hits)))
        profile = _threshold_profile(rule.rule_type)
        hit_threshold = profile["hit_group"]
        review_threshold = profile["review_group"]
        missing = [e for e in primary if e not in primary_hits]
        if score >= hit_threshold and (not primary or non_role_hits):
            return ElementMatch("hit", score, atom, primary_hits, secondary_hits, [], missing, "扁平元素综合命中")
        if score >= review_threshold and non_role_hits:
            return ElementMatch("review", score, atom, primary_hits, secondary_hits, [], missing, "扁平元素部分命中，处于仲裁层")
        return ElementMatch("miss", score, atom, primary_hits, secondary_hits, [], missing, "未达到召回/精判阈值")

    def _match_grouped_rule(self, rule: ElementRule, atoms: list[DialogueAtom]) -> ElementMatch:
        candidates = self._recall_candidates(rule, atoms)
        if not candidates:
            return ElementMatch("miss", reason="元素组 main+pool 召回未命中候选 atom")
        best: ElementMatch | None = None
        audits: list[dict[str, Any]] = []
        for anchor in candidates:
            for window in self.candidate_windows(rule, anchor, atoms):
                group_evals = [self._eval_group(rule, group, window) for group in rule.element_groups]
                match = self._settle_group_evals(rule, anchor, window, group_evals)
                audits.append(_candidate_audit(anchor, window, match, group_evals))
                if best is None or _match_rank(match) > _match_rank(best):
                    best = match
        if best is None:
            return ElementMatch("miss", reason="元素组没有可结算候选")
        audits.sort(key=lambda x: ({"hit": 3, "review": 2, "miss": 1}.get(str(x.get("verdict")), 0), float(x.get("score") or 0.0)), reverse=True)
        best.candidate_results = audits
        return best

    def _recall_candidates(self, rule: ElementRule, atoms: list[DialogueAtom]) -> list[DialogueAtom]:
        elements = [e for g in rule.element_groups for e in g.elements if e.type not in ROLE_TYPES]
        main_elements = [e for e in elements if e.required]
        # Current project decision: candidate recall uses only main=true elements
        # and every term in their pool.  Non-main and fact-only elements never
        # recall candidates by themselves; they only settle the recalled atom.
        if not main_elements:
            main_elements = elements
        candidates: list[DialogueAtom] = []
        evidence_atoms = self._evidence_atoms(atoms)
        for atom in evidence_atoms:
            if any(element_hit(e, atom, rule.secondary_pools) for e in main_elements):
                candidates.append(atom)
        return candidates

    def _evidence_atoms(self, atoms: list[DialogueAtom]) -> list[DialogueAtom]:
        out: list[DialogueAtom] = []
        by_turn: dict[int, list[DialogueAtom]] = {}
        for atom in atoms:
            by_turn.setdefault(atom.turn_index, []).append(atom)
        for turn_atoms in by_turn.values():
            sub = [a for a in turn_atoms if a.span_type != "turn"]
            out.extend(sub or turn_atoms)
        return out

    def candidate_windows(self, rule: ElementRule, anchor: DialogueAtom, atoms: list[DialogueAtom], *, context: bool = False) -> list[EvidenceWindow]:
        if context:
            return [EvidenceWindow("context_carry_over", [anchor], WINDOW_FACTORS["context_carry_over"])]
        same_turn_all = [a for a in atoms if a.turn_index == anchor.turn_index]
        same_turn = [a for a in same_turn_all if a.span_type != "turn"] or same_turn_all
        seq_atoms = sorted(same_turn, key=_atom_seq)
        idx = seq_atoms.index(anchor) if anchor in seq_atoms else 0
        windows: list[EvidenceWindow] = [EvidenceWindow("same_atom", [anchor], WINDOW_FACTORS["same_atom"])]
        # Hard constraints are negative-object checks.  A forbidden modality word
        # in one comma-clause must not bind to a result object in a different
        # clause, otherwise a denial clause plus a rule object in another clause becomes a false violation.
        if rule.rule_type.startswith("constraint_"):
            return windows
        for radius, kind in ((1, "same_turn_adjacent_2"), (2, "same_turn_adjacent_3")):
            lo = max(0, idx - radius)
            hi = min(len(seq_atoms), idx + radius + 1)
            chunk = seq_atoms[lo:hi]
            if len(chunk) > 1:
                windows.append(EvidenceWindow(kind, chunk, WINDOW_FACTORS[kind]))

        # Node matching is a completion detector, not a fact verifier.  A single
        # node atom may be expressed across adjacent assistant turns, especially
        # in Chinese telephone style: "前一天18点前在App取消" and the next
        # sentence "取消后次日生效" jointly complete one FAQ atom.  Knowledge
        # and hard constraints still do not use this cross-turn window because
        # their fact/safety scopes are stricter.
        if rule.rule_type == "node_positive":
            local_speaker_atoms = [
                a for a in atoms
                if a.speaker == anchor.speaker and a.span_type != "turn"
                and abs(a.turn_index - anchor.turn_index) <= 2
            ]
            # Precision groups may also be split across two adjacent assistant
            # turns; allow radius=1 for them, but avoid a wider radius that
            # could borrow facts from another FAQ answer.  Non-precision node
            # atoms retain the wider radius=2 completion window.
            radius_plan = ((1, "same_speaker_adjacent_turn_2"), (2, "same_speaker_adjacent_turn_3"))
            for radius, kind in radius_plan:
                chunk = [a for a in local_speaker_atoms if abs(a.turn_index - anchor.turn_index) <= radius]
                if len(chunk) > 1:
                    chunk = sorted(chunk, key=lambda a: (a.turn_index, _atom_seq(a)))
                    windows.append(EvidenceWindow(kind, chunk, WINDOW_FACTORS[kind]))
        return windows

    def context_carry_window(self, anchor: DialogueAtom, atoms: list[DialogueAtom], *, max_turn_gap: int = 2, speaker: str | None = "assistant") -> list[DialogueAtom]:
        lo = max(0, anchor.turn_index - max_turn_gap)
        hi = anchor.turn_index
        return [a for a in atoms if lo <= a.turn_index <= hi and (speaker is None or a.speaker == speaker)]

    def _eval_group(self, rule: ElementRule, group: ElementGroup, window: EvidenceWindow) -> GroupEval:
        hit_elements: list[SemanticElement] = []
        missing: list[SemanticElement] = []
        hit_atoms: list[DialogueAtom] = []
        window_text = " ".join(a.text for a in window.atoms)
        allow_ordered_window = rule.rule_type == "node_positive" and len(window.atoms) > 1
        for element in group.elements:
            matched_atom = next((a for a in window.atoms if element_hit(element, a, rule.secondary_pools)), None)
            if matched_atom is None and allow_ordered_window and element_surface_hit(element, window_text, rule.secondary_pools, allow_ordered_window=True):
                matched_atom = window.atoms[0]
            if matched_atom is not None:
                hit_elements.append(element)
                if matched_atom not in hit_atoms:
                    hit_atoms.append(matched_atom)
            else:
                missing.append(element)
        non_role_hits = [e for e in hit_elements if e.type not in ROLE_TYPES]
        main_elements = [e for e in group.elements if e.required and e.type not in ROLE_TYPES]
        if not main_elements:
            main_elements = [e for e in group.elements if e.type not in ROLE_TYPES] or list(group.elements)
        main_hits = [e for e in hit_elements if e in main_elements]
        total_weight = sum(self._element_weight(e) for e in group.elements) or 1.0
        hit_weight = sum(self._element_weight(e) for e in hit_elements)
        main_total = sum(self._element_weight(e) for e in main_elements) or 1.0
        main_hit_weight = sum(self._element_weight(e) for e in main_hits)
        raw_group_cov = hit_weight / total_weight
        raw_main_cov = main_hit_weight / main_total
        # main=true marks core words, but it should not mean every core word must
        # appear verbatim. If at least one core word is hit, blend weighted core
        # coverage with core-hit count coverage; group_cov then checks whether
        # enough non-core/context elements also support the same local window.
        if main_elements and main_hits:
            count_cov = len(main_hits) / max(1, len(main_elements))
            raw_main_cov = max(raw_main_cov, 0.50 + 0.50 * count_cov)
        group_cov = min(1.0, raw_group_cov * window.factor)
        main_cov = min(1.0, raw_main_cov * window.factor)
        # If all required and supporting elements are present inside one
        # assistant turn, do not punish only because the evidence extractor split
        # the original sentence at punctuation/comma boundaries.  Cross-turn
        # carry-over still keeps its lower factor.
        if window.kind.startswith("same_turn_adjacent") and raw_group_cov >= 0.999 and raw_main_cov >= 0.999:
            group_cov = 1.0
            main_cov = 1.0
        # Score emphasizes main elements while preserving auxiliary coverage.
        score = 0.70 * main_cov + 0.30 * group_cov
        profile = _threshold_profile(rule.rule_type)
        fact_elements = [e for e in group.elements if getattr(e, "fact", False) and e.type not in ROLE_TYPES]
        missing_facts = [e for e in fact_elements if e not in hit_elements]
        precision_elements = [
            e for e in group.elements
            if e.type not in ROLE_TYPES and (getattr(e, "required", False) or getattr(e, "fact", False)) and _is_precision_element(e)
        ]
        missing_precision = [e for e in precision_elements if e not in hit_elements]
        fact_gate_types = ("knowledge_", "constraint_")
        fact_gate_applies = bool(fact_elements) and rule.rule_type.startswith(fact_gate_types)
        node_precision_gate_applies = rule.rule_type == "node_positive" and bool(precision_elements)
        # fact must be bound to its main trunk.  A value such as “10单” cannot be
        # used by itself to satisfy/refute a different knowledge atom; the local
        # window must also hit at least one non-fact main anchor in the same group.
        non_fact_main = [e for e in group.elements if e.required and not getattr(e, "fact", False) and e.type not in ROLE_TYPES]
        non_fact_main_hits = [e for e in hit_elements if e in non_fact_main]
        missing_fact_trunk_elements = [e for e in non_fact_main if e not in non_fact_main_hits]
        missing_fact_trunk = bool(fact_gate_applies and missing_fact_trunk_elements)
        if node_precision_gate_applies and missing_precision:
            verdict = "miss"
            miss_text = "、".join(e.value for e in missing_precision[:4])
            reason = f"节点精确事实元素未命中，不能用邻近FAQ/分支泛化补足；缺失={miss_text}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
            score = min(score, profile["review_group"] * 0.65)
        elif fact_gate_applies and missing_fact_trunk:
            verdict = "miss"
            trunk_text = "、".join(e.value for e in missing_fact_trunk_elements[:4])
            reason = f"fact 主干未完整命中，禁止相似知识串用；缺失主干={trunk_text}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
            score = min(score, profile["review_group"] * 0.7)
        elif fact_gate_applies and missing_facts:
            verdict = "miss"
            miss_text = "、".join(e.value for e in missing_facts[:4])
            reason = f"fact 元素未命中，按开关/否决规则不成立；缺失={miss_text}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
            score = min(score, profile["review_group"] * 0.8)
        elif rule.rule_type == "node_trigger" and _trigger_main_cluster_missing(rule.rule_type, main_elements, hit_elements):
            trigger_missing = _trigger_main_cluster_missing(rule.rule_type, main_elements, hit_elements)
            verdict = "miss"
            miss_text = "、".join(e.value for e in trigger_missing[:4])
            reason = f"用户触发组未完整命中判别主干；缺失={miss_text}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
            score = min(score, profile["review_group"] * 0.65)
        elif getattr(group, "require_all_main", False) and main_elements and len(main_hits) < len(main_elements):
            verdict = "miss"
            miss_text = "、".join(e.value for e in main_elements if e not in main_hits)
            reason = f"该触发/精判组要求全部 main 主干同时命中；缺失={miss_text}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
            score = min(score, profile["review_group"] * 0.75)
        elif fact_gate_applies and main_cov >= profile["hit_main"] and non_role_hits:
            # For knowledge/canonical hard-constraint sides, fact=true elements
            # are deterministic gates.  Once all fact gates in the local window
            # are satisfied, auxiliary non-main context must not demote the side
            # to review; quantity/time/polarity facts have already exercised
            # their veto above.
            verdict = "hit"
            reason = f"fact 开关/否决项已通过，元素组命中；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
        elif main_cov >= profile["hit_main"] and group_cov >= profile["hit_group"] and non_role_hits:
            verdict = "hit"
            fact_note = "；fact已通过" if fact_gate_applies else ""
            reason = f"元素组命中{fact_note}；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
        elif main_cov >= profile["review_main"] and group_cov >= profile["review_group"] and non_role_hits:
            verdict = "review"
            reason = f"元素组处于仲裁层；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
        else:
            verdict = "miss"
            reason = f"元素组未达阈值；窗口={window.kind}；main={main_cov:.2f}, group={group_cov:.2f}"
        return GroupEval(group, verdict, score, hit_elements, missing, hit_atoms, reason, main_cov, group_cov, window.kind)

    def _settle_group_evals(self, rule: ElementRule, anchor_atom: DialogueAtom, window: EvidenceWindow, group_evals: list[GroupEval]) -> ElementMatch:
        decisive = _decisive_groups(rule.rule_type, group_evals)
        if not decisive:
            decisive = group_evals
        decisive_hits = [g for g in decisive if g.verdict == "hit"]
        decisive_reviews = [g for g in decisive if g.verdict == "review"]
        require_all = _requires_all_decisive_groups(rule.rule_type)
        decisive_ok = (len(decisive_hits) == len(decisive)) if require_all else bool(decisive_hits)
        decisive_review = (bool(decisive_reviews) or bool(decisive_hits)) if not require_all else (len(decisive_hits) + len(decisive_reviews) == len(decisive))

        # Node atom score: main groups dominate, supporting groups contribute.
        # Multiple decisive groups are alternatives unless a future policy opts
        # into require_all.  Therefore a matched alternative must not be averaged
        # down by other unmentioned alternatives; this is essential for safe/
        # wrong/correct groups and for natural multi-expression nodes.
        supporting = [g for g in group_evals if g not in decisive]
        if require_all:
            decisive_score = sum(g.score for g in decisive) / max(1, len(decisive))
        elif decisive_hits:
            decisive_score = max(g.score for g in decisive_hits)
        elif decisive_reviews:
            decisive_score = max(g.score for g in decisive_reviews)
        else:
            decisive_score = max((g.score for g in decisive), default=0.0)
        if supporting:
            support_score = sum(g.score for g in supporting) / max(1, len(supporting))
            score = 0.80 * decisive_score + 0.20 * support_score
        else:
            score = decisive_score
        profile = _threshold_profile(rule.rule_type)
        primary_hits = [e for g in group_evals for e in g.hit_elements]
        missing = [e for g in group_evals for e in g.missing_elements]
        secondary_hits = dedupe([term for g in group_evals for e in g.hit_elements for term in e.secondary_pool if any(text_hit(term, a.text) for a in window.atoms)])
        best_atom = next((a for g in group_evals for a in g.atoms if a.span_type != "turn"), anchor_atom)
        if decisive_ok and score >= profile["hit_group"]:
            return ElementMatch("hit", score, best_atom, primary_hits, secondary_hits, [], missing, "主元素组命中，局部窗口与局部IDF加权分达标", group_evals, window.atoms)
        if decisive_review or score >= profile["review_group"]:
            return ElementMatch("review", score, best_atom, primary_hits, secondary_hits, [], missing, "命中仲裁层：存在局部证据但未达到本地命中阈值", group_evals, window.atoms)
        return ElementMatch("miss", score, best_atom, primary_hits, secondary_hits, [], missing, "主元素组未形成有效命中", group_evals, window.atoms)

    def _element_weight(self, element: SemanticElement) -> float:
        main_factor = 1.5 if element.required else 1.0
        if getattr(element, "fact", False):
            main_factor *= 1.25
        term_keys = [compact(element.value), *[compact(x) for x in element.secondary_pool], *[compact(x) for x in element.aliases]]
        idfs = [self._idf.get(k) for k in term_keys if k and k in self._idf]
        idf = max(idfs) if idfs else 1.0
        special = _special_factor(element)
        weight = main_factor * idf * special
        return max(0.2, min(4.0, weight))

    def _spans(self, text: str) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        spans = [raw]
        parts = re.split(r"[。！？!?；;\n]+", raw)
        for part in parts:
            part = part.strip(" ，,、：:")
            if not part:
                continue
            subs = [sub.strip(" ，,、：:") for sub in re.split(r"[，,、]|然后|另外|还有|同时|接下来|那我|所以", part)]
            subs = [sub for sub in subs if len(compact(sub)) >= 2]
            if len(subs) <= 1:
                spans.append(part)
            else:
                spans.extend(subs)
        return dedupe(spans)

    def _generic_elements(self, atom: DialogueAtom) -> list[SemanticElement]:
        text = compact(atom.text)
        raw = atom.text
        out: list[SemanticElement] = [SemanticElement("speaker_role", atom.speaker, source="local", span=raw, weight=0.2)]

        def add(t: str, v: str, w: float = 1.0, span: str = "") -> None:
            el = SemanticElement(t, v, source="local", span=span or raw, weight=w)
            if el.key not in {(x.type, x.value) for x in out}:
                out.append(el)

        if any(q in raw for q in QUESTION_WORDS):
            add("form", "question", 0.7)
            add("intent", "ask", 0.5)
        if any(x in raw for x in ("您好", "你好", "早上好", "下午好")):
            add("intent", "greet", 0.7)
        if any(x in text for x in ("确认", "核实", "请问", "是不是", "对吧", "是吧")):
            add("intent", "confirm", 1.0)
        if any(x in text for x in ("本人", "接听", "负责人", "负责", "当前接听")):
            add("target", "identity", 1.0)
        if any(x in text for x in ("方便", "有空", "没时间", "忙", "打扰", "时间")):
            add("target", "availability", 0.8)
        if any(x in text for x in ("不方便", "没时间", "忙", "等会", "稍后")):
            add("context_state", "unavailable", 1.0)
        if any(x in text for x in ("开车", "骑车", "开着车", "路上", "安全")):
            add("context_state", "driving", 1.0)
            add("target", "safety", 1.0)
        if any(x in text for x in ("不是负责人", "不负责", "找负责人", "负责人不在")):
            add("context_state", "not_responsible", 1.0)
        if any(x in text for x in ("不想", "算了", "不做", "拒绝", "不要")):
            add("context_state", "refusing", 0.8)
        if any(x in text for x in ("不懂", "没懂", "什么意思", "为什么", "凭什么", "质疑")):
            add("context_state", "confused", 0.8)
            add("intent", "challenge", 0.7)
        if any(x in text for x in NEGATION_WORDS):
            add("polarity", "negation", 1.2)
            add("modality", "negation", 1.0)
            add("intent", "deny", 0.9)
        if any(x in text for x in GUARANTEE_WORDS):
            add("modality", "guarantee", 1.1)
            if not any(x in text for x in ("不能保证", "不保证", "无法保证", "不能承诺", "不承诺", "无法承诺")):
                add("intent", "promise", 1.1)
                add("modality", "certain", 0.8)
        if any(x in text for x in ("不能保证", "不保证", "无法保证", "不能承诺", "不承诺", "无法承诺", "不可以", "不能")):
            add("intent", "refuse", 1.0)
        if any(x in text for x in ("为准", "以系统", "以页面", "按规则", "平台规则", "规则")):
            add("target", "rule", 0.9)
        # Generic system outcome cue. Keep this domain-neutral; concrete
        # business objects such as qualification, ranking, delivery, contract,
        # course, revenue, etc. must come from the graph elements/pools, not
        # from the local evaluator.
        if any(x in text for x in ("结果", "状态", "显示", "成功", "失败", "通过", "未通过", "可用", "不可用")):
            add("target", "system_result", 0.8)
        if any(x in text for x in ("人工", "帮你改", "协调", "我帮您改", "后台改", "后台", "手动", "我给你", "我帮你", "帮你", "我来", "留住", "保留", "搞定", "安排", "优先", "抹掉", "清除")):
            add("target", "manual_intervention", 0.9)
        if any(x in text for x in ("我给你", "我帮你", "帮你", "我来", "可以帮", "能帮", "给你安排", "给你留", "给你保", "我处理")):
            add("intent", "claim", 0.8)
        if any(x in text for x in ("说明", "告知", "同步", "通知", "提醒", "介绍", "讲一下", "说一下")):
            add("intent", "inform", 0.7)
        if any(x in text for x in ("您先别急", "理解", "抱歉", "不好意思", "辛苦")):
            add("intent", "reassure", 0.8)
        if any(x in text for x in ("再确认", "再说一下", "方便说下", "具体")):
            add("intent", "clarify", 0.8)
        if any(x in text for x in ("不打扰", "再联系", "稍后联系", "祝", "再见", "结束", "先这样")):
            add("intent", "close", 0.8)
        if any(x in text for x in ("继续", "接着", "简单说", "说完", "先跟您说")):
            add("intent", "continue_push", 0.9)
            add("target", "business_process", 0.8)
        return out


def parse_elements(value: Any) -> list[SemanticElement]:
    raw = value
    if raw is None:
        return []
    if isinstance(raw, dict) and ("primary_elements" in raw or "required_elements" in raw):
        raw = raw.get("primary_elements") or raw.get("required_elements")
    if not isinstance(raw, list):
        raw = [raw]
    out: list[SemanticElement] = []
    for item in raw:
        if isinstance(item, dict) and not (item.get("type") or item.get("element_type")):
            # New minimal element row: {"value":"核心对象", "main":true, "pool":[...]}
            if item.get("value") or item.get("v"):
                el = SemanticElement.from_any({
                    "type": "surface",
                    "value": item.get("value") or item.get("v"),
                    "main": item.get("main", False),
                    "fact": item.get("fact", False),
                    "pool": item.get("pool") or item.get("secondary_pool") or item.get("variants") or [],
                    "aliases": item.get("aliases") or [],
                })
                if el is not None:
                    out.append(el)
                continue
            for key in SEMANTIC_KEYS:
                val = item.get(key)
                if val is None or val == "":
                    continue
                vals = val if isinstance(val, list) else [val]
                for one in vals:
                    raw_el = {
                        "type": key,
                        "value": str(one),
                        "main": item.get("main", False),
                        "fact": item.get("fact", False),
                        "aliases": item.get("aliases") or item.get("surface_forms") or [],
                        "pool": item.get("pool") or item.get("secondary_pool") or item.get("variants") or [],
                    }
                    el = SemanticElement.from_any(raw_el)
                    if el is not None:
                        out.append(el)
            continue
        el = SemanticElement.from_any(item)
        if el is not None:
            out.append(el)
    return _dedupe_elements(out)


def parse_element_groups(value: Any, default_role: str = "main") -> list[ElementGroup]:
    if value is None:
        return []
    raw_groups = value
    if isinstance(value, dict):
        for key in (
            "element_groups", "groups", "selector_groups", "correct_groups", "wrong_groups",
            "positive_element_groups", "negative_element_groups", "trigger_element_groups", "safe_groups", "global_element_groups",
        ):
            if isinstance(value.get(key), list):
                raw_groups = value[key]
                break
        else:
            raw_groups = [value]
    if not isinstance(raw_groups, list):
        return []
    groups: list[ElementGroup] = []
    for idx, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            continue
        elems_raw = raw.get("elements") or raw.get("primary_elements") or raw.get("required_elements") or []
        elements = parse_elements(elems_raw)
        # Attach slot-level secondary pools from compact rows.
        if isinstance(elems_raw, list):
            by_key: dict[tuple[str, str], list[str]] = {}
            for erow in elems_raw:
                if not isinstance(erow, dict):
                    continue
                terms = as_list(erow.get("secondary_pool") or erow.get("secondary_pool_terms") or erow.get("variants") or erow.get("pool"))
                if not terms:
                    continue
                e = SemanticElement.from_any(erow if (erow.get("type") or erow.get("element_type")) else {"type": "surface", "value": erow.get("value") or erow.get("v"), "main": erow.get("main", False), "pool": terms})
                if e:
                    by_key[e.key] = terms
            for e in elements:
                if not e.secondary_pool and e.key in by_key:
                    e.secondary_pool = by_key[e.key]
        if not elements:
            continue
        role = str(raw.get("group_role") or raw.get("role") or default_role or "main")
        groups.append(ElementGroup(
            group_id=str(raw.get("group_id") or raw.get("id") or f"g{idx}"),
            name=str(raw.get("name") or raw.get("description") or ""),
            role=role,
            elements=elements,
            required=role in {"main", "required", "negative", "positive", "trigger", "safe"},
            threshold=0.0,
            review_threshold=0.0,
            min_element_hits=0,
            require_all_main=bool(raw.get("require_all_main") or raw.get("all_main_required") or raw.get("require_all_core")),
            locality="same_atom_or_adjacent_in_same_turn",
            weight=1.0,
            description=str(raw.get("description") or ""),
        ))
    return groups


def _default_group_role(rule_type: str) -> str:
    if "negative" in rule_type:
        return "negative"
    if "positive" in rule_type:
        return "positive"
    if "trigger" in rule_type:
        return "trigger"
    if "selector" in rule_type:
        return "selector"
    if "soft" in rule_type or "global" in rule_type:
        return "global"
    return "main"


def _dedupe_elements(items: list[SemanticElement]) -> list[SemanticElement]:
    seen: set[tuple[str, str]] = set()
    out: list[SemanticElement] = []
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        out.append(item)
    return out


def _ordered_phrase_hit(term: str, text: str) -> bool:
    """Domain-neutral split-window phrase matching for node completion.

    Some node atoms are naturally spoken across adjacent assistant clauses, e.g.
    an operation object in one sentence and the operation verb in the next.  This
    helper only accepts long, non-numeric phrases whose characters appear in the
    same order with bounded gaps in the combined local window.  It is stricter
    than unordered overlap and is not used by hard constraints or knowledge facts.
    """
    a = compact(term)
    b = compact(text)
    if not a or not b or len(a) < 6:
        return False
    if re.search(r"\d", a):
        return False
    # Do not sequence-match pure modal/negation fragments; those must be explicit
    # so safe/unsafe polarity cannot be inferred from distant clauses.
    if any(x in a for x in ("不能", "不可以", "不会", "不得", "禁止", "保证", "承诺", "一定", "肯定")):
        return False
    pos = -1
    max_gap = 18
    for ch in a:
        nxt = b.find(ch, pos + 1)
        if nxt < 0:
            return False
        if pos >= 0 and nxt - pos - 1 > max_gap:
            return False
        pos = nxt
    return True


def _element_terms(element: SemanticElement, secondary: dict[str, Any]) -> list[str]:
    terms = [element.value, *element.aliases, *element.secondary_pool]
    pool = pool_for_element(element, secondary)
    terms.extend(as_list(pool.get("surface_forms") or pool.get("aliases") or pool.get("semantic_equivalents") or pool.get("terms") or pool.get("secondary_pool") or pool.get("variants") or pool.get("pool")))
    return dedupe([str(t or "") for t in terms])


def element_surface_hit(element: SemanticElement, text: str, secondary: dict[str, Any], *, allow_ordered_window: bool = False) -> bool:
    terms = _element_terms(element, secondary)
    if element.type in {"surface", "business_target", "attribute", "value", "time", "date", "duration", "quantity", "condition", "target", "action", "object", "subject", "unit"}:
        return any(text_hit(term, text) or (allow_ordered_window and _ordered_phrase_hit(term, text)) for term in terms)
    return any((text_hit(term, text) or (allow_ordered_window and _ordered_phrase_hit(term, text))) for term in terms if term and compact(term) != compact(element.value))


def element_hit(element: SemanticElement, atom: DialogueAtom, secondary: dict[str, Any]) -> bool:
    for have in atom.elements:
        if have.type == element.type and compact(have.value) == compact(element.value):
            return True
    return element_surface_hit(element, atom.text, secondary)


def pool_for_element(element: SemanticElement, secondary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(secondary, dict):
        return {}
    keys = [f"{element.type}:{element.value}", f"{element.type}={element.value}", element.element_id, element.value, element.type]
    for key in keys:
        if key and isinstance(secondary.get(key), dict):
            return secondary[key]
        if key and isinstance(secondary.get(key), list):
            return {"terms": secondary[key]}
    pools = secondary.get("pools")
    if isinstance(pools, list):
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            pe = pool.get("primary_element") or pool.get("element") or {}
            if isinstance(pe, dict):
                ptype = str(pe.get("type") or pe.get("element_type") or "surface")
                pval = str(pe.get("value") or pe.get("v") or pe.get("name") or pe.get("text") or "")
                if ptype == element.type and compact(pval) == compact(element.value):
                    return pool.get("secondary_pool") if isinstance(pool.get("secondary_pool"), dict) else pool
    return {}


def secondary_surface_hits(rule: ElementRule, atom: DialogueAtom) -> list[str]:
    hits: list[str] = []
    for element in [*rule.primary_elements, *rule.positive_elements, *rule.negative_elements, *[e for g in rule.element_groups for e in g.elements]]:
        pool = pool_for_element(element, rule.secondary_pools)
        for term in [*as_list(pool), *element.secondary_pool]:
            if text_hit(term, atom.text):
                hits.append(term)
    return dedupe(hits)


def match_side(
    engine: ElementEngine,
    rule_id: str,
    side: str,
    elements: list[dict[str, Any]],
    secondary: dict[str, Any],
    zero: list[dict[str, Any]],
    policy: dict[str, Any],
    atoms: list[DialogueAtom],
    element_groups: Any = None,
) -> ElementMatch:
    rule = engine.make_rule(rule_id, side, primary=elements, secondary=secondary, zero=zero, policy=policy, element_groups=element_groups)
    return engine.match_rule(rule, atoms)


def _atom_seq(atom: DialogueAtom) -> int:
    m = re.search(r"_(\d+)$", atom.atom_id or "")
    return int(m.group(1)) if m else 0


def _match_rank(match: ElementMatch) -> tuple[int, float]:
    order = {"hit": 3, "review": 2, "miss": 1}
    return (order.get(match.verdict, 0), match.score)


def _element_audit(e: SemanticElement) -> dict[str, Any]:
    return {
        "value": e.value,
        "main": bool(e.required),
        "fact": bool(getattr(e, "fact", False)),
        "pool": list(e.secondary_pool),
        "type": e.type,
    }


def _candidate_audit(anchor: DialogueAtom, window: EvidenceWindow, match: ElementMatch, group_evals: list[GroupEval]) -> dict[str, Any]:
    return {
        "anchor_atom_id": anchor.atom_id,
        "turn_index": anchor.turn_index,
        "speaker": anchor.speaker,
        "text": anchor.text,
        "window": window.kind,
        "window_texts": [a.text for a in window.atoms],
        "verdict": match.verdict,
        "score": round(float(match.score or 0.0), 4),
        "reason": match.reason,
        "groups": [
            {
                "group_id": g.group.group_id,
                "role": g.group.role,
                "verdict": g.verdict,
                "score": round(float(g.score or 0.0), 4),
                "main_coverage": round(float(g.main_coverage or 0.0), 4),
                "group_coverage": round(float(g.group_coverage or 0.0), 4),
                "hit_elements": [_element_audit(e) for e in g.hit_elements],
                "missing_elements": [_element_audit(e) for e in g.missing_elements],
                "all_elements": [_element_audit(e) for e in g.group.elements],
                "reason": g.reason,
            }
            for g in group_evals
        ],
    }


def _threshold_profile(rule_type: str) -> dict[str, float]:
    if rule_type in THRESHOLD_PROFILES:
        return THRESHOLD_PROFILES[rule_type]
    if "knowledge" in rule_type and ("negative" in rule_type or "positive" in rule_type):
        return THRESHOLD_PROFILES["knowledge_positive"]
    if "constraint" in rule_type and "negative" in rule_type:
        return THRESHOLD_PROFILES["constraint_negative"]
    if "constraint" in rule_type and "positive" in rule_type:
        return THRESHOLD_PROFILES["constraint_positive"]
    if "trigger" in rule_type:
        return THRESHOLD_PROFILES["node_trigger"]
    return THRESHOLD_PROFILES["default"]


def _decisive_groups(rule_type: str, group_evals: list[GroupEval]) -> list[GroupEval]:
    roles_by_type = {
        "node_positive": {"main", "required"},
        "node_trigger": {"trigger", "main", "required"},
        "knowledge_selector": {"selector", "main", "required"},
        "knowledge_positive": {"correct", "positive", "main", "required"},
        "knowledge_negative": {"wrong", "negative", "main", "required"},
        "constraint_trigger": {"trigger", "main", "required"},
        "constraint_negative": {"negative", "negative_main", "main", "required"},
        "constraint_positive": {"safe", "safe_exception", "positive", "main", "required"},
        "soft_global": {"global", "main", "required"},
    }
    roles = roles_by_type.get(rule_type, {"main", "required"})
    picked = [g for g in group_evals if g.group.role in roles or (rule_type == "node_positive" and g.group.required and g.group.role != "supporting")]
    return picked or group_evals


def _requires_all_decisive_groups(rule_type: str) -> bool:
    # Element-group roles are no longer part of the prompt contract. Multiple
    # groups are settled by aggregate local-IDF score rather than a hard
    # conjunctive gate. This avoids over-strict node misses when a客服自然地
    # splits one atom into a core clause and a nearby explanatory clause.
    return False


def _special_factor(element: SemanticElement) -> float:
    terms = " ".join([element.value, *element.secondary_pool, *element.aliases])
    c = compact(terms)
    factor = 1.0
    if re.search(r"\d", terms) or any(x in c for x in ("元", "点", "天", "单", "小时", "分钟", "%", "％")):
        factor *= 1.20
    if any(x in c for x in NEGATION_WORDS) or element.value in {"否定", "negation"}:
        factor *= 1.15
    if len(compact(element.value)) <= 1:
        factor *= 0.80
    if compact(element.value) in {"可以", "需要", "进行", "这个", "那个", "一下", "今天"}:
        factor *= 0.85
    return factor


def _element_to_raw(e: SemanticElement) -> dict[str, Any]:
    return {"type": e.type, "value": e.value, "main": e.required, "fact": getattr(e, "fact", False), "pool": list(e.secondary_pool)}
