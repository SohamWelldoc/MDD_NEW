"""
Deterministic Mermaid builders for HLD diagrams.

The builders intentionally stay within Mermaid features that render reliably in
Markdown and DOCX: flowchart and sequence diagrams with plain labels.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from services.shared.diagram_facts import build_diagram_facts


def _safe_id(value: str, used: Set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "", "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", value or "") if part))
    if not base:
        base = "Node"
    if base[0].isdigit():
        base = f"N{base}"
    candidate = base[:48]
    index = 2
    while candidate in used:
        candidate = f"{base[:42]}{index}"
        index += 1
    used.add(candidate)
    return candidate


def _quote(value: Any) -> str:
    return '"' + re.sub(r"\s+", " ", str(value or "").replace('"', "'")).strip() + '"'


def _short(value: Any, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace('"', "'")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _project_label(project: str) -> str:
    return Path(str(project).replace("\\", "/")).name or str(project)


def _topic(requirements: Dict[str, Any], plan: Dict[str, Any]) -> str:
    title = plan.get("project_name") or requirements.get("project_name") or "System"
    intro = requirements.get("hld_content", {}).get("1_introduction", {})
    scope = intro.get("1_1_purpose_and_scope", {}) if isinstance(intro, dict) else {}
    in_scope = scope.get("in_scope") if isinstance(scope, dict) else None
    if isinstance(in_scope, list) and in_scope:
        title = str(in_scope[0]).strip() or title
    return re.sub(r"\s+feature$", "", str(title), flags=re.IGNORECASE).strip() or "System"


def _top_components(facts: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    components = facts.get("components") or []
    preferred = [c for c in components if c.get("layer") in {"api", "domain", "data", "integration"}]
    selected = preferred or components
    return selected[:limit]


def build_context_diagram(requirements: Dict[str, Any], code_graph: Dict[str, Any], plan: Dict[str, Any], facts: Dict[str, Any]) -> str:
    used: Set[str] = set()
    system_id = _safe_id(_topic(requirements, plan), used)
    lines = [
        "flowchart LR",
        f"    {system_id}[{_quote(_short(_topic(requirements, plan), 48) + ' / HLD scope')}]",
    ]

    intro = requirements.get("hld_content", {}).get("1_introduction", {})
    context = intro.get("1_4_context", {}) if isinstance(intro, dict) else {}
    upstream = context.get("upstream_dependencies", []) if isinstance(context, dict) else []
    downstream = context.get("downstream_consumers", []) if isinstance(context, dict) else []

    for index, item in enumerate(upstream[:4], start=1):
        name = item.get("system_name") or item.get("name") or f"Upstream {index}"
        trigger = item.get("trigger_event") or item.get("mechanism") or "input"
        node_id = _safe_id(name, used)
        lines.append(f"    {node_id}[{_quote(_short(name, 48))}] -->|{_quote(_short(trigger, 40))}| {system_id}")

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for comp in _top_components(facts, 10):
        grouped.setdefault(comp.get("layer") or "component", []).append(comp)
    for layer in ("api", "domain", "data", "integration", "contract", "component"):
        comps = grouped.get(layer) or []
        if not comps:
            continue
        group_id = _safe_id(f"{layer} layer", used)
        lines.append(f"    {group_id}[{_quote(layer.title() + ' layer')}]")
        lines.append(f"    {system_id} --> {group_id}")
        for comp in comps[:3]:
            comp_id = _safe_id(comp.get("name", "Component"), used)
            label = comp.get("name", "Component")
            if comp.get("project"):
                label = f"{label} / {comp['project']}"
            lines.append(f"    {group_id} --> {comp_id}[{_quote(_short(label, 64))}]")

    for project in (facts.get("target_projects") or [])[:5]:
        node_id = _safe_id(project, used)
        lines.append(f"    {system_id} -.-> {node_id}[{_quote('Project: ' + _short(_project_label(project), 44))}]")

    for item in downstream[:4]:
        name = item.get("system_name") or item.get("name") or "Downstream"
        data = item.get("data_transmitted") or item.get("mechanism") or "output"
        node_id = _safe_id(name, used)
        lines.append(f"    {system_id} -->|{_quote(_short(data, 42))}| {node_id}[{_quote(_short(name, 48))}]")

    return "\n".join(lines)


def build_feature_flow_diagram(facts: Dict[str, Any]) -> str:
    edges = facts.get("operation_edges") or []
    if not edges:
        return ""
    used: Set[str] = set()
    ids: Dict[str, str] = {}
    lines = ["flowchart LR"]

    def node(label: str) -> str:
        if label not in ids:
            node_id = _safe_id(label, used)
            ids[label] = node_id
            lines.append(f"    {node_id}[{_quote(_short(label, 54))}]")
        return ids[label]

    for edge in edges[:12]:
        src = node(edge.get("source", "Source"))
        dst = node(edge.get("target", "Destination"))
        operation = edge.get("operation") or "call"
        payload = edge.get("payload")
        label = operation if not payload else f"{operation} / {payload}"
        lines.append(f"    {src} -->|{_quote(_short(label, 56))}| {dst}")
    return "\n".join(lines)


def build_primary_sequence_diagram(facts: Dict[str, Any]) -> str:
    edges = facts.get("operation_edges") or []
    if not edges:
        return ""
    used: Set[str] = set()
    participants: Dict[str, Tuple[str, str]] = {}
    messages: List[str] = []

    def participant(label: str) -> str:
        if label not in participants:
            participants[label] = (_safe_id(label, used), _short(label, 54))
        return participants[label][0]

    for edge in edges[:10]:
        src_id = participant(edge.get("source", "Source"))
        dst_id = participant(edge.get("target", "Destination"))
        messages.append(f"    {src_id}->>{dst_id}: {_short(edge.get('operation') or 'call', 80)}")

    lines = ["sequenceDiagram"]
    for pid, alias in participants.values():
        lines.append(f"    participant {pid} as {_quote(alias)}")
    lines.extend(messages)
    return "\n".join(lines)


def build_lifecycle_diagram(facts: Dict[str, Any]) -> str:
    states = facts.get("lifecycle") or []
    if not states:
        return ""
    used: Set[str] = set()
    lines = ["flowchart LR"]
    previous = ""
    for index, state in enumerate(states[:6], start=1):
        node_id = _safe_id(state.get("name") or f"State {index}", used)
        label = f"{state.get('name', 'State')}: {_short(state.get('description', ''), 56)}"
        lines.append(f"    {node_id}[{_quote(label)}]")
        if previous:
            lines.append(f"    {previous} --> {node_id}")
        previous = node_id
    return "\n".join(lines)


def build_decision_diagram(facts: Dict[str, Any]) -> str:
    decisions = facts.get("decisions") or []
    if not decisions:
        return ""
    used: Set[str] = set()
    root = _safe_id("Architecture Decisions", used)
    lines = ["flowchart LR", f"    {root}[{_quote('Architecture decisions and evidence')}]"]
    for decision in decisions[:10]:
        node_id = _safe_id(decision.get("id") or decision.get("kind") or "Decision", used)
        label = f"{decision.get('kind', 'decision')}: {_short(decision.get('text', ''), 82)}"
        lines.append(f"    {root} --> {node_id}[{_quote(label)}]")
    return "\n".join(lines)


def build_infrastructure_diagram(facts: Dict[str, Any]) -> str:
    projects = facts.get("target_projects") or []
    decisions = [
        d for d in facts.get("decisions") or []
        if d.get("kind") in {"persistence", "processing"}
    ]
    if not projects and not decisions:
        return ""
    used: Set[str] = set()
    lines = ["flowchart LR"]
    api_id = _safe_id("Feature Runtime", used)
    lines.append(f"    {api_id}[{_quote('Feature runtime boundary')}]")
    for project in projects[:8]:
        node_id = _safe_id(project, used)
        lines.append(f"    {api_id} --> {node_id}[{_quote(_short(_project_label(project), 54))}]")
    for decision in decisions[:4]:
        node_id = _safe_id(decision.get("id") or decision.get("kind") or "Decision", used)
        lines.append(f"    {api_id} -.->|{_quote(decision.get('kind', 'decision'))}| {node_id}[{_quote(_short(decision.get('text', ''), 70))}]")
    return "\n".join(lines)


def build_hld_diagrams(
    requirements: Dict[str, Any],
    code_graph: Dict[str, Any],
    plan: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    facts = build_diagram_facts(requirements, code_graph)
    diagrams = {
        "hld_context": build_context_diagram(requirements, code_graph, plan, facts),
        "hld_feature_flow": build_feature_flow_diagram(facts),
        "hld_primary_sequence": build_primary_sequence_diagram(facts),
        "hld_lifecycle": build_lifecycle_diagram(facts),
        "hld_decision_view": build_decision_diagram(facts),
    }
    if plan.get("include_sections", {}).get("infrastructure"):
        diagrams["hld_infrastructure"] = build_infrastructure_diagram(facts)
    return {key: value for key, value in diagrams.items() if value}, facts
