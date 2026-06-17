"""
MDD module catalog — discover logical modules from three sources only:

  1. requirements.json  → hld_content.2_logical_view.modules[].module_name
  2. HLD.md             → ### 2.N {name} Logical View headings
  3. code_graph.mapping → mapped_modules[].module_name

Enrichment per module still uses requirements slice, HLD excerpt, and code_graph mapping.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .mdd_template import slugify_module_name


@dataclass
class ModuleCatalogResult:
    job_id: str
    ticket: Optional[str]
    modules: List[Dict[str, Any]]
    catalog_warnings: List[str]
    artifact_path: str
    started_at: str
    completed_at: str


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _project_from_source_file(source_file: str) -> str:
    if not source_file:
        return ""
    parts = source_file.replace("\\", "/").split("/")
    return parts[0] if parts else ""


def _parse_hld_modules(hld_markdown: str) -> List[Dict[str, str]]:
    """Extract ### 2.N {name} Logical View headings from HLD."""
    pattern = re.compile(
        r"^###\s+2\.(\d+)\s+(.+?)\s+Logical View\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    return [
        {"section": f"2.{m.group(1)}", "logical_name": m.group(2).strip()}
        for m in pattern.finditer(hld_markdown)
    ]


def _extract_hld_section(hld_markdown: str, logical_name: str) -> str:
    esc = re.escape(logical_name)
    pattern = re.compile(
        rf"(###\s+2\.\d+\s+{esc}\s+Logical View\s*\n)(.*?)(?=\n###\s+2\.|\n##\s+|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(hld_markdown)
    return m.group(2).strip() if m else ""


def _find_mapping_for_module(
    module_name: str,
    mapped_modules: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    name_lower = module_name.lower()
    for mod in mapped_modules:
        if (mod.get("module_name") or "").lower() == name_lower:
            return mod
    for mod in mapped_modules:
        mn = (mod.get("module_name") or "").lower()
        if name_lower in mn or mn in name_lower:
            return mod
    return None


def _symbols_from_mapping(mapping: Optional[Dict[str, Any]]) -> List[str]:
    if not mapping:
        return []
    symbols: List[str] = []
    for cb in mapping.get("codebase_mappings") or []:
        sym = cb.get("codebase_symbol", "")
        if sym and sym not in symbols:
            symbols.append(sym)
    for api in mapping.get("interfaces_and_apis") or []:
        for acb in api.get("codebase_mappings") or []:
            sym = acb.get("codebase_symbol", "")
            if sym and sym not in symbols:
                symbols.append(sym)
    return symbols


def _target_projects_from_mapping(
    mapping: Optional[Dict[str, Any]],
    all_target_projects: List[str],
) -> List[str]:
    if not mapping:
        return []
    projects: Set[str] = set()
    for cb in mapping.get("codebase_mappings") or []:
        p = _project_from_source_file(cb.get("source_file", ""))
        if p:
            projects.add(p)
    for api in mapping.get("interfaces_and_apis") or []:
        for acb in api.get("codebase_mappings") or []:
            p = _project_from_source_file(acb.get("source_file", ""))
            if p:
                projects.add(p)
    if all_target_projects:
        projects &= set(all_target_projects)
    return sorted(projects)


def _filter_flows_for_module(
    flows: List[Dict[str, Any]],
    module_name: str,
    symbols: List[str],
) -> List[Dict[str, Any]]:
    name_lower = module_name.lower()
    sym_tokens = {s.lower().replace(".", "") for s in symbols}
    matched = []
    for flow in flows:
        flow_name = (flow.get("flow_name") or "").lower()
        if name_lower in flow_name:
            matched.append(flow)
            continue
        for step in flow.get("step_by_step_sequence") or []:
            blob = " ".join([
                step.get("source_component", ""),
                step.get("destination_component", ""),
                step.get("operation_signature", ""),
            ]).lower()
            if name_lower in blob:
                matched.append(flow)
                break
            if any(t in blob for t in sym_tokens if len(t) > 3):
                matched.append(flow)
                break
    return matched


def _union_module_names(*name_lists: List[str]) -> List[str]:
    """Case-insensitive dedupe preserving first-seen casing."""
    all_names: List[str] = []
    seen: Set[str] = set()
    for names in name_lists:
        for name in names:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                all_names.append(name.strip())
    return all_names


def _requirements_module_by_name(
    modules: List[Dict[str, Any]],
    logical_name: str,
) -> Optional[Dict[str, Any]]:
    nl = logical_name.lower()
    for mod in modules:
        if (mod.get("module_name") or "").lower() == nl:
            return mod
    for mod in modules:
        mn = (mod.get("module_name") or "").lower()
        if nl in mn or mn in nl:
            return mod
    return None


def build_module_catalog(
    *,
    artifact_dir: Optional[str] = None,
    requirements_path: Optional[str] = None,
    hld_path: Optional[str] = None,
    code_graph_path: Optional[str] = None,
    ticket: Optional[str] = None,
) -> ModuleCatalogResult:
    """Build and persist the MDD module catalog from latest pipeline artifacts."""
    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.utcnow().isoformat()
    out_dir = artifact_dir or os.getenv("ARTIFACT_DIR", "./artifacts")
    os.makedirs(out_dir, exist_ok=True)

    req_path = requirements_path or os.path.join(out_dir, "requirements.json")
    hld_file = hld_path or os.path.join(out_dir, "HLD.md")
    cg_path = code_graph_path or os.path.join(out_dir, "code_graph.json")

    if not os.path.isfile(req_path):
        raise FileNotFoundError(f"requirements.json not found at {req_path}")
    if not os.path.isfile(hld_file):
        raise FileNotFoundError(f"HLD.md not found at {hld_file}. Run HLD generation first.")

    req_payload = _load_json(req_path)
    requirements = req_payload.get("requirements", req_payload)
    hld_markdown = Path(hld_file).read_text(encoding="utf-8")

    code_graph: Dict[str, Any] = {}
    if os.path.isfile(cg_path):
        cg_payload = _load_json(cg_path)
        code_graph = cg_payload.get("code_graph", cg_payload)

    resolved_ticket = ticket or code_graph.get("contract", {}).get("ticket")
    if not resolved_ticket:
        resolved_ticket = requirements.get("ticket")

    req_modules = requirements.get("hld_content", {}).get("2_logical_view", {}).get("modules", [])
    req_names = [m.get("module_name", "") for m in req_modules if m.get("module_name")]
    hld_modules = _parse_hld_modules(hld_markdown)
    hld_names = [m["logical_name"] for m in hld_modules]

    mapped_modules = code_graph.get("mapping", {}).get("mapped_modules", [])
    cg_names = [m.get("module_name", "") for m in mapped_modules if m.get("module_name")]

    all_names = _union_module_names(req_names, hld_names, cg_names)

    catalog_warnings: List[str] = []
    req_set = {n.lower() for n in req_names if n}
    hld_set = {n.lower() for n in hld_names if n}
    cg_set = {n.lower() for n in cg_names if n}

    only_req = req_set - hld_set
    only_hld = hld_set - req_set
    only_cg = cg_set - req_set - hld_set

    if only_req:
        catalog_warnings.append(
            f"Modules in requirements.json but not in HLD §2 headings: {sorted(only_req)}"
        )
    if only_hld:
        catalog_warnings.append(
            f"Modules in HLD §2 headings but not in requirements.json: {sorted(only_hld)}"
        )
    if only_cg:
        catalog_warnings.append(
            f"Modules in code_graph.mapping but not in requirements/HLD: {sorted(only_cg)}"
        )
    all_target_projects = code_graph.get("target_projects", [])
    all_flows = requirements.get("hld_content", {}).get("2_logical_view", {}).get(
        "interactions_and_flows", []
    )

    hld_section_by_name = {m["logical_name"].lower(): m["section"] for m in hld_modules}

    catalog_modules: List[Dict[str, Any]] = []
    for logical_name in all_names:
        name_lower = logical_name.lower()
        req_mod = _requirements_module_by_name(req_modules, logical_name)
        mapping = _find_mapping_for_module(logical_name, mapped_modules)
        symbols = _symbols_from_mapping(mapping)
        target_projects = _target_projects_from_mapping(mapping, all_target_projects)
        filtered_flows = _filter_flows_for_module(all_flows, logical_name, symbols)
        hld_excerpt = _extract_hld_section(hld_markdown, logical_name)

        arch_layer = ""
        if req_mod:
            arch_layer = req_mod.get("architectural_layer", "")

        name_lower = logical_name.lower()
        catalog_modules.append({
            "id": slugify_module_name(logical_name),
            "logical_name": logical_name,
            "slug": slugify_module_name(logical_name),
            "hld_section": hld_section_by_name.get(name_lower, ""),
            "architectural_layer": arch_layer,
            "summary": (req_mod or {}).get("detailed_responsibility", "")[:300],
            "target_projects": target_projects,
            "primary_symbols": symbols[:20],
            "flow_count": len(filtered_flows),
            "has_hld_section": bool(hld_excerpt),
            "has_code_mapping": bool(mapping and symbols),
            "in_requirements": name_lower in req_set,
            "in_hld": name_lower in hld_set,
            "in_code_graph": name_lower in cg_set,
            "dependency_only": False,
        })

    completed_at = datetime.utcnow().isoformat()
    artifact_path = os.path.join(out_dir, "mdd_modules.json")
    payload = {
        "job_id": job_id,
        "ticket": resolved_ticket,
        "started_at": started_at,
        "completed_at": completed_at,
        "catalog_source": "requirements.json + HLD.md + code_graph.mapping",
        "catalog_warnings": catalog_warnings,
        "module_count": len(catalog_modules),
        "modules": catalog_modules,
        "hld_path": os.path.abspath(hld_file),
        "requirements_path": os.path.abspath(req_path),
        "code_graph_path": os.path.abspath(cg_path) if os.path.isfile(cg_path) else None,
    }
    with open(artifact_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    return ModuleCatalogResult(
        job_id=job_id,
        ticket=resolved_ticket,
        modules=catalog_modules,
        catalog_warnings=catalog_warnings,
        artifact_path=artifact_path,
        started_at=started_at,
        completed_at=completed_at,
    )


def load_module_catalog(artifact_dir: Optional[str] = None) -> Dict[str, Any]:
    out_dir = artifact_dir or os.getenv("ARTIFACT_DIR", "./artifacts")
    path = os.path.join(out_dir, "mdd_modules.json")
    if not os.path.isfile(path):
        result = build_module_catalog(artifact_dir=out_dir)
        return _load_json(result.artifact_path)
    return _load_json(path)


def get_catalog_module_names(catalog: Dict[str, Any]) -> List[str]:
    return [m["logical_name"] for m in catalog.get("modules", [])]
