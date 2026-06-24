"""
Deterministic diagram fact extraction.

This module keeps Mermaid builders data-driven: builders consume normalized
facts from requirements/code_graph/contract artifacts instead of feature-specific
templates or LLM diagram prose.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


_CONFIDENCE_RANK = {
    "explicit": 0,
    "1": 0,
    "high": 1,
    "0.9": 1,
    "inferred": 2,
    "0.8": 2,
    "0.7": 3,
    "keyword_fallback": 4,
    "new": 5,
    "unresolved": 6,
    "unknown": 7,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace('"', "'")).strip()


def _short(value: Any, limit: int = 72) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _dedupe_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _confidence_value(raw: Any) -> str:
    value = _clean(raw).lower()
    return value or "unknown"


def _confidence_rank(raw: Any) -> int:
    return _CONFIDENCE_RANK.get(_confidence_value(raw), 6)


def _project_from_source(source_file: str) -> str:
    path = (source_file or "").replace("\\", "/")
    return path.split("/")[0] if "/" in path else ""


def _layer_from_name(*values: str) -> str:
    blob = " ".join(values).lower()
    if "controller" in blob or ".api" in blob or "/api/" in blob:
        return "api"
    if "workflow" in blob or "bridge" in blob or "service" in blob:
        return "domain"
    if "repository" in blob or "mongo" in blob or "data" in blob:
        return "data"
    if "integration" in blob or "libre" in blob or "dexcom" in blob:
        return "integration"
    if "dto" in blob or "contract" in blob:
        return "contract"
    return "component"


def _class_from_symbol(symbol: str, source_file: str = "") -> str:
    symbol = _clean(symbol)
    if not symbol:
        return ""
    if symbol.startswith("."):
        name = Path(source_file.replace("\\", "/")).stem if source_file else ""
        return name
    if "." in symbol:
        return symbol.split(".", 1)[0]
    if " " in symbol or "+" in symbol:
        first = re.split(r"\s+|\+", symbol)[0]
        return first if re.match(r"^[A-Za-z_]\w*$", first) else ""
    return symbol if re.match(r"^[A-Za-z_]\w*$", symbol) else ""


def _method_names(mapping: Dict[str, Any]) -> List[str]:
    methods: List[str] = []
    for method in mapping.get("method_impacts") or mapping.get("methods") or []:
        if isinstance(method, dict):
            raw = method.get("method_name") or method.get("display_name") or method.get("method") or ""
        else:
            raw = str(method)
        name = _clean(raw).strip(".").replace("()", "")
        if name and name not in methods:
            methods.append(name)
        if len(methods) == 5:
            break
    symbol = mapping.get("codebase_symbol") or mapping.get("normalized_symbol") or ""
    if not methods and "." in symbol:
        tail = symbol.rsplit(".", 1)[-1].strip().replace("()", "")
        if tail and re.match(r"^[A-Za-z_]\w*$", tail):
            methods.append(tail)
    return methods


def _add_component(
    components: Dict[str, Dict[str, Any]],
    *,
    name: str,
    source_file: str = "",
    class_name: str = "",
    methods: Optional[List[str]] = None,
    dtos: Optional[List[str]] = None,
    confidence: Any = "",
    evidence: str = "",
    note: str = "",
    is_new: bool = False,
) -> None:
    label = _clean(class_name or name)
    if not label:
        return
    key = _dedupe_key(label)
    if not key:
        return
    candidate = {
        "id": key,
        "name": label,
        "class_name": _clean(class_name),
        "source_file": _clean(source_file),
        "project": _project_from_source(source_file),
        "layer": _layer_from_name(label, source_file, note),
        "methods": methods or [],
        "dtos": dtos or [],
        "confidence": _confidence_value(confidence),
        "confidence_rank": _confidence_rank(confidence),
        "evidence": evidence or ("new_capability" if is_new else "mapped"),
        "note": _short(note, 180),
        "is_new": bool(is_new),
    }
    existing = components.get(key)
    if not existing or candidate["confidence_rank"] < existing.get("confidence_rank", 99):
        components[key] = candidate
        return
    if existing:
        for field in ("methods", "dtos"):
            merged = list(existing.get(field) or [])
            for item in candidate.get(field) or []:
                if item and item not in merged:
                    merged.append(item)
            existing[field] = merged[:8]
        if not existing.get("source_file") and candidate.get("source_file"):
            existing["source_file"] = candidate["source_file"]
            existing["project"] = candidate["project"]


def _component_from_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    source_file = mapping.get("source_file", "")
    raw_symbol = (
        mapping.get("class_name")
        or mapping.get("normalized_symbol")
        or mapping.get("codebase_symbol")
        or mapping.get("raw_symbol")
        or ""
    )
    class_name = mapping.get("class_name") or _class_from_symbol(raw_symbol, source_file)
    confidence = mapping.get("mapping_confidence") or mapping.get("confidence") or mapping.get("relation") or ""
    note = mapping.get("note") or mapping.get("design_impact") or ""
    is_new = bool(mapping.get("is_new_capability")) or confidence == "new" or "new" in str(mapping.get("relation", "")).lower()
    return {
        "name": raw_symbol or class_name,
        "source_file": source_file,
        "class_name": class_name,
        "methods": _method_names(mapping),
        "dtos": mapping.get("dtos") or [],
        "confidence": confidence,
        "evidence": mapping.get("mapping_confidence") or mapping.get("relation") or "mapped",
        "note": note,
        "is_new": is_new,
    }


def _sort_components(values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    layer_rank = {"api": 0, "domain": 1, "service": 2, "data": 3, "integration": 4, "contract": 5, "component": 6}
    return sorted(
        values,
        key=lambda item: (
            item.get("confidence_rank", 99),
            layer_rank.get(item.get("layer", "component"), 9),
            item.get("project", "").lower(),
            item.get("name", "").lower(),
        ),
    )


def _add_edge(
    edges: List[Dict[str, Any]],
    seen: Set[Tuple[str, str, str]],
    *,
    source: str,
    target: str,
    operation: str = "",
    payload: str = "",
    step_number: Any = "",
    confidence: Any = "",
    evidence: str = "",
) -> None:
    src = _clean(source)
    dst = _clean(target)
    if not src or not dst or src == dst:
        return
    op = _short(operation or "call", 80)
    key = (_dedupe_key(src), _dedupe_key(dst), _dedupe_key(op))
    if key in seen:
        return
    seen.add(key)
    edges.append({
        "source": src,
        "target": dst,
        "operation": op,
        "payload": _short(payload, 96),
        "step_number": step_number,
        "confidence": _confidence_value(confidence),
        "confidence_rank": _confidence_rank(confidence),
        "evidence": evidence or "flow",
    })


def _mapped_labels_for_step(step: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for mapping in step.get("codebase_mappings") or []:
        info = _component_from_mapping(mapping)
        label = info.get("class_name") or info.get("name")
        if label and label not in labels:
            labels.append(label)
    return labels


def _extract_flow_edges(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    flows = code_graph.get("mapping", {}).get("mapped_flows", []) or []
    for flow in flows:
        for step in sorted(flow.get("steps", []) or [], key=lambda item: item.get("step_number", 0)):
            src = step.get("source_component") or "Source"
            dst = step.get("destination_component") or "Destination"
            chain = [src] + _mapped_labels_for_step(step) + [dst]
            chain = [item for index, item in enumerate(chain) if item and item not in chain[:index]]
            for index in range(len(chain) - 1):
                _add_edge(
                    edges,
                    seen,
                    source=chain[index],
                    target=chain[index + 1],
                    operation=step.get("operation_signature") if index == 0 else "mapped call",
                    payload=step.get("payload_description") or step.get("business_rule") or "",
                    step_number=step.get("step_number"),
                    confidence=step.get("mapping_confidence") or "explicit",
                    evidence="code_graph.mapped_flows",
                )
    if edges:
        return sorted(edges, key=lambda item: (item.get("step_number") or 999, item["source"].lower(), item["target"].lower()))

    req_flows = requirements.get("hld_content", {}).get("2_logical_view", {}).get("interactions_and_flows", []) or []
    for flow in req_flows:
        for step in flow.get("steps") or flow.get("step_by_step_sequence") or []:
            _add_edge(
                edges,
                seen,
                source=step.get("source_component") or "Source",
                target=step.get("destination_component") or "Destination",
                operation=step.get("operation_signature") or f"step {step.get('step_number', '')}",
                payload=step.get("payload_description") or step.get("business_rule") or "",
                step_number=step.get("step_number"),
                confidence="inferred",
                evidence="requirements.flow",
            )
    return sorted(edges, key=lambda item: (item.get("step_number") or 999, item["source"].lower(), item["target"].lower()))


def _extract_components(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    components: Dict[str, Dict[str, Any]] = {}
    for project in code_graph.get("target_projects", []) or []:
        _add_component(
            components,
            name=Path(str(project).replace("\\", "/")).name or str(project),
            source_file=str(project),
            confidence="explicit",
            evidence="contract.targetProjects",
        )
    for module in code_graph.get("mapping", {}).get("mapped_modules", []) or []:
        module_name = module.get("module_name", "")
        if module_name:
            _add_component(components, name=module_name, confidence="explicit", evidence="requirements.module")
        for mapping in module.get("codebase_mappings") or []:
            _add_component(components, **_component_from_mapping(mapping))
        for api in module.get("interfaces_and_apis") or []:
            for mapping in api.get("codebase_mappings") or []:
                info = _component_from_mapping(mapping)
                info["evidence"] = "interface_mapping"
                _add_component(components, **info)
    for res in code_graph.get("seed_resolutions", []) or []:
        node = res.get("node") or {}
        label = node.get("label") or res.get("name") or ""
        _add_component(
            components,
            name=label,
            source_file=node.get("source_file") or res.get("sourceFile") or "",
            class_name=_class_from_symbol(label, node.get("source_file", "")),
            confidence=res.get("confidence") or "explicit",
            evidence="contract.seedSymbols",
            note=res.get("note", ""),
        )
    for seed in code_graph.get("contract", {}).get("seedSymbols", []) or []:
        _add_component(
            components,
            name=seed.get("name", ""),
            source_file=seed.get("sourceFile", ""),
            confidence=seed.get("confidence", ""),
            evidence="contract.seedSymbols",
            note=seed.get("note", ""),
        )
    for module in requirements.get("hld_content", {}).get("2_logical_view", {}).get("modules", []) or []:
        _add_component(
            components,
            name=module.get("module_name", ""),
            confidence="inferred",
            evidence="requirements.module",
            note=module.get("detailed_responsibility", ""),
        )
    return _sort_components(list(components.values()))


def _extract_decisions(code_graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    for index, decision in enumerate(code_graph.get("resolved_at_checkpoint_b", []) or [], start=1):
        text = _clean(decision)
        if not text:
            continue
        kind = "decision"
        lower = text.lower()
        if "persist" in lower or "mongo" in lower or "repository" in lower:
            kind = "persistence"
        elif "compute" in lower or "background" in lower or "timer" in lower:
            kind = "processing"
        elif "new" in lower:
            kind = "new_capability"
        decisions.append({
            "id": f"D{index}",
            "text": text,
            "kind": kind,
            "confidence": "explicit",
            "evidence": "contract.resolvedAtCheckpointB",
        })
    for index, item in enumerate(code_graph.get("unresolved", []) or [], start=1):
        text = _clean(item.get("question") if isinstance(item, dict) else item)
        if text:
            decisions.append({
                "id": f"U{index}",
                "text": text,
                "kind": "unresolved",
                "confidence": "unresolved",
                "evidence": "contract.unresolved",
            })
    return decisions


def _extract_lifecycle(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> List[Dict[str, str]]:
    text_parts = [
        str(code_graph.get("resolved_at_checkpoint_b", "")),
        str(code_graph.get("acceptance_criteria", "")),
        str(code_graph.get("constraints", "")),
        str(requirements.get("hld_content", {}).get("2_logical_view", {})),
    ]
    blob = " ".join(text_parts)
    candidates: List[Tuple[str, str]] = []
    patterns = [
        ("Eligibility", r"\b(?:eligible|eligibility|active\s+\w*\s*cgm|libre|dexcom)\b[^.;]{0,120}"),
        ("Trigger", r"\b(?:when|on)\s+[^.;]{0,80}(?:create|update|delete|log|logged)[^.;]{0,80}"),
        ("Grouping", r"\b\d+\s*[- ]?(?:min|minute)[^.;]{0,120}"),
        ("Active Window", r"\b\d+\s*[- ]?(?:h|hr|hour)[^.;]{0,120}"),
        ("Materialization", r"\b(?:compute[- ]?on[- ]?read|background|materiali[sz]e|persist)[^.;]{0,140}"),
        ("Outcome", r"\b(?:card|label|display|show|hide|suppress)[^.;]{0,140}"),
    ]
    for label, pattern in patterns:
        match = re.search(pattern, blob, re.IGNORECASE)
        if match:
            candidates.append((label, _short(match.group(0), 100)))
    seen: Set[str] = set()
    states: List[Dict[str, str]] = []
    for label, description in candidates:
        key = _dedupe_key(label + description)
        if key in seen:
            continue
        seen.add(key)
        states.append({"name": label, "description": description, "evidence": "requirements/contract"})
    return states


def build_diagram_facts(
    requirements: Dict[str, Any],
    code_graph: Dict[str, Any],
    *,
    module_name: str = "",
    module_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build deterministic facts for HLD or one MDD module."""
    components = _extract_components(requirements, code_graph)
    edges = _extract_flow_edges(requirements, code_graph)
    if module_name:
        tokens = {_dedupe_key(module_name)}
        if module_bundle:
            for sym in module_bundle.get("primary_symbols") or module_bundle.get("raw_symbols") or []:
                tokens.add(_dedupe_key(sym))
            for item in module_bundle.get("component_evidence") or []:
                for field in ("name", "class_name", "normalized_symbol", "raw_symbol"):
                    tokens.add(_dedupe_key(item.get(field, "")))
        components = [
            comp for comp in components
            if any(token and (token in _dedupe_key(str(comp)) or token in comp.get("id", "")) for token in tokens)
        ] or _sort_components(module_bundle.get("component_evidence", []) if module_bundle else [])
        edges = [
            edge for edge in edges
            if any(
                token and (
                    token in _dedupe_key(edge.get("source", ""))
                    or token in _dedupe_key(edge.get("target", ""))
                    or token in _dedupe_key(edge.get("operation", ""))
                )
                for token in tokens
            )
        ]
        if module_bundle and not edges:
            for flow in module_bundle.get("sequence_flows") or module_bundle.get("use_cases") or []:
                for step in flow.get("steps") or []:
                    _add_edge(
                        edges,
                        set(),
                        source=step.get("source_component") or module_name,
                        target=step.get("destination_component") or module_name,
                        operation=step.get("operation_signature") or "call",
                        payload=step.get("payload_description") or step.get("business_rule") or "",
                        step_number=step.get("step_number"),
                        confidence="inferred",
                        evidence="module.flow",
                    )
    return {
        "scope": "module" if module_name else "hld",
        "module_name": module_name,
        "components": components,
        "operation_edges": edges,
        "decisions": _extract_decisions(code_graph),
        "lifecycle": _extract_lifecycle(requirements, code_graph),
        "acceptance_criteria": code_graph.get("acceptance_criteria", []) or [],
        "constraints": code_graph.get("constraints", []) or [],
        "target_projects": code_graph.get("target_projects", []) or [],
        "coverage": {
            "component_count": len(components),
            "edge_count": len(edges),
            "decision_count": len(_extract_decisions(code_graph)),
            "lifecycle_state_count": len(_extract_lifecycle(requirements, code_graph)),
        },
    }
