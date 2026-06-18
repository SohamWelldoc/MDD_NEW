"""
Codebase Analyzer (graphify adapter shell)
==========================================

Single integration point between the HLD pipeline and **graphify**,
the chosen codebase-analysis tool for MDD_NEW.

Design notes
------------
* No AST fallback. If graphify cannot run, this service raises — we want
  loud failure rather than silent degraded output.
* The graphify call is isolated in `_run_graphify()`. Swap that one
  function when the exact import path / API is finalised; nothing else
  in the pipeline needs to change.
* The JSON envelope written to disk is intentionally a **passthrough**:
  whatever shape graphify returns is stored under `code_graph`. The HLD
  generator already treats `code_graph` as opaque metadata, so a
  normaliser can be added later without breaking callers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add agentic-orchestrator to sys.path statically
_ORCHESTRATOR_DIR = str(Path(__file__).resolve().parent.parent.parent / "agentic-orchestrator")
if _ORCHESTRATOR_DIR not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_DIR)

try:
    from query import resolve_symbol, resolve_node_by_id, load_indexes, canonical  # type: ignore
    from graph_normalizer import normalize_graph  # type: ignore
    from adapter import (  # type: ignore
        find_base_class,
        find_implemented_interfaces,
        find_callers,
        find_callees,
        find_containing_class,
        find_related_dtos,
        find_methods,
    )
except ImportError:
    resolve_symbol = None  # type: ignore
    resolve_node_by_id = None  # type: ignore
    load_indexes = None  # type: ignore
    canonical = None  # type: ignore
    normalize_graph = None  # type: ignore
    find_base_class = None  # type: ignore
    find_implemented_interfaces = None  # type: ignore
    find_callers = None  # type: ignore
    find_callees = None  # type: ignore
    find_containing_class = None  # type: ignore
    find_related_dtos = None  # type: ignore
    find_methods = None  # type: ignore


@dataclass
class CodebaseAnalysisResult:
    job_id: str
    source_path: str
    contract_path: str
    started_at: str
    completed_at: str
    code_graph: Dict[str, Any]
    artifact_path: str


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _resolve_graph_path(override: Optional[str] = None) -> str:
    """Resolve monolith graph.json from GRAPH_PATH env or workspace default."""
    root = _workspace_root()
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            return str(p.resolve())
        raise FileNotFoundError(f"Graph not found at {override}")

    env = os.getenv("GRAPH_PATH", "../graph.json")
    p = Path(env)
    if not p.is_absolute():
        p = (Path(__file__).resolve().parent.parent / env).resolve()
    if not p.is_file():
        p = root / "graph.json"
    if not p.is_file():
        raise FileNotFoundError(
            f"No graph.json found (GRAPH_PATH={env}). "
            "Set GRAPH_PATH in .env to the monolith Graphify export."
        )
    return str(p.resolve())


def _normalize_ticket(ticket: str) -> str:
    t = ticket.strip().upper()
    if t.startswith("AL-"):
        return t
    if t.isdigit():
        return f"AL-{t}"
    return t


def _resolve_contract_path(
    contract_path: Optional[str] = None,
    ticket: Optional[str] = None,
    requirements: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve feature contract JSON (explicit path or contract_{ticket}.json)."""
    root = _workspace_root()
    if contract_path:
        p = Path(contract_path)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            return str(p.resolve())
        raise FileNotFoundError(f"Contract not found at {contract_path}")

    t = ticket
    if not t and requirements:
        t = requirements.get("ticket")

    if t:
        t_norm = _normalize_ticket(str(t))
        for cand in (root / f"contract_{t_norm}.json", root / f"contract_{t}.json"):
            if cand.is_file():
                return str(cand.resolve())

    raise FileNotFoundError(
        "Feature contract required. Pass contract_path or ticket "
        "(e.g. ticket='AL-27103' loads contract_AL-27103.json)."
    )


def _load_contract(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _class_from_source_file(source_file: str) -> str:
    if not source_file:
        return ""
    name = source_file.replace("\\", "/").split("/")[-1]
    return name[:-3] if name.endswith(".cs") else name


def _method_from_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip()
    if not symbol:
        return ""
    if symbol.startswith("."):
        return symbol.lstrip(".").replace("()", "")
    if "." in symbol and not symbol.endswith(".cs"):
        tail = symbol.rsplit(".", 1)[-1]
        if re.match(r"^[A-Za-z_]\w*(?:\(\))?$", tail):
            return tail.replace("()", "")
    return ""


def _normalize_symbol_metadata(node: Dict[str, Any]) -> Dict[str, str]:
    label = (node.get("label") or "").strip()
    source_file = node.get("source_file", "") or ""
    source_class = _class_from_source_file(source_file)
    method_name = _method_from_symbol(label)

    class_name = source_class
    if not class_name and label and not label.startswith(".") and not label.endswith(".cs"):
        class_name = label.split(".", 1)[0]

    normalized = label
    if label.startswith("."):
        normalized = f"{class_name}{label}" if class_name else label.lstrip(".")
    return {
        "normalized_symbol": normalized,
        "class_name": class_name,
        "method_name": method_name,
        "source_location": node.get("source_location", ""),
        "graph_id": node.get("id", ""),
    }


def _unique_peers(result: Dict[str, Any], limit: int = 8) -> List[str]:
    peers = []
    for item in result.get("results", []) if result else []:
        peer = item.get("peer") or item.get("label") or item.get("peer_id")
        if peer and peer not in peers:
            peers.append(peer)
    return peers[:limit]


def _method_impact(method: str, owner_class: str = "", source_file: str = "") -> Dict[str, str]:
    name = _method_from_symbol(method) or method.split("_")[-1].replace("()", "")
    return {
        "method": method,
        "method_name": name,
        "owner_class": owner_class,
        "source_file": source_file,
    }


def _dto_candidates_for_mapping(label: str, note: str = "") -> List[str]:
    dto_re = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:DTO|Dto|Request|Response|Model)\b")
    values = set(dto_re.findall(f"{label} {note}"))
    if find_related_dtos and label:
        try:
            values.update(_unique_peers(find_related_dtos(label), limit=12))
        except Exception:  # noqa: BLE001
            pass
    return sorted(values)


def _symbol_mapping_from_node(
    node: Dict[str, Any],
    note: str = "",
    extra_callers: Optional[List[str]] = None,
    extra_callees: Optional[List[str]] = None,
    links_index: Optional[Dict[str, Any]] = None,
    mapping_confidence: str = "high",
    is_new_capability: bool = False,
) -> Dict[str, Any]:
    label = node.get("label", "")
    symbol_meta = _normalize_symbol_metadata(node)
    owner_class = symbol_meta.get("class_name", "")
    methods: List[str] = []

    if find_containing_class and symbol_meta.get("method_name"):
        try:
            owners = _unique_peers(find_containing_class(label), limit=1)
            if owners:
                owner_class = owners[0]
                symbol_meta["class_name"] = owner_class
                if label not in methods:
                    methods.append(label)
        except Exception:  # noqa: BLE001
            pass

    mapping: Dict[str, Any] = {
        "codebase_symbol": label,
        **symbol_meta,
        "source_file": node.get("source_file", ""),
        "source_location": node.get("source_location", ""),
        "note": note or node.get("note", ""),
        "base_classes": [],
        "implemented_interfaces": [],
        "methods": methods,
        "method_impacts": [],
        "dtos": [],
        "callers": extra_callers or [],
        "callees": extra_callees or [],
        "mapping_confidence": mapping_confidence,
        "is_new_capability": is_new_capability,
    }
    if find_base_class and label:
        mapping["base_classes"] = _unique_peers(find_base_class(label), limit=5)
        mapping["implemented_interfaces"] = _unique_peers(find_implemented_interfaces(label), limit=8)
        declared_methods = _unique_peers(find_methods(label), limit=20)
        if declared_methods:
            mapping["methods"] = declared_methods
        if not mapping["callers"] or not mapping["callees"]:
            callers, callees = _resolve_call_graph_for_node(
                node, links_index, mapping["methods"]
            )
            if not mapping["callers"]:
                mapping["callers"] = callers
            if not mapping["callees"]:
                mapping["callees"] = callees
    mapping["method_impacts"] = [
        _method_impact(m, mapping.get("class_name", ""), mapping.get("source_file", ""))
        for m in mapping.get("methods", [])[:12]
    ]
    if not mapping["method_impacts"] and symbol_meta.get("method_name"):
        mapping["method_impacts"] = [
            _method_impact(label, mapping.get("class_name", ""), mapping.get("source_file", ""))
        ]
    mapping["dtos"] = _dto_candidates_for_mapping(label, mapping.get("note", ""))
    return mapping


def _resolve_call_graph_for_node(
    node: Dict[str, Any],
    links_index: Optional[Dict[str, Any]],
    method_peers: Optional[List[str]] = None,
) -> tuple:
    """Resolve callers/callees via links_index (graphId) and method-level adapter queries."""
    nid = node.get("id")
    label = node.get("label", "")
    callers: List[str] = []
    callees: List[str] = []

    if nid and links_index:
        rels = links_index.get(nid, {"outgoing": [], "incoming": []})
        for edge in rels.get("incoming", []):
            if edge.get("relation") == "calls" and edge.get("source"):
                callers.append(edge["source"])
        for edge in rels.get("outgoing", []):
            if edge.get("relation") == "calls" and edge.get("target"):
                callees.append(edge["target"])

    # Method-level node (e.g. .GetAGPGraphData())
    if find_callers and label and label.startswith("."):
        if not callers:
            callers = [c["peer"] for c in find_callers(label).get("results", [])[:5]]
        if not callees:
            callees = [c["peer"] for c in find_callees(label).get("results", [])[:5]]

    # Class-level: aggregate from declared methods
    peers = method_peers or []
    if find_callers and find_callees and peers:
        for m_peer in peers[:6]:
            for c in find_callers(m_peer).get("results", [])[:3]:
                if c["peer"] not in callers:
                    callers.append(c["peer"])
            for c in find_callees(m_peer).get("results", [])[:3]:
                if c["peer"] not in callees:
                    callees.append(c["peer"])

    callers = list(dict.fromkeys(callers))[:8]
    callees = list(dict.fromkeys(callees))[:8]
    return callers, callees


def _resolve_contract_seeds(
    contract: Dict[str, Any],
    nodes_index: Dict[str, Any],
    links_index: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Resolve each contract seedSymbol.graphId against the monolith graph index."""
    seed_resolutions: List[Dict[str, Any]] = []
    for sym in contract.get("seedSymbols", []):
        gid = sym.get("graphId")
        name = sym.get("name", "")
        note = sym.get("note", "")
        source_file = sym.get("sourceFile") or sym.get("source_file") or ""
        is_new = sym.get("relation") == "new" or gid is None

        resolution: Dict[str, Any] = {
            "name": name,
            "graphId": gid,
            "sourceFile": source_file,
            "note": note,
            "relation": sym.get("relation"),
            "confidence": sym.get("confidence"),
            "resolved": False,
            "is_new_capability": is_new,
            "node": None,
            "callers": [],
            "callees": [],
        }

        node = None
        if gid and resolve_node_by_id:
            node, _ = resolve_node_by_id(gid, nodes_index)
        if node:
            resolution["resolved"] = True
            resolution["node"] = node
            methods = []
            if find_methods:
                methods = [m["peer"] for m in find_methods(node.get("label", "")).get("results", [])]
            resolution["callers"], resolution["callees"] = _resolve_call_graph_for_node(
                node, links_index, methods
            )
        elif is_new and source_file:
            resolution["resolved"] = True
            resolution["node"] = {
                "id": gid,
                "label": name,
                "source_file": source_file,
                "note": note,
            }
        elif is_new:
            resolution["resolved"] = False
            resolution["note"] = (note or "") + " [NEW capability — not yet in monolith graph]"
        seed_resolutions.append(resolution)
    return seed_resolutions


def _seeds_matching_text(text: str, seed_resolutions: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    import re

    if not text:
        return []
    parts = re.split(r"[\s_\-\.\/\(\)]+", text.lower())
    stopwords = {
        "api", "the", "and", "a", "of", "to", "in", "is", "for", "with", "on",
        "data", "application", "system", "service", "module", "internal",
    }
    keywords = [p for p in parts if len(p) > 2 and p not in stopwords]
    if not keywords:
        return []

    scored: List[tuple] = []
    for res in seed_resolutions:
        if not res.get("resolved"):
            continue
        name = (res.get("name") or "").lower()
        note = (res.get("note") or "").lower()
        score = 0
        for kw in keywords:
            if kw in name:
                score += 3
            elif kw in note:
                score += 1
        if score > 0:
            scored.append((score, res))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def _seeds_by_names(
    names: List[str],
    seed_resolutions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Match seed resolutions by exact seed name (from contract requirement_mappings)."""
    name_set = {n.strip() for n in names if n}
    matched = []
    for res in seed_resolutions:
        if res.get("name", "") in name_set:
            matched.append(res)
    return matched


def _scoped_stats(
    seed_resolutions: List[Dict[str, Any]],
    links_index: Dict[str, Any],
    hops: int = 2,
) -> Dict[str, Any]:
    node_ids: set = set()
    modules: set = set()
    link_ids: set = set()

    for res in seed_resolutions:
        node = res.get("node") or {}
        nid = node.get("id")
        if nid:
            node_ids.add(nid)
        src = node.get("source_file") or res.get("sourceFile") or ""
        if src:
            parts = src.replace("\\", "/").split("/")
            if parts and parts[0]:
                modules.add(parts[0])
        for peer in res.get("callers", []) + res.get("callees", []):
            if peer:
                node_ids.add(peer)

    for _ in range(hops):
        frontier = list(node_ids)
        for nid in frontier:
            rels = links_index.get(nid, {})
            for edge in rels.get("outgoing", []):
                link_ids.add((nid, edge.get("target"), edge.get("relation")))
                if edge.get("target"):
                    node_ids.add(edge["target"])
            for edge in rels.get("incoming", []):
                link_ids.add((edge.get("source"), nid, edge.get("relation")))
                if edge.get("source"):
                    node_ids.add(edge["source"])

    return {
        "total_nodes": len(node_ids),
        "total_links": len(link_ids),
        "total_modules": len(modules),
        "scope": f"contract_seeds_plus_{hops}_hop",
    }


def _map_contract_and_requirements(
    contract: Dict[str, Any],
    requirements: Dict[str, Any],
    seed_resolutions: List[Dict[str, Any]],
    nodes_index: Dict[str, Any],
    links_index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map requirements modules/flows to contract seeds first, explicit mappings second, keyword last."""
    req_mappings = contract.get("requirement_mappings", {})
    module_map: Dict[str, List[str]] = req_mappings.get("modules", {})
    api_map: Dict[str, str] = req_mappings.get("apis", {})
    flow_map: Dict[str, List[str]] = req_mappings.get("flows", {})

    mapped_modules = []
    logical_view = requirements.get("hld_content", {}).get("2_logical_view", {})
    modules = logical_view.get("modules", [])

    for mod in modules:
        mod_name = mod.get("module_name", "")

        # 1. Explicit contract requirement_mappings
        explicit_seed_names = module_map.get(mod_name, [])
        matched_seeds = _seeds_by_names(explicit_seed_names, seed_resolutions)
        mapping_confidence = "explicit" if matched_seeds else "inferred"

        # 2. Keyword match on seed names/notes
        if not matched_seeds:
            matched_seeds = _seeds_matching_text(mod_name, seed_resolutions, limit=5)

        module_mappings = []
        seen_labels: set = set()
        for res in matched_seeds:
            node = res.get("node")
            if not node:
                continue
            m = _symbol_mapping_from_node(
                node,
                note=res.get("note", ""),
                extra_callers=res.get("callers"),
                extra_callees=res.get("callees"),
                links_index=links_index,
                mapping_confidence=mapping_confidence,
                is_new_capability=res.get("is_new_capability", False),
            )
            if m["codebase_symbol"] not in seen_labels:
                seen_labels.add(m["codebase_symbol"])
                module_mappings.append(m)

        if not module_mappings:
            for sym in _find_best_codebase_matches(mod_name, nodes_index, limit=3):
                module_mappings.append(_symbol_mapping_from_node(
                    sym, links_index=links_index, mapping_confidence="keyword_fallback"
                ))

        mapped_apis = []
        for api in mod.get("interfaces_and_apis", []):
            api_name = api.get("interface_name", "")
            sig = api.get("signature", "")
            sig_method = sig.split("(")[0].strip() if "(" in sig else sig
            if "." in sig_method:
                sig_method = sig_method.split(".")[-1]

            api_confidence = "inferred"
            api_seeds: List[Dict[str, Any]] = []
            if api_name in api_map:
                api_seeds = _seeds_by_names([api_map[api_name]], seed_resolutions)
                api_confidence = "explicit"
            if not api_seeds:
                api_seeds = _seeds_matching_text(
                    f"{api_name} {sig_method}", seed_resolutions, limit=3
                )
            api_mappings = []
            for res in api_seeds:
                node = res.get("node")
                if node:
                    api_mappings.append(_symbol_mapping_from_node(
                        node,
                        note=res.get("note", ""),
                        extra_callers=res.get("callers"),
                        extra_callees=res.get("callees"),
                        links_index=links_index,
                        mapping_confidence=api_confidence,
                        is_new_capability=res.get("is_new_capability", False),
                    ))
            if not api_mappings:
                for query_term in [sig_method, api_name]:
                    if query_term and len(query_term) > 2:
                        for asym in _find_best_codebase_matches(query_term, nodes_index, limit=3):
                            api_mappings.append(_symbol_mapping_from_node(
                                asym, links_index=links_index, mapping_confidence="keyword_fallback"
                            ))
                        if api_mappings:
                            break

            mapped_apis.append({
                "interface_name": api_name,
                "signature": sig,
                "codebase_mappings": api_mappings,
            })

        mapped_deps = []
        for dep in mod.get("dependencies", []):
            dep_seeds = _seeds_matching_text(dep, seed_resolutions, limit=1)
            if dep_seeds and dep_seeds[0].get("node"):
                node = dep_seeds[0]["node"]
                mapped_deps.append({
                    "dependency": dep,
                    "codebase_symbol": node.get("label", ""),
                    "source_file": node.get("source_file", ""),
                })
            else:
                dep_symbols = _find_best_codebase_matches(dep, nodes_index, limit=1)
                if dep_symbols:
                    dsym = dep_symbols[0]
                    mapped_deps.append({
                        "dependency": dep,
                        "codebase_symbol": dsym["label"],
                        "source_file": dsym.get("source_file", ""),
                    })

        mapped_modules.append({
            "module_name": mod_name,
            "codebase_mappings": module_mappings,
            "interfaces_and_apis": mapped_apis,
            "dependencies": mapped_deps,
        })

    mapped_flows = []
    for flow in logical_view.get("interactions_and_flows", []):
        flow_name = flow.get("flow_name", "")
        flow_confidence = "inferred"
        flow_seed_names = flow_map.get(flow_name, [])

        mapped_steps = []
        for step in flow.get("step_by_step_sequence", []):
            op = step.get("operation_signature", "")
            src = step.get("source_component", "")
            dst = step.get("destination_component", "")
            op_method = op.split("(")[0].strip() if "(" in op else op
            if "." in op_method:
                op_method = op_method.split(".")[-1]

            step_seeds: List[Dict[str, Any]] = []
            if flow_seed_names:
                step_seeds = _seeds_by_names(flow_seed_names, seed_resolutions)
                flow_confidence = "explicit"
            if not step_seeds:
                step_seeds = _seeds_matching_text(
                    f"{src} {dst} {op_method}", seed_resolutions, limit=2
                )
            step_mappings = []
            for res in step_seeds:
                node = res.get("node")
                if node:
                    step_mappings.append(_symbol_mapping_from_node(
                        node,
                        note=res.get("note", ""),
                        extra_callers=res.get("callers"),
                        extra_callees=res.get("callees"),
                        links_index=links_index,
                        mapping_confidence=flow_confidence,
                        is_new_capability=res.get("is_new_capability", False),
                    ))
            if not step_mappings and op_method and len(op_method) > 2:
                for osym in _find_best_codebase_matches(op_method, nodes_index, limit=2):
                    step_mappings.append(_symbol_mapping_from_node(
                        osym, links_index=links_index, mapping_confidence="keyword_fallback"
                    ))

            mapped_steps.append({
                "step_number": step.get("step_number"),
                "source_component": src,
                "destination_component": dst,
                "operation_signature": op,
                "codebase_mappings": step_mappings,
            })
        mapped_flows.append({
            "flow_name": flow_name,
            "steps": mapped_steps,
        })

    return {
        "mapped_modules": mapped_modules,
        "mapped_flows": mapped_flows,
    }

# ----------------------------------------------------------------------
# Helper: Mapping codebase to Confluence requirements (Token overlap)
# ----------------------------------------------------------------------
def _find_best_codebase_matches(
    name: str,
    nodes_index: Dict[str, Any],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    import re

    # Try exact match first
    chosen, _ = resolve_symbol(name, nodes_index)
    if chosen:
        return [chosen]

    parts = re.split(r'[\s_\-\.\/]+', name)
    keywords = []
    stopwords = {"api", "the", "and", "a", "of", "to", "in", "is", "for", "with", "on", "data", "application", "system", "service", "controller", "endpoint", "endpoints", "model", "prediction", "predictions", "flow", "flows", "event", "events"}

    for p in parts:
        c = canonical(p)
        if c and c not in stopwords and len(c) > 2:
            keywords.append(c)

    if not keywords:
        return []

    node_scores = {}
    for key, nds in nodes_index.items():
        key_canon = canonical(key)
        matched_count = 0
        unique_matches = 0
        for kw in keywords:
            if kw in key_canon:
                unique_matches += 1
                if kw == key_canon:
                    matched_count += 5
                else:
                    matched_count += 2
        if matched_count > 0:
            for n in nds:
                node_id = n["id"]
                has_src = 1 if n.get("source_file") else 0
                current_score = node_scores.get(node_id, (0, n))[0]
                new_score = current_score + (matched_count * (unique_matches ** 2)) + (has_src * 4)
                node_scores[node_id] = (new_score, n)

    sorted_nodes = sorted(node_scores.values(), key=lambda x: x[0], reverse=True)
    return [n for _, n in sorted_nodes[:limit]]


def _map_codebase_to_requirements(
    requirements: Dict[str, Any],
    nodes_index: Dict[str, Any],
) -> Dict[str, Any]:
    """Use agentic-orchestrator indices to map requirements to codebase symbols using keyword matching."""
    mapped_modules = []
    logical_view = requirements.get("hld_content", {}).get("2_logical_view", {})
    modules = logical_view.get("modules", [])

    for mod in modules:
        mod_name = mod.get("module_name", "")
        # Find best codebase class/interface symbols
        chosen_symbols = _find_best_codebase_matches(mod_name, nodes_index, limit=3)

        module_mappings = []
        for sym in chosen_symbols:
            base_class_res = find_base_class(sym["label"])
            base_class = [b["peer"] for b in base_class_res.get("results", [])]

            impl_interfaces_res = find_implemented_interfaces(sym["label"])
            interfaces = [i["peer"] for i in impl_interfaces_res.get("results", [])]

            methods_res = find_methods(sym["label"])
            methods_list = [m["peer"] for m in methods_res.get("results", [])]

            module_mappings.append({
                "codebase_symbol": sym["label"],
                "source_file": sym.get("source_file", ""),
                "note": sym.get("note", ""),
                "base_classes": base_class,
                "implemented_interfaces": interfaces,
                "methods": methods_list,
            })

        # Also resolve APIs/interfaces
        mapped_apis = []
        apis = mod.get("interfaces_and_apis", [])
        for api in apis:
            api_name = api.get("interface_name", "")
            sig = api.get("signature", "")

            sig_method = sig.split("(")[0].strip() if "(" in sig else sig
            if "." in sig_method:
                sig_method = sig_method.split(".")[-1]

            api_symbols = []
            for query_term in [sig_method, api_name]:
                if query_term and len(query_term) > 2:
                    api_symbols = _find_best_codebase_matches(query_term, nodes_index, limit=3)
                    if api_symbols:
                        break

            api_mappings = []
            for asym in api_symbols:
                api_mappings.append({
                    "codebase_symbol": asym["label"],
                    "source_file": asym.get("source_file", ""),
                    "callers": [c["peer"] for c in find_callers(asym["label"]).get("results", [])],
                    "callees": [c["peer"] for c in find_callees(asym["label"]).get("results", [])],
                })

            mapped_apis.append({
                "interface_name": api_name,
                "signature": sig,
                "codebase_mappings": api_mappings
            })

        # Resolve dependencies
        mapped_deps = []
        for dep in mod.get("dependencies", []):
            dep_symbols = _find_best_codebase_matches(dep, nodes_index, limit=1)
            if dep_symbols:
                dsym = dep_symbols[0]
                mapped_deps.append({
                    "dependency": dep,
                    "codebase_symbol": dsym["label"],
                    "source_file": dsym.get("source_file", "")
                })

        mapped_modules.append({
            "module_name": mod_name,
            "codebase_mappings": module_mappings,
            "interfaces_and_apis": mapped_apis,
            "dependencies": mapped_deps,
        })

    mapped_flows = []
    flows = logical_view.get("interactions_and_flows", [])
    for flow in flows:
        flow_name = flow.get("flow_name", "")
        steps = flow.get("step_by_step_sequence", [])
        mapped_steps = []
        for step in steps:
            op = step.get("operation_signature", "")
            src = step.get("source_component", "")
            dst = step.get("destination_component", "")

            op_method = op.split("(")[0].strip() if "(" in op else op
            if "." in op_method:
                op_method = op_method.split(".")[-1]

            op_symbols = []
            if op_method and len(op_method) > 2:
                op_symbols = _find_best_codebase_matches(op_method, nodes_index, limit=2)

            step_mappings = []
            for osym in op_symbols:
                step_mappings.append({
                    "codebase_symbol": osym["label"],
                    "source_file": osym.get("source_file", ""),
                    "callers": [c["peer"] for c in find_callers(osym["label"]).get("results", [])],
                    "callees": [c["peer"] for c in find_callees(osym["label"]).get("results", [])],
                })

            mapped_steps.append({
                "step_number": step.get("step_number"),
                "source_component": src,
                "destination_component": dst,
                "operation_signature": op,
                "codebase_mappings": step_mappings
            })
        mapped_flows.append({
            "flow_name": flow_name,
            "steps": mapped_steps
        })

    return {
        "mapped_modules": mapped_modules,
        "mapped_flows": mapped_flows,
    }


def _generate_codebase_summary_markdown(
    stats: Dict[str, Any],
    mapping: Dict[str, Any],
    requirements: Dict[str, Any],
    job_id: str,
    completed_at: str,
    seed_resolutions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Generate a markdown report of the overall extracted requirement-to-codebase info."""
    project_name = requirements.get("project_name", "Untitled Product")
    lines = []
    lines.append(f"# Codebase & Requirements Mapping: {project_name}")
    lines.append(f"**Job ID:** `{job_id}` | **Generated:** {completed_at}  \n")
    lines.append("---")
    lines.append("## 1. Scoped Graph Statistics")
    lines.append(f"- **Scoped Nodes (seeds + 1-hop):** {stats.get('total_nodes', 0)}")
    lines.append(f"- **Scoped Links:** {stats.get('total_links', 0)}")
    lines.append(f"- **In-Scope Projects:** {stats.get('total_modules', 0)}\n")

    if seed_resolutions:
        lines.append("## 2. Contract Seed Symbol Resolutions")
        for res in seed_resolutions:
            status = "resolved" if res.get("resolved") else "unresolved"
            new_tag = " [NEW]" if res.get("is_new_capability") else ""
            node = res.get("node") or {}
            lines.append(f"### {res.get('name', 'unknown')} [{status}]{new_tag}")
            if node:
                lines.append(f"- **Graph ID:** `{res.get('graphId') or 'n/a'}`")
                lines.append(f"- **Label:** `{node.get('label', '')}`")
                lines.append(f"- **Source File:** `{node.get('source_file') or res.get('sourceFile') or 'external'}`")
            if res.get("callers"):
                lines.append(f"- **Callers:** {', '.join(f'`{c}`' for c in res['callers'][:3])}")
            if res.get("callees"):
                lines.append(f"- **Callees:** {', '.join(f'`{c}`' for c in res['callees'][:3])}")
            lines.append("")

    lines.append("## 3. Requirements-to-Codebase Mapping")

    mapped_modules = mapping.get("mapped_modules", [])
    if not mapped_modules:
        lines.append("No module mapping could be established or no requirements document was available.\n")
    else:
        for mod in mapped_modules:
            lines.append(f"### Module: {mod['module_name']}")
            cb_list = mod.get("codebase_mappings", [])
            if cb_list:
                for idx, cb in enumerate(cb_list):
                    lines.append(f"#### Match #{idx+1}: `{cb.get('codebase_symbol')}`")
                    lines.append(f"- **Source File:** `{cb.get('source_file')}`" if cb.get("source_file") else "- **Source File:** (external/framework)")
                    if cb.get("base_classes"):
                        lines.append(f"- **Inherits From:** {', '.join([f'`{b}`' for b in cb['base_classes']])}")
                    if cb.get("implemented_interfaces"):
                        lines.append(f"- **Implements:** {', '.join([f'`{i}`' for i in cb['implemented_interfaces']])}")
                    if cb.get("methods"):
                        lines.append(f"- **Declared Methods:** {', '.join([f'`{m}`' for m in cb['methods'][:10]])}")
                        if len(cb["methods"]) > 10:
                            lines.append(f"  *... and {len(cb['methods']) - 10} more*")
            else:
                lines.append("- *No direct codebase class mapping found in code graph index.*")

            apis = mod.get("interfaces_and_apis", [])
            if apis:
                lines.append("\n**APIs & Interfaces Mapping:**")
                for api in apis:
                    lines.append(f"- **Interface:** {api['interface_name']} (`{api['signature']}`)")
                    acb_list = api.get("codebase_mappings", [])
                    if acb_list:
                        for acb in acb_list:
                            lines.append(f"  - **Mapped Symbol:** `{acb.get('codebase_symbol')}` (in `{acb.get('source_file') or 'external'}`)")
                            if acb.get("callers"):
                                lines.append(f"    - **Called by:** {', '.join([f'`{c}`' for c in acb['callers'][:3]])}")
                            if acb.get("callees"):
                                lines.append(f"    - **Calls:** {', '.join([f'`{c}`' for c in acb['callees'][:3]])}")
                    else:
                        lines.append("  - *No codebase method mapping found.*")

            deps = mod.get("dependencies", [])
            if deps:
                lines.append("\n**Dependencies Mapping:**")
                for dep in deps:
                    lines.append(f"- `{dep.get('dependency')}` -> `{dep.get('codebase_symbol')}` (`{dep.get('source_file') or 'external'}`)")
            lines.append("")

    mapped_flows = mapping.get("mapped_flows", [])
    if mapped_flows:
        lines.append("## 4. Interaction & Flow Tracing")
        for flow in mapped_flows:
            lines.append(f"### Flow: {flow['flow_name']}")
            lines.append("| Step | Source | Destination | Operation | Codebase Mapped Symbols | Details / Call Graph |")
            lines.append("|---|---|---|---|---|---|")
            for step in flow.get("steps", []):
                scb_list = step.get("codebase_mappings", [])
                num = step.get("step_number", "")
                src = step.get("source_component", "")
                dst = step.get("destination_component", "")
                op = step.get("operation_signature", "")
                if scb_list:
                    symbols_str = "<br/>".join([f"`{s.get('codebase_symbol')}`" for s in scb_list])
                    details_list = []
                    for s in scb_list:
                        d = f"File: `{s.get('source_file') or 'external'}`"
                        if s.get("callers"):
                            d += f"<br/>Called by: {', '.join([f'`{c}`' for c in s['callers'][:2]])}"
                        details_list.append(d)
                    details_str = "<hr/>".join(details_list)
                else:
                    symbols_str = "*Unresolved*"
                    details_str = ""
                lines.append(f"| {num} | {src} | {dst} | `{op}` | {symbols_str} | {details_str} |")
            lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Monolith graph indexing
# ----------------------------------------------------------------------
def _ensure_graph_indexed(graph_path: str) -> Dict[str, Any]:
    """Load monolith graph.json and ensure query indices are built."""
    print(f"Loading monolith graph from {graph_path}...")
    with open(graph_path, "r", encoding="utf-8") as fh:
        graph = json.load(fh)

    # Only normalize legacy seed-only graphs (not the full Graphify export)
    if normalize_graph is not None and graph.get("seedSymbols") and not graph.get("nodes"):
        graph = normalize_graph(graph)
        print(f"  Normalized seedSymbols -> {len(graph.get('nodes', []))} indexable nodes")

    nodes = graph.get("nodes", [])
    links = graph.get("links", [])

    modules_set: set = set()
    for n in nodes:
        src = n.get("source_file")
        if src:
            parts = src.replace("\\", "/").split("/")
            if parts and parts[0]:
                modules_set.add(parts[0])

    monolith_stats = {
        "monolith_nodes": len(nodes),
        "monolith_links": len(links),
        "monolith_modules": len(modules_set),
    }

    orchestrator_dir = _workspace_root() / "agentic-orchestrator"
    nodes_index_path = orchestrator_dir / "index" / "nodes_index.json"
    links_index_path = orchestrator_dir / "index" / "links_index.json"

    need_rebuild = False
    if not nodes_index_path.exists() or not links_index_path.exists():
        need_rebuild = True
    else:
        graph_mtime = os.path.getmtime(graph_path)
        nodes_mtime = os.path.getmtime(str(nodes_index_path))
        links_mtime = os.path.getmtime(str(links_index_path))
        if graph_mtime > min(nodes_mtime, links_mtime):
            need_rebuild = True

    if need_rebuild:
        print("Precomputed query indices are missing or outdated. Rebuilding indices...")
        sys.path.insert(0, str(orchestrator_dir))
        try:
            import build_index  # type: ignore
            build_index.build_index(Path(graph_path), orchestrator_dir / "index")
            print("Successfully rebuilt indices.")
        except Exception as e:
            print(f"Error rebuilding indices: {e}", file=sys.stderr)
            raise
        finally:
            if str(orchestrator_dir) in sys.path:
                sys.path.remove(str(orchestrator_dir))

    return monolith_stats


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------
def analyze_codebase(
    *,
    contract_path: Optional[str] = None,
    ticket: Optional[str] = None,
    graph_path: Optional[str] = None,
    source_path: Optional[str] = None,
    artifact_dir: Optional[str] = None,
) -> CodebaseAnalysisResult:
    """Resolve a feature contract against the monolith graph and requirements.json."""
    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.utcnow().isoformat()

    graph_override = graph_path or source_path
    resolved_graph = _resolve_graph_path(graph_override)
    monolith_stats = _ensure_graph_indexed(resolved_graph)

    out_dir = artifact_dir or os.getenv("ARTIFACT_DIR", "./artifacts")
    os.makedirs(out_dir, exist_ok=True)

    req_path = os.path.join(out_dir, "requirements.json")
    requirements: Dict[str, Any] = {}
    if os.path.isfile(req_path):
        print(f"Loading Confluence requirements from {req_path}...")
        with open(req_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
            requirements = payload.get("requirements", payload)

    resolved_contract = _resolve_contract_path(contract_path, ticket, requirements)
    contract = _load_contract(resolved_contract)
    print(f"Loaded feature contract: {resolved_contract}")

    orchestrator_dir = str(_workspace_root() / "agentic-orchestrator")
    if orchestrator_dir not in sys.path:
        sys.path.insert(0, orchestrator_dir)

    try:
        nodes_index, links_index = load_indexes()
        seed_resolutions = _resolve_contract_seeds(contract, nodes_index, links_index)
        resolved_count = sum(1 for s in seed_resolutions if s.get("resolved"))
        print(f"Resolved {resolved_count}/{len(seed_resolutions)} contract seed symbols.")

        mapping_res: Dict[str, Any] = {}
        if requirements:
            print("Mapping contract seeds + requirements...")
            mapping_res = _map_contract_and_requirements(
                contract, requirements, seed_resolutions, nodes_index, links_index
            )
        else:
            print("Warning: No requirements.json found; mapping will be seed-only.", file=sys.stderr)

        scoped = _scoped_stats(seed_resolutions, links_index)
    finally:
        if orchestrator_dir in sys.path:
            sys.path.remove(orchestrator_dir)

    completed_at = datetime.utcnow().isoformat()

    target_projects = contract.get("targetProjects", [])
    modules_list = [{"module": p} for p in target_projects]

    raw: Dict[str, Any] = {
        "stats": scoped,
        "monolith_stats": monolith_stats,
        "modules": modules_list,
        "graph_format": "graphify",
        "contract": {
            "ticket": contract.get("ticket"),
            "title": contract.get("title"),
            "contract_path": resolved_contract,
            "graph_path": resolved_graph,
        },
        "target_projects": target_projects,
        "seed_resolutions": seed_resolutions,
        "acceptance_criteria": contract.get("acceptanceCriteria", []),
        "constraints": contract.get("constraints", []),
        "resolved_at_checkpoint_b": contract.get("resolvedAtCheckpointB", []),
        "out_of_scope": contract.get("outOfScope", []),
        "unresolved": contract.get("unresolved", []),
        "requirement_mappings": contract.get("requirement_mappings", {}),
        "mapping": mapping_res,
    }

    md_content = _generate_codebase_summary_markdown(
        raw["stats"], mapping_res, requirements, job_id, completed_at, seed_resolutions
    )
    md_path = os.path.join(out_dir, "codebase_summary.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md_content)
    print(f"Generated codebase mapping document at {md_path}")

    artifact_path = os.path.join(out_dir, "code_graph.json")
    payload = {
        "job_id": job_id,
        "graph_path": resolved_graph,
        "contract_path": resolved_contract,
        "started_at": started_at,
        "completed_at": completed_at,
        "analyzer": "contract_graph_index",
        "code_graph": raw,
    }
    with open(artifact_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    return CodebaseAnalysisResult(
        job_id=job_id,
        source_path=resolved_graph,
        contract_path=resolved_contract,
        started_at=started_at,
        completed_at=completed_at,
        code_graph=raw,
        artifact_path=artifact_path,
    )

