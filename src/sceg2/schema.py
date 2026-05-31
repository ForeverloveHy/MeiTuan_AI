from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NodeType = Literal["process", "knowledge", "constraint", "terminal", "branch", "meta"]
Severity = Literal["low", "medium", "high", "critical"]


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
    """A small verifiable task inside a state-graph node.

    The evaluator does not know task semantics. It only executes the evidence
    groups supplied by the graph-building stage. Legacy node-level
    evidence_groups are converted to one requirement per group so earlier graph
    files remain usable.
    """

    id: str
    text: str = ""
    evidence_groups: list[EvidenceGroup] = field(default_factory=list)
    required: bool = True
    weight: float = 1.0
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Requirement":
        groups = [EvidenceGroup.from_dict(x) for x in data.get("evidence_groups", [])]
        if not groups and data.get("patterns") is not None:
            groups = [
                EvidenceGroup.from_dict(
                    {
                        "id": str(data.get("id") or "requirement") + ".evidence",
                        "description": data.get("text") or data.get("description") or "",
                        "patterns": data.get("patterns") or [],
                        "min_hits": data.get("min_hits", 1),
                        "required": True,
                        "weight": 1.0,
                    }
                )
            ]
        return cls(
            id=str(data.get("id") or "requirement"),
            text=str(data.get("text") or data.get("description") or data.get("name") or ""),
            evidence_groups=groups,
            required=bool(data.get("required", True)),
            weight=float(data.get("weight", 1.0)),
            aliases=[str(x) for x in data.get("aliases", [])],
        )

    @classmethod
    def from_legacy_group(cls, group: EvidenceGroup) -> "Requirement":
        return cls(
            id=group.id,
            text=group.description,
            evidence_groups=[group],
            required=group.required,
            weight=group.weight,
            aliases=list(group.aliases),
        )


@dataclass(slots=True)
class ActivationProfile:
    mode: str = "always"  # always | user_triggered | condition | optional
    patterns: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ActivationProfile":
        data = data or {}
        return cls(mode=str(data.get("mode", "always")), patterns=list(data.get("patterns") or []))


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

    @property
    def evidence_groups(self) -> list[EvidenceGroup]:
        groups: list[EvidenceGroup] = []
        for req in self.requirements:
            groups.extend(req.evidence_groups)
        return groups

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphNode":
        reqs = [Requirement.from_dict(x) for x in data.get("requirements", [])]
        if not reqs:
            legacy_groups = [EvidenceGroup.from_dict(x) for x in data.get("evidence_groups", [])]
            reqs = [Requirement.from_legacy_group(g) for g in legacy_groups]
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            node_type=data.get("type", "process"),
            required=bool(data.get("required", True)),
            activation=ActivationProfile.from_dict(data.get("activation")),
            requirements=reqs,
            tags=list(data.get("tags") or []),
            aliases=[str(x) for x in data.get("aliases", [])],
        )


@dataclass(slots=True)
class GraphEdge:
    source: str
    target: str
    relation: str = "soft_order"  # strict_order | soft_order | branch | terminal
    weight: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            relation=str(data.get("relation", "soft_order")),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass(slots=True)
class RelationGroup:
    id: str
    name: str
    group_type: str = "all_of"  # all_of | any_of | ordered | unordered
    nodes: list[str] = field(default_factory=list)
    min_completed: int | None = None
    weight: float = 1.0
    required: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelationGroup":
        nodes = [str(x) for x in data.get("nodes", [])]
        min_completed = data.get("min_completed")
        return cls(
            id=str(data.get("id") or "relation_group"),
            name=str(data.get("name") or data.get("id") or "关系组"),
            group_type=str(data.get("type") or data.get("group_type") or "all_of"),
            nodes=nodes,
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
    judge_type: str = "pattern_conflict"
    expected: dict[str, Any] = field(default_factory=dict)
    conflict_patterns: list[dict[str, Any]] = field(default_factory=list)
    support_patterns: list[dict[str, Any]] = field(default_factory=list)
    claims: list[KnowledgeClaim] = field(default_factory=list)
    severity: Severity = "medium"
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeItem":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            node_id=data.get("node_id"),
            judge_type=str(data.get("judge_type", "pattern_conflict")),
            expected=dict(data.get("expected") or {}),
            conflict_patterns=list(data.get("conflict_patterns") or data.get("refute_patterns") or []),
            support_patterns=list(data.get("support_patterns") or []),
            claims=[KnowledgeClaim.from_dict(x) for x in data.get("claims", [])],
            severity=data.get("severity", "medium"),
            aliases=[str(x) for x in data.get("aliases", [])],
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
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    violation_scope: dict[str, Any] = field(default_factory=dict)
    requires_resolution: bool = False
    description: str = ""
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConstraintRule":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            node_id=data.get("node_id"),
            severity=data.get("severity", "high"),
            prohibited=list(data.get("prohibited") or []),
            safe_context=list(data.get("safe_context") or []),
            trigger=list(data.get("trigger") or []),
            unresolved=list(data.get("unresolved") or data.get("grey_zone") or []),
            violation_scope=dict(data.get("violation_scope") or {}),
            requires_resolution=bool(data.get("requires_resolution", False)),
            description=str(data.get("description") or ""),
            aliases=[str(x) for x in data.get("aliases", [])],
        )


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
        nodes = [GraphNode.from_dict(x) for x in data.get("nodes", [])]
        node_ids = {n.id for n in nodes}
        edges = [GraphEdge.from_dict(x) for x in data.get("edges", [])]
        for e in edges:
            if e.source not in node_ids or e.target not in node_ids:
                raise ValueError(f"edge references unknown node: {e.source}->{e.target}")
        groups = [RelationGroup.from_dict(x) for x in data.get("relation_groups", [])]
        for g in groups:
            unknown = [n for n in g.nodes if n not in node_ids]
            if unknown:
                raise ValueError(f"relation group {g.id} references unknown nodes: {unknown}")
        return cls(
            graph_id=str(data.get("graph_id", "graph")),
            name=str(data.get("name", "未命名状态图")),
            nodes=nodes,
            edges=edges,
            relation_groups=groups,
            knowledge=[KnowledgeItem.from_dict(x) for x in data.get("knowledge_table", [])],
            constraints=[ConstraintRule.from_dict(x) for x in data.get("constraint_table", [])],
            terminal_policies=list(data.get("terminal_policies") or data.get("termination_policies") or []),
            metadata=dict(data.get("metadata") or {}),
        )

    def node_by_id(self) -> dict[str, GraphNode]:
        return {n.id: n for n in self.nodes}

    def required_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if n.required]
