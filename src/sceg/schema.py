from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal

NodeType = Literal["process", "knowledge", "constraint", "terminal", "branch", "meta"]
Severity = Literal["low", "medium", "high", "critical"]


def _list_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(x) for x in value if isinstance(x, dict)]
    return []


def _dict(value: Any) -> dict[str, Any]:
    """Return a safe mapping without trusting model-emitted JSON shapes.

    LLM sometimes returns fields that are documented as objects as strings,
    lists, nulls, or malformed partial pair lists.  Runtime loading should not
    fail because a non-critical metadata field used a loose shape.  Only real
    mappings are preserved; pair lists are accepted when they are valid; other
    values are ignored.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            if isinstance(item, dict):
                out.update(item)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = item[1]
        return out
    return {}


def _safe_score_effect(value: Any) -> dict[str, Any]:
    """Normalize score_effect from model output.

    Soft constraints are often generated with a plain string like
    ``"回复超过20字时扣分"``.  Treat that as a human-readable description
    rather than letting ``dict(str)`` raise and block offline evaluation.
    """
    if isinstance(value, str):
        text = value.strip()
        return {"description": text} if text else {}
    return _dict(value)


def _element_from_loose(value: Any, *, main: bool | None = None, fact: bool | None = None) -> dict[str, Any] | None:
    """Convert loose model element shapes into the canonical {value,main,fact,pool}."""
    if isinstance(value, str):
        text = value.strip()
        return {"value": text, "main": bool(main), "fact": bool(fact), "pool": []} if text else None
    if not isinstance(value, dict):
        return None
    val = str(value.get("value") or value.get("v") or value.get("text") or value.get("name") or value.get("description") or "").strip()
    if not val:
        return None
    return {
        "value": val,
        "main": bool(value.get("main") if main is None else main),
        "fact": bool(value.get("fact") if fact is None else fact),
        "pool": list(value.get("pool") or value.get("secondary_pool") or value.get("variants") or []),
    }


def _normalize_group_list(value: Any) -> list[dict[str, Any]]:
    """Normalize element group shapes from LLM and older graphs.

    Canonical shape is [{"elements":[{"value":...,"main":...,"fact":...,"pool":[...]}]}].
    The loader also accepts mistaken flat element arrays and rows such as
    {"element":"对象", "value":"相反值", "fact":true}.
    """
    if value is None:
        return []
    raw = value
    if isinstance(raw, dict):
        for k in ("element_groups", "groups", "selector_groups", "correct_groups", "wrong_groups", "negative_groups", "safe_groups", "trigger_groups"):
            if isinstance(raw.get(k), list):
                raw = raw[k]
                break
        else:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    groups: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, str):
            e = _element_from_loose(row)
            if e:
                flat.append(e)
            continue
        if not isinstance(row, dict):
            continue
        elems: list[dict[str, Any]] = []
        if isinstance(row.get("elements"), list):
            elems = [e for e in (_element_from_loose(x) for x in row.get("elements") or []) if e]
        elif "element" in row and "value" in row:
            # Common malformed wrong_group: {"element":"对象", "value":"错误值", ...}
            obj = _element_from_loose(row.get("element"), main=True, fact=False)
            val = _element_from_loose({"value": row.get("value"), "pool": row.get("pool") or []}, main=bool(row.get("main", False)), fact=bool(row.get("fact", True)))
            elems = [x for x in (obj, val) if x]
        elif row.get("value") or row.get("v") or row.get("text") or row.get("name"):
            e = _element_from_loose(row)
            if e:
                flat.append(e)
            continue
        if elems:
            g = {k: v for k, v in row.items() if k not in {"elements", "element", "value", "v", "text", "name", "description", "main", "fact", "pool", "secondary_pool", "variants"}}
            g["elements"] = elems
            groups.append(g)
    if groups:
        return groups
    return [{"elements": flat}] if flat else []


def _ensure_group_main(groups: list[dict[str, Any]], *, fact_allowed: bool = True) -> list[dict[str, Any]]:
    out = []
    for g in groups or []:
        g = dict(g)
        elems = [dict(e) for e in g.get("elements") or [] if isinstance(e, dict) and str(e.get("value") or "").strip()]
        if not elems:
            continue
        if not fact_allowed:
            for e in elems:
                e["fact"] = False
        if not any(e.get("main") is True for e in elems):
            elems[0]["main"] = True
        g["elements"] = elems
        out.append(g)
    return out


def _bind_fact_groups(groups: list[dict[str, Any]], selector_groups: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    selector_main: dict[str, Any] | None = None
    for sg in selector_groups or []:
        for e in sg.get("elements") or []:
            if isinstance(e, dict) and e.get("main") is True and e.get("fact") is not True:
                selector_main = {"value": e.get("value"), "main": True, "fact": False, "pool": list(e.get("pool") or [])}
                break
        if selector_main:
            break
    out: list[dict[str, Any]] = []
    for g in groups or []:
        g = dict(g)
        elems = [dict(e) for e in g.get("elements") or [] if isinstance(e, dict) and str(e.get("value") or "").strip()]
        if not elems:
            continue
        for e in elems:
            if e.get("fact") is True:
                e["main"] = False
        if any(e.get("fact") is True for e in elems):
            has_non_fact_main = any(e.get("main") is True and e.get("fact") is not True for e in elems)
            if not has_non_fact_main and selector_main:
                if not any(str(x.get("value")) == str(selector_main.get("value")) for x in elems):
                    elems.insert(0, dict(selector_main))
                else:
                    for x in elems:
                        if str(x.get("value")) == str(selector_main.get("value")):
                            x["main"] = True
                            x["fact"] = False
                            break
        if not any(e.get("main") is True for e in elems):
            for e in elems:
                if e.get("fact") is not True:
                    e["main"] = True
                    break
        g["elements"] = elems
        out.append(g)
    return out


def _value_check_has_comparable(vc: Any) -> bool:
    vc = _dict(vc)
    if not vc:
        return False
    candidates: list[str] = []
    for x in (vc.get("expected_value"), vc.get("expected"), vc.get("normalized_expected")):
        if str(x or "").strip():
            candidates.append(str(x))
    checks = vc.get("checks") or vc.get("value_checks") or []
    if isinstance(checks, dict):
        checks = [checks]
    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, dict):
                for x in (c.get("expected_value"), c.get("expected"), c.get("normalized_expected")):
                    if str(x or "").strip():
                        candidates.append(str(x))
    for text in candidates:
        if re.search(r"\d+(?:\.\d+)?\s*(单|天|元|秒|分钟|小时|点)", text):
            return True
        if any(term in text for term in ("今天", "当天", "当日", "今日", "次日", "第二天", "隔天", "明天", "前一天", "前日", "上一天", "18点", "18:00")):
            return True
        if any(term in text for term in ("一单","两单","三单","四单","五单","六单","七单","八单","九单","十单","十二单","十天","七天")):
            return True
    return False

def _norm_elem_text(v: Any) -> str:
    return re.sub(r"[\s，。,.、：:；;（）()【】\[\]\-~—_]+", "", str(v or "").lower())

def _groups_signature(groups: list[dict[str, Any]]) -> set[str]:
    sig: set[str] = set()
    for g in groups or []:
        for e in g.get("elements") or []:
            if isinstance(e, dict):
                val = _norm_elem_text(e.get("value"))
                if val:
                    sig.add(val)
    return sig

def _drop_non_executable_wrong_groups(wrong_groups: list[dict[str, Any]], correct_groups: list[dict[str, Any]], value_check: Any) -> list[dict[str, Any]]:
    # Comparable facts are judged by value_check only; wrong_groups on such rows
    # cause topic-only false conflicts when the model writes the opposite value.
    if _value_check_has_comparable(value_check):
        return []
    w_sig = _groups_signature(wrong_groups)
    c_sig = _groups_signature(correct_groups)
    if w_sig and c_sig and w_sig <= c_sig:
        return []
    return wrong_groups


@dataclass(slots=True)
class EvidenceGroup:
    id: str
    description: str = ""
    patterns: list[dict[str, Any]] = field(default_factory=list)
    min_hits: int = 1
    required: bool = True
    weight: float = 1.0
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceGroup":
        return cls(
            id=str(data.get("id") or "group"),
            description=str(data.get("description") or data.get("text") or ""),
            patterns=list(data.get("patterns") or []),
            min_hits=int(data.get("min_hits", 1)),
            required=bool(data.get("required", True)),
            weight=float(data.get("weight", 1.0)),
            aliases=[str(x) for x in data.get("aliases", [])],
        )


@dataclass(slots=True)
class Requirement:
    """Node-internal atom/requirement.

    The new executor treats this as a positive target atom.  It may be emitted as
    ``requirements`` or as node-level ``atoms``.  Old evidence_groups fields are
    intentionally only kept as raw graph context; they are no longer the primary
    evaluation mechanism.
    """

    id: str
    text: str = ""
    evidence_groups: list[EvidenceGroup] = field(default_factory=list)
    required: bool = True
    weight: float = 1.0
    aliases: list[str] = field(default_factory=list)
    positive_object: dict[str, Any] = field(default_factory=dict)
    atom_type: str = "positive_object_atom"
    object_role: str = "positive_object"
    primary_elements: list[dict[str, Any]] = field(default_factory=list)
    secondary_elements: dict[str, Any] = field(default_factory=dict)
    zero_level_elements: list[dict[str, Any]] = field(default_factory=list)
    positive_elements: list[dict[str, Any]] = field(default_factory=list)
    negative_elements: list[dict[str, Any]] = field(default_factory=list)
    match_policy: dict[str, Any] = field(default_factory=dict)
    element_groups: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Requirement":
        er = _dict(data.get("element_rule"))
        groups = [EvidenceGroup.from_dict(x) for x in data.get("evidence_groups", []) if isinstance(x, dict)]
        if not groups and data.get("patterns") is not None:
            groups = [EvidenceGroup.from_dict({"id": f"{data.get('id') or 'requirement'}.legacy", "description": data.get("text") or data.get("description") or "", "patterns": data.get("patterns") or []})]
        positive_object = _dict(data.get("positive_object") or data.get("target_object"))
        primary = list(data.get("primary_elements") or er.get("primary_elements") or er.get("required_elements") or positive_object.get("primary_elements") or [])
        return cls(
            id=str(data.get("id") or data.get("atom_id") or "requirement"),
            text=str(data.get("text") or data.get("description") or data.get("name") or ""),
            evidence_groups=groups,
            required=bool(data.get("required", True)),
            weight=float(data.get("weight", 1.0)),
            aliases=[str(x) for x in data.get("aliases", [])],
            positive_object=positive_object,
            atom_type=str(data.get("atom_type") or data.get("type") or "positive_object_atom"),
            object_role=str(data.get("object_role") or data.get("role") or "positive_object"),
            primary_elements=primary,
            secondary_elements=_dict(data.get("secondary_elements") or data.get("secondary_pools") or er.get("secondary_elements") or er.get("secondary_pools")),
            # zero_level_elements were removed from the runtime model; keep empty for compatibility.
            zero_level_elements=[],
            positive_elements=_list_dicts(data.get("positive_elements") or er.get("positive_elements")),
            negative_elements=_list_dicts(data.get("negative_elements") or er.get("negative_elements")),
            match_policy=_dict(data.get("match_policy") or er.get("match_policy")),
            element_groups=_normalize_group_list(data.get("element_groups") or er.get("element_groups") or []),
        )

    @classmethod
    def from_legacy_group(cls, group: EvidenceGroup) -> "Requirement":
        return cls(id=group.id, text=group.description, evidence_groups=[group], required=group.required, weight=group.weight, aliases=list(group.aliases))


@dataclass(slots=True)
class ActivationProfile:
    mode: str = "always"
    patterns: list[dict[str, Any]] = field(default_factory=list)
    trigger_object: dict[str, Any] = field(default_factory=dict)
    primary_elements: list[dict[str, Any]] = field(default_factory=list)
    secondary_elements: dict[str, Any] = field(default_factory=dict)
    zero_level_elements: list[dict[str, Any]] = field(default_factory=list)
    positive_elements: list[dict[str, Any]] = field(default_factory=list)
    negative_elements: list[dict[str, Any]] = field(default_factory=list)
    match_policy: dict[str, Any] = field(default_factory=dict)
    element_groups: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str | None) -> "ActivationProfile":
        if isinstance(data, dict):
            er = _dict(data.get("element_rule"))
            raw_patterns = data.get("patterns") or []
            if isinstance(raw_patterns, dict):
                patterns = [raw_patterns]
            elif isinstance(raw_patterns, str):
                patterns = [{"any": [raw_patterns]}] if raw_patterns.strip() else []
            elif isinstance(raw_patterns, list):
                patterns = [p for p in raw_patterns if isinstance(p, dict)]
            else:
                patterns = []
            trigger_object = _dict(data.get("trigger_object") or data.get("target_object") or data.get("positive_object"))
            return cls(
                mode=str(data.get("mode", "always")),
                patterns=patterns,
                trigger_object=trigger_object,
                primary_elements=list(data.get("primary_elements") or er.get("primary_elements") or er.get("required_elements") or trigger_object.get("primary_elements") or []),
                secondary_elements=_dict(data.get("secondary_elements") or data.get("secondary_pools") or er.get("secondary_elements") or er.get("secondary_pools")),
                # zero_level_elements were removed from the runtime model; keep empty for compatibility.
                zero_level_elements=[],
                positive_elements=_list_dicts(data.get("positive_elements") or er.get("positive_elements")),
                negative_elements=_list_dicts(data.get("negative_elements") or er.get("negative_elements")),
                match_policy=_dict(data.get("match_policy") or er.get("match_policy")),
                element_groups=[dict(x) for x in (data.get("trigger_groups") or data.get("element_groups") or er.get("trigger_groups") or er.get("element_groups") or data.get("trigger_element_groups") or []) if isinstance(x, dict)],
            )
        if isinstance(data, str):
            low = data.strip().lower()
            if low in {"always", "start", "required", "默认", "始终", "必做"}:
                return cls(mode="always")
            if low in {"optional", "faq", "可选", "非必做"}:
                return cls(mode="optional")
            return cls(mode="user_triggered", trigger_object={"surface_forms": [data.strip()]} if data.strip() else {})
        return cls()


@dataclass(slots=True)
class GraphNode:
    id: str
    name: str
    node_type: NodeType = "process"
    required: bool = True
    activation: ActivationProfile = field(default_factory=ActivationProfile)
    requirements: list[Requirement] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    atoms: list[dict[str, Any]] = field(default_factory=list)
    atom_relations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def evidence_groups(self) -> list[EvidenceGroup]:
        groups: list[EvidenceGroup] = []
        for req in self.requirements:
            groups.extend(req.evidence_groups)
        return groups

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphNode":
        reqs = [Requirement.from_dict(x) for x in data.get("requirements", []) if isinstance(x, dict)]
        atoms = [dict(x) for x in data.get("atoms", []) if isinstance(x, dict)]
        atom_reqs = [Requirement.from_dict(x) for x in atoms]
        # In the atom/element schema, atoms are the minimum executable scoring
        # unit.  Old requirements generated by the compiler are compatibility
        # shells only; evaluating both atoms and legacy shells double-counts the
        # same obligation and can drag positive samples to zero because those
        # shells often contain only atom ids such as ``a_confirm_identity``.
        # Therefore, once executable atoms exist, they are the *only* scoring
        # requirements.  Legacy requirements are used only for graphs without
        # atoms.
        if atom_reqs:
            reqs = atom_reqs
        if not reqs:
            reqs = [Requirement.from_legacy_group(EvidenceGroup.from_dict(x)) for x in data.get("evidence_groups", []) if isinstance(x, dict)]
        node_id = data.get("id") or data.get("node_id")
        if node_id is None:
            raise ValueError(f"Invalid graph node, expected id or node_id: {data!r}")
        return cls(
            id=str(node_id),
            name=str(data.get("name") or node_id),
            node_type=str(data.get("type") or data.get("node_type") or data.get("state_role") or "process"),
            required=bool(data.get("required", True)),
            activation=ActivationProfile.from_dict(data.get("activation")),
            requirements=reqs,
            tags=list(data.get("tags") or []),
            aliases=[str(x) for x in data.get("aliases", [])],
            atoms=atoms,
            atom_relations=[dict(x) for x in data.get("atom_relations", []) if isinstance(x, dict)],
        )


@dataclass(slots=True)
class GraphEdge:
    source: str
    target: str
    relation: str = "before"
    weight: float = 1.0
    condition: str = ""
    prerequisite: str = "source_active"
    branch_group: str = ""
    terminal_effect: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        """Build an edge from both new and legacy graph schemas.

        Edges are state-graph transitions, not just ordering hints.  The runtime
        keeps the transition type (before / condition_on / branch_choice /
        terminal_after / suppress_after), the natural-language condition and
        optional terminal effects so branch and early-stop structures remain
        executable after compilation.
        """
        source = data.get("source", data.get("from"))
        target = data.get("target", data.get("to"))
        if source is None or target is None:
            raise ValueError(f"Invalid graph edge, expected source/target or from/to: {data!r}")
        edge_type = str(data.get("type") or data.get("edge_type") or data.get("relation") or "before")
        relation = str(data.get("relation") or edge_type or "before")
        condition = data.get("condition")
        if isinstance(condition, dict):
            condition_text = str(condition.get("description") or condition.get("text") or condition)
        else:
            condition_text = str(condition or "")
        terminal_effect = data.get("terminal_effect") or data.get("effect") or {}
        return cls(
            source=str(source),
            target=str(target),
            relation=relation,
            weight=float(data.get("weight", 1.0)),
            condition=condition_text,
            prerequisite=str(data.get("prerequisite") or data.get("gate") or data.get("prerequisite_policy") or "source_active"),
            branch_group=str(data.get("branch_group") or data.get("branch_id") or ""),
            terminal_effect=dict(terminal_effect) if isinstance(terminal_effect, dict) else {},
        )


@dataclass(slots=True)
class RelationGroup:
    id: str
    name: str
    group_type: str = "all_of"
    nodes: list[str] = field(default_factory=list)
    min_completed: int | None = None
    weight: float = 1.0
    required: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelationGroup":
        min_completed = data.get("min_completed")
        return cls(
            id=str(data.get("id") or data.get("group_id") or "relation_group"),
            name=str(data.get("name") or data.get("id") or data.get("group_id") or "关系组"),
            group_type=str(data.get("type") or data.get("group_type") or data.get("relation") or data.get("relation_type") or "all_of"),
            # relation_groups are node-level relations for the local executor.
            # Some LLM graphs also emit atom-level relation_groups with atom_ids;
            # those are executed through node.atom_relations, so they must not be
            # interpreted as node ids here.
            nodes=[str(x) for x in (data.get("nodes") or data.get("node_ids") or data.get("members") or data.get("member_nodes") or [])],
            min_completed=int(min_completed) if min_completed is not None else None,
            weight=float(data.get("weight", 1.0)),
            required=bool(data.get("required", True)),
            description=str(data.get("description") or ""),
        )


@dataclass(slots=True)
class KnowledgeClaim:
    id: str
    name: str
    claim_patterns: list[dict[str, Any]] = field(default_factory=list)
    support_patterns: list[dict[str, Any]] = field(default_factory=list)
    refute_patterns: list[dict[str, Any]] = field(default_factory=list)
    severity: Severity = "medium"
    reason: str = ""
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeClaim":
        return cls(
            id=str(data.get("id") or "claim"),
            name=str(data.get("name") or data.get("id") or "知识声明"),
            claim_patterns=list(data.get("claim_patterns") or []),
            support_patterns=list(data.get("support_patterns") or []),
            refute_patterns=list(data.get("refute_patterns") or data.get("conflict_patterns") or []),
            severity=data.get("severity", "medium"),
            reason=str(data.get("reason") or ""),
            aliases=[str(x) for x in data.get("aliases", [])],
        )


@dataclass(slots=True)
class KnowledgeItem:
    id: str
    name: str
    node_id: str | None = None
    judge_type: str = "element_fact_verification"
    expected: dict[str, Any] = field(default_factory=dict)
    conflict_patterns: list[dict[str, Any]] = field(default_factory=list)
    support_patterns: list[dict[str, Any]] = field(default_factory=list)
    claims: list[KnowledgeClaim] = field(default_factory=list)
    severity: Severity = "medium"
    aliases: list[str] = field(default_factory=list)
    atom_type: str = "business_fact_atom"
    positive_elements: list[dict[str, Any]] = field(default_factory=list)
    negative_elements: list[dict[str, Any]] = field(default_factory=list)
    primary_elements: list[dict[str, Any]] = field(default_factory=list)
    secondary_elements: dict[str, Any] = field(default_factory=dict)
    zero_level_elements: list[dict[str, Any]] = field(default_factory=list)
    match_policy: dict[str, Any] = field(default_factory=dict)
    negation_rule: dict[str, Any] = field(default_factory=dict)
    value_check: dict[str, Any] = field(default_factory=dict)
    element_groups: list[dict[str, Any]] = field(default_factory=list)
    positive_element_groups: list[dict[str, Any]] = field(default_factory=list)
    negative_element_groups: list[dict[str, Any]] = field(default_factory=list)
    selector_element_groups: list[dict[str, Any]] = field(default_factory=list)
    correct_element_groups: list[dict[str, Any]] = field(default_factory=list)
    wrong_element_groups: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeItem":
        er = _dict(data.get("element_rule"))
        pos = _list_dicts(data.get("positive_elements") or data.get("support_elements") or data.get("fact_elements") or er.get("positive_elements") or er.get("fact_elements"))
        neg = _list_dicts(data.get("negative_elements") or data.get("refute_elements") or er.get("negative_elements"))
        kid = data.get("id") or data.get("knowledge_id") or data.get("atom_id")
        if kid is None:
            raise ValueError(f"Invalid knowledge atom, expected id/knowledge_id/atom_id: {data!r}")
        selector_groups = _ensure_group_main(_normalize_group_list(data.get("selector_groups") or data.get("selector_element_groups") or er.get("selector_groups") or []), fact_allowed=False)
        correct_groups = _bind_fact_groups(_normalize_group_list(data.get("correct_groups") or data.get("correct_element_groups") or data.get("positive_element_groups") or er.get("correct_groups") or er.get("positive_element_groups") or []), selector_groups)
        wrong_groups = _bind_fact_groups(_normalize_group_list(data.get("wrong_groups") or data.get("wrong_element_groups") or data.get("negative_element_groups") or er.get("wrong_groups") or er.get("negative_element_groups") or []), selector_groups)
        value_check = _dict(data.get("value_check"))
        wrong_groups = _drop_non_executable_wrong_groups(wrong_groups, correct_groups, value_check)
        element_groups = _normalize_group_list(data.get("element_groups") or er.get("element_groups") or [])
        return cls(
            id=str(kid),
            name=str(data.get("name") or kid),
            node_id=data.get("node_id"),
            judge_type=str(data.get("judge_type", "element_fact_verification")),
            expected=_dict(data.get("expected")),
            conflict_patterns=list(data.get("conflict_patterns") or data.get("refute_patterns") or []),
            support_patterns=list(data.get("support_patterns") or []),
            claims=[KnowledgeClaim.from_dict(x) for x in data.get("claims", []) if isinstance(x, dict)],
            severity=data.get("severity", "medium"),
            aliases=[str(x) for x in data.get("aliases", [])],
            atom_type=str(data.get("atom_type") or "business_fact_atom"),
            positive_elements=pos,
            negative_elements=neg,
            primary_elements=_list_dicts(data.get("primary_elements") or er.get("primary_elements") or pos),
            secondary_elements=_dict(data.get("secondary_elements") or data.get("secondary_pools") or er.get("secondary_elements") or er.get("secondary_pools")),
            zero_level_elements=[],
            match_policy=_dict(data.get("match_policy") or er.get("match_policy")),
            negation_rule=_dict(data.get("negation_rule")),
            value_check=value_check,
            element_groups=element_groups,
            positive_element_groups=correct_groups,
            negative_element_groups=wrong_groups,
            selector_element_groups=selector_groups,
            correct_element_groups=correct_groups,
            wrong_element_groups=wrong_groups,
        )


@dataclass(slots=True)
class ConstraintRule:
    id: str
    name: str
    node_id: str | None = None
    severity: Severity = "high"
    prohibited: list[dict[str, Any]] = field(default_factory=list)
    safe_context: list[dict[str, Any]] = field(default_factory=list)
    trigger: list[dict[str, Any]] = field(default_factory=list)
    trigger_object: dict[str, Any] = field(default_factory=dict)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    violation_scope: dict[str, Any] = field(default_factory=dict)
    requires_resolution: bool = False
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    enforcement: str = "hard"
    constraint_kind: str = "semantic_object"
    constraint_type: str = ""
    trigger_policy: str = "self_sufficient"
    negative_object: dict[str, Any] = field(default_factory=dict)
    detection_scope: dict[str, Any] = field(default_factory=dict)
    verdict_logic: str = ""
    soft_rule: dict[str, Any] = field(default_factory=dict)
    quality_dimension: str = ""
    evaluation_basis: dict[str, Any] = field(default_factory=dict)
    score_effect: dict[str, Any] = field(default_factory=dict)
    requires_arbitration_when_ambiguous: bool = False
    allow_multiple: bool = False
    atom_type: str = "negative_object_atom"
    positive_elements: list[dict[str, Any]] = field(default_factory=list)
    negative_elements: list[dict[str, Any]] = field(default_factory=list)
    primary_elements: list[dict[str, Any]] = field(default_factory=list)
    secondary_elements: dict[str, Any] = field(default_factory=dict)
    zero_level_elements: list[dict[str, Any]] = field(default_factory=list)
    global_elements: list[dict[str, Any]] = field(default_factory=list)
    metric: dict[str, Any] = field(default_factory=dict)
    match_policy: dict[str, Any] = field(default_factory=dict)
    element_groups: list[dict[str, Any]] = field(default_factory=list)
    positive_element_groups: list[dict[str, Any]] = field(default_factory=list)
    negative_element_groups: list[dict[str, Any]] = field(default_factory=list)
    trigger_element_groups: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConstraintRule":
        er = _dict(data.get("element_rule"))
        enforcement = str(data.get("enforcement") or ("soft" if data.get("constraint_kind") == "fuzzy_quality" or data.get("soft_rule") else "hard"))
        neg_obj = _dict(data.get("negative_object"))
        trig_obj = _dict(data.get("trigger_object") or data.get("trigger_target"))
        neg = _list_dicts(data.get("negative_elements") or data.get("violation_elements") or er.get("negative_elements") or er.get("primary_elements") or neg_obj.get("primary_elements"))
        pos = _list_dicts(data.get("positive_elements") or data.get("safe_elements") or data.get("safe_exception_elements") or er.get("positive_elements") or neg_obj.get("safe_exception_elements"))
        cid = data.get("id") or data.get("constraint_id") or data.get("atom_id")
        if cid is None:
            raise ValueError(f"Invalid constraint atom, expected id/constraint_id/atom_id: {data!r}")
        return cls(
            id=str(cid),
            name=str(data.get("name") or cid),
            node_id=data.get("node_id"),
            severity=data.get("severity", "high"),
            prohibited=list(data.get("prohibited") or []),
            safe_context=list(data.get("safe_context") or []),
            trigger=list(data.get("trigger") or []),
            trigger_object=trig_obj,
            unresolved=list(data.get("unresolved") or data.get("grey_zone") or []),
            violation_scope=_dict(data.get("violation_scope")),
            requires_resolution=bool(data.get("requires_resolution", False)),
            description=str(data.get("description") or ""),
            aliases=[str(x) for x in data.get("aliases", [])],
            enforcement=enforcement,
            constraint_kind=str(data.get("constraint_kind") or data.get("constraint_type") or ("fuzzy_quality" if enforcement == "soft" else "semantic_object")),
            constraint_type=str(data.get("constraint_type") or ""),
            trigger_policy=str(data.get("trigger_policy") or ("global_style" if enforcement == "soft" else ("requires_user_trigger" if trig_obj or data.get("trigger") else "self_sufficient"))),
            negative_object=neg_obj,
            detection_scope=_dict(data.get("detection_scope")),
            verdict_logic=str(data.get("verdict_logic") or ""),
            soft_rule=_dict(data.get("soft_rule")),
            quality_dimension=str(data.get("quality_dimension") or ""),
            evaluation_basis=_dict(data.get("evaluation_basis")),
            score_effect=_safe_score_effect(data.get("score_effect")),
            requires_arbitration_when_ambiguous=bool(data.get("requires_arbitration_when_ambiguous", data.get("requires_resolution", False))),
            allow_multiple=bool(data.get("allow_multiple", False)),
            atom_type=str(data.get("atom_type") or ("soft_quality_atom" if enforcement == "soft" else "negative_object_atom")),
            positive_elements=pos,
            negative_elements=neg,
            primary_elements=_list_dicts(data.get("primary_elements") or er.get("primary_elements") or neg),
            secondary_elements=_dict(data.get("secondary_elements") or data.get("secondary_pools") or er.get("secondary_elements") or er.get("secondary_pools") or neg_obj.get("secondary_pools")),
            # Zero-level elements were removed from the runtime model.
            # Keep the field for backward-compatible JSON loading, but ignore any
            # supplied value so old graphs cannot create automatic review routes.
            zero_level_elements=[],
            global_elements=_list_dicts(data.get("global_elements") or data.get("quality_elements") or (data.get("soft_rule", {}).get("global_elements") if isinstance(data.get("soft_rule"), dict) else [])),
            metric=_dict(data.get("metric") or (data.get("soft_rule", {}).get("metric") if isinstance(data.get("soft_rule"), dict) else {})),
            match_policy=_dict(data.get("match_policy") or er.get("match_policy")),
            element_groups=_normalize_group_list(data.get("element_groups") or er.get("element_groups") or []),
            positive_element_groups=_bind_fact_groups(_normalize_group_list(data.get("safe_groups") or data.get("positive_element_groups") or er.get("safe_groups") or er.get("positive_element_groups") or []), _normalize_group_list(data.get("negative_groups") or data.get("negative_element_groups") or er.get("negative_groups") or er.get("negative_element_groups") or [])),
            negative_element_groups=_ensure_group_main(_normalize_group_list(data.get("negative_groups") or data.get("negative_element_groups") or er.get("negative_groups") or er.get("negative_element_groups") or []), fact_allowed=True),
            trigger_element_groups=_ensure_group_main(_normalize_group_list(data.get("trigger_groups") or data.get("trigger_element_groups") or er.get("trigger_groups") or er.get("trigger_element_groups") or []), fact_allowed=False),
        )



def _normalize_terminal_policies(value: Any) -> list[dict[str, Any]]:
    """Normalize terminal policy config into executable context policies.

    Recent graph_core may use terminal_policies as a global config object, e.g.
    {"allow_terminal_nodes": [...], "max_faq_rounds": 3}.  The evaluator
    expects a list of executable policies with trigger/handling fields.  Iterating
    a dict would yield string keys and crash with "str object has no attribute get".
    Only dicts that actually look like context policies are returned.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(x) for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("policies", "items", "rules", "terminal_policies"):
            if isinstance(value.get(key), list):
                return [dict(x) for x in value[key] if isinstance(x, dict)]
        executable_keys = {"trigger", "safe_response", "handling", "resolution", "forbidden_after_safe_response", "suppress_nodes", "suppress_nodes_after_safe_response"}
        if any(k in value for k in executable_keys):
            return [dict(value)]
        return []
    return []

def _richness_score(value: Any) -> int:
    """Small structural richness heuristic used when duplicate schema rows exist."""
    if value is None:
        return 0
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, list):
        return len(value) + sum(_richness_score(x) for x in value[:20])
    if isinstance(value, dict):
        return len(value) + sum(_richness_score(v) for v in value.values())
    return 0


def _merge_rich_dict(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate constraint rows without losing rich stage-2/3 fields.

    Recent graphs may carry all three fields: hard_constraint_table,
    soft_constraint_table and a compiler-materialized constraint_table.  The
    source hard/soft rows often contain richer secondary pools, while the
    compiled constraint_table may contain executor conveniences such as
    detection_scope/violation_scope.  A simple "prefer constraint_table" drops
    useful element pools; a simple concatenation double-scores rules.  This
    merge keeps one rule id while preserving the richer value per field.
    """
    out = dict(base)
    for key, val in extra.items():
        if key not in out or out.get(key) in (None, "", [], {}):
            out[key] = val
            continue
        cur = out[key]
        if isinstance(cur, dict) and isinstance(val, dict):
            out[key] = _merge_rich_dict(cur, val)
        elif isinstance(cur, list) and isinstance(val, list):
            seen: set[str] = set()
            merged: list[Any] = []
            for item in [*cur, *val]:
                marker = str(item)
                if marker not in seen:
                    seen.add(marker)
                    merged.append(item)
            out[key] = merged
        elif _richness_score(val) > _richness_score(cur):
            out[key] = val
    return out


def _merged_constraint_table(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one executable constraint row per id without dropping rich fields.

    The compiler may materialize hard/soft constraints into constraint_table,
    but graph files can still retain the original hard_constraint_table and
    soft_constraint_table.  We need de-duplication, not blind preference: source
    tables can have better secondary_elements, while compiled rows can have
    executor metadata.  Duplicate rows are merged by id/enforcement/kind.
    """
    raw_items: list[dict[str, Any]] = []
    for key, enforcement, default_kind in (
        ("hard_constraint_table", "hard", "semantic_object"),
        ("soft_constraint_table", "soft", "fuzzy_quality"),
        ("constraint_table", "", ""),
    ):
        for item in data.get(key, []) or []:
            if isinstance(item, dict):
                rule = dict(item)
                if enforcement:
                    rule.setdefault("enforcement", enforcement)
                if default_kind:
                    rule.setdefault("constraint_kind", default_kind)
                raw_items.append(rule)

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for idx, item in enumerate(raw_items, start=1):
        cid = str(item.get("id") or item.get("constraint_id") or f"constraint_{idx}")
        enforcement = str(item.get("enforcement") or ("soft" if item.get("constraint_kind") == "fuzzy_quality" else "hard"))
        kind = str(item.get("constraint_kind") or item.get("constraint_type") or ("fuzzy_quality" if enforcement == "soft" else "semantic_object"))
        key = (cid, enforcement, kind)
        rule = dict(item)
        rule.setdefault("id", cid)
        rule.setdefault("enforcement", enforcement)
        rule.setdefault("constraint_kind", kind)
        if key not in by_key:
            by_key[key] = rule
            order.append(key)
        else:
            by_key[key] = _merge_rich_dict(by_key[key], rule)
    return [by_key[k] for k in order]



def _flatten_knowledge_table(value: Any) -> list[dict[str, Any]]:
    """Flatten new knowledge rows shaped as knowledge -> atoms.

    The prompt-level structure is knowledge point -> atoms, but the runtime
    judges one executable knowledge atom at a time.  Parent metadata is copied
    down without introducing any answer-label leakage.
    """
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(value or [], start=1):
        if not isinstance(item, dict):
            continue
        parent = {k: v for k, v in item.items() if k != "atoms"}
        atoms = item.get("atoms")
        if not isinstance(atoms, list):
            row = dict(item)
            row.setdefault("id", row.get("knowledge_id") or f"knowledge_{idx}")
            rows.append(row)
            continue
        parent_id = str(parent.get("knowledge_id") or parent.get("id") or f"knowledge_{idx}")
        for j, atom in enumerate(atoms, start=1):
            if not isinstance(atom, dict):
                continue
            row = dict(parent)
            row.update(atom)
            row.setdefault("knowledge_id", parent_id)
            row.setdefault("id", atom.get("atom_id") or f"{parent_id}_atom_{j:02d}")
            row.setdefault("name", atom.get("name") or parent.get("name") or row["id"])
            row.setdefault("severity", atom.get("severity") or parent.get("severity") or "medium")
            rows.append(row)
    return rows


def _flatten_constraint_table(value: Any) -> list[dict[str, Any]]:
    """Flatten constraint point -> atoms while preserving enforcement metadata."""
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(value or [], start=1):
        if not isinstance(item, dict):
            continue
        parent = {k: v for k, v in item.items() if k != "atoms"}
        atoms = item.get("atoms")
        if not isinstance(atoms, list):
            row = dict(item)
            row.setdefault("id", row.get("constraint_id") or f"constraint_{idx}")
            rows.append(row)
            continue
        parent_id = str(parent.get("constraint_id") or parent.get("id") or f"constraint_{idx}")
        for j, atom in enumerate(atoms, start=1):
            if not isinstance(atom, dict):
                continue
            row = dict(parent)
            row.update(atom)
            row.setdefault("constraint_id", parent_id)
            row.setdefault("id", atom.get("atom_id") or f"{parent_id}_atom_{j:02d}")
            row.setdefault("name", atom.get("name") or parent.get("name") or row["id"])
            row.setdefault("severity", atom.get("severity") or parent.get("severity") or "high")
            rows.append(row)
    return rows

def _element_key(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    typ = str(raw.get("type") or raw.get("element_type") or "").strip()
    val = str(raw.get("value") or raw.get("name") or raw.get("text") or "").strip()
    return f"{typ}:{val}" if typ and val else ""

def _merge_element_pools(*values: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        out = _merge_rich_dict(out, value)
    return out

def _collect_node_element_pools(nodes: list[GraphNode]) -> dict[str, Any]:
    pools: dict[str, Any] = {}
    def ingest(elements: Any, secondary: Any) -> None:
        if not isinstance(secondary, dict):
            return
        for raw in elements or []:
            key = _element_key(raw)
            if key and isinstance(secondary.get(key), dict):
                pools[key] = _merge_rich_dict(pools.get(key, {}), secondary[key])
    for node in nodes:
        ingest(node.activation.primary_elements, node.activation.secondary_elements)
        for req in node.requirements:
            ingest(req.primary_elements, req.secondary_elements)
            ingest(req.positive_elements, req.secondary_elements)
            ingest(req.negative_elements, req.secondary_elements)
    return pools


def _augment_node_requirement_pools(nodes: list[GraphNode]) -> None:
    global_pools = _collect_node_element_pools(nodes)
    for node in nodes:
        node_pools = _merge_element_pools(global_pools, node.activation.secondary_elements)
        for req in node.requirements:
            sec = dict(req.secondary_elements or {})
            for raw in req.primary_elements or []:
                key = _element_key(raw)
                if key and key not in sec and key in node_pools:
                    sec[key] = node_pools[key]
            req.secondary_elements = sec

def _augment_knowledge_pools(items: list[KnowledgeItem], pools: dict[str, Any]) -> None:
    for item in items:
        sec = dict(item.secondary_elements or {})
        for raw in [*(item.primary_elements or []), *(item.positive_elements or []), *(item.negative_elements or [])]:
            key = _element_key(raw)
            if key and key not in sec and key in pools:
                sec[key] = pools[key]
        item.secondary_elements = sec

def _augment_constraint_secondary(rules: list[ConstraintRule]) -> None:
    for rule in rules:
        neg_pools = rule.negative_object.get("secondary_pools") if isinstance(rule.negative_object, dict) else {}
        trig_pools = rule.trigger_object.get("secondary_pools") if isinstance(rule.trigger_object, dict) else {}
        rule.secondary_elements = _merge_element_pools(neg_pools, rule.secondary_elements, trig_pools)


@dataclass(slots=True)
class StateGraph:
    graph_id: str
    name: str
    nodes: list[GraphNode]
    edges: list[GraphEdge] = field(default_factory=list)
    relation_groups: list[RelationGroup] = field(default_factory=list)
    knowledge: list[KnowledgeItem] = field(default_factory=list)
    constraints: list[ConstraintRule] = field(default_factory=list)
    terminal_policies: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateGraph":
        try:
            # Always run the lightweight constraint sanitizer on load.  Recent
            # graphs can have only 4-8 hard rows but still contain duplicate
            # semantic clusters such as banned-phrase/backfill or safety-stop/
            # generic-safety.  Waiting until >20 rows lets those duplicates
            # reach runtime and creates negative-pack false positives.
            from .schema_atomic_pipeline import sanitize_constraint_tables
            data = sanitize_constraint_tables(data)
        except Exception:
            pass
        nodes = [GraphNode.from_dict(x) for x in data.get("nodes", []) if isinstance(x, dict)]
        node_ids = {n.id for n in nodes}
        edges = [GraphEdge.from_dict(x) for x in data.get("edges", []) if isinstance(x, dict)]
        for e in edges:
            if e.source not in node_ids or e.target not in node_ids:
                raise ValueError(f"edge references unknown node: {e.source}->{e.target}")
        groups: list[RelationGroup] = []
        for raw_group in data.get("relation_groups", []) or []:
            if not isinstance(raw_group, dict):
                continue
            g = RelationGroup.from_dict(raw_group)
            # LLM occasionally emits atom-step pseudo IDs in relation_groups
            # (for example N11_1/N11_2 for atoms inside node N11).  Relation
            # groups are node-level runtime objects, so unknown references should
            # not crash offline evaluation of an otherwise usable graph.
            filtered_nodes = [n for n in g.nodes if n in node_ids]
            if filtered_nodes:
                g.nodes = filtered_nodes
                groups.append(g)
        knowledge = [KnowledgeItem.from_dict(x) for x in _flatten_knowledge_table(data.get("knowledge_table", []))]
        constraints = [ConstraintRule.from_dict(x) for x in _flatten_constraint_table(_merged_constraint_table(data))]
        # Merge schema-internal secondary pools by element key.  This is not a
        # legacy pattern fallback; it simply ensures the atom/element runtime can
        # see aliases that LLM placed at node activation level rather than
        # duplicating into every atom/knowledge row.
        _augment_node_requirement_pools(nodes)
        _augment_knowledge_pools(knowledge, _collect_node_element_pools(nodes))
        _augment_constraint_secondary(constraints)
        return cls(
            graph_id=str(data.get("graph_id", "graph")),
            name=str(data.get("name", "未命名状态图")),
            nodes=nodes,
            edges=edges,
            relation_groups=groups,
            knowledge=knowledge,
            constraints=constraints,
            terminal_policies=_normalize_terminal_policies(data.get("terminal_policies") or data.get("termination_policies")),
            metadata=_dict(data.get("metadata")),
        )

    def node_by_id(self) -> dict[str, GraphNode]:
        return {n.id: n for n in self.nodes}

    def required_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if n.required]
