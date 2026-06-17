"""
Normalize graph.json into the Graphify node-link shape expected by build_index.

Supports two input formats:
  1. Graphify (nodes + links) — passed through unchanged
  2. Feature-spec index (seedSymbols) — converted to synthetic nodes
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_seed_graph(graph: Dict[str, Any]) -> bool:
    return bool(graph.get("seedSymbols")) and not graph.get("nodes")


def _extract_identifiers(name: str) -> List[str]:
    """Pull class/method identifiers from a seed symbol name string."""
    if not name:
        return []
    paren_parts: List[str] = []
    for m in re.finditer(r"\(([^)]+)\)", name):
        paren_parts.extend(_IDENT_RE.findall(m.group(1)))
    head = re.split(r"[\(/]", name, maxsplit=1)[0].strip()
    ids: List[str] = []
    if head:
        ids.extend(_IDENT_RE.findall(head))
    ids.extend(paren_parts)
    seen: Set[str] = set()
    out: List[str] = []
    for ident in ids:
        key = ident.lower()
        if key not in seen:
            seen.add(key)
            out.append(ident)
    return out or [name.strip()]


def seed_symbols_to_nodes(seed_symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert seedSymbols entries into Graphify-style node dicts."""
    nodes: List[Dict[str, Any]] = []
    for i, sym in enumerate(seed_symbols):
        name = sym.get("name", "")
        graph_id = sym.get("graphId") or f"seed_{i}"
        source_file = sym.get("sourceFile") or sym.get("source_file") or ""
        identifiers = _extract_identifiers(name)
        primary = identifiers[0]
        nodes.append({
            "id": graph_id,
            "label": name,
            "norm_label": primary.lower(),
            "source_file": source_file,
            "relation": sym.get("relation"),
            "confidence": sym.get("confidence"),
            "ambiguous": sym.get("ambiguous"),
            "note": sym.get("note", ""),
            "aliases": identifiers[1:],
            "graph_format": "seedSymbols",
        })
        for j, alias in enumerate(identifiers[1:], start=1):
            nodes.append({
                "id": f"{graph_id}__alias_{j}",
                "label": alias,
                "norm_label": alias.lower(),
                "source_file": source_file,
                "relation": sym.get("relation"),
                "confidence": sym.get("confidence"),
                "parent_seed_id": graph_id,
                "note": sym.get("note", ""),
                "graph_format": "seedSymbols",
            })
    return nodes


def normalize_graph(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Return a graph dict that always has nodes[] and links[]."""
    if graph.get("nodes"):
        return graph

    if graph.get("seedSymbols"):
        nodes = seed_symbols_to_nodes(graph["seedSymbols"])
        return {
            **graph,
            "nodes": nodes,
            "links": graph.get("links", []),
            "_normalized_from": "seedSymbols",
        }

    return {**graph, "nodes": [], "links": graph.get("links", [])}


def graph_stats(graph: Dict[str, Any]) -> Tuple[int, int, int]:
    """Return (node_count, link_count, module_count) after normalization."""
    normalized = normalize_graph(graph)
    nodes = normalized.get("nodes", [])
    links = normalized.get("links", [])
    modules: Set[str] = set()
    for n in nodes:
        src = n.get("source_file") or ""
        if src:
            parts = src.replace("\\", "/").split("/")
            if parts and parts[0]:
                modules.add(parts[0])
    return len(nodes), len(links), len(modules)
