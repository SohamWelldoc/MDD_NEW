"""
MDD Generator — one SOP-036 Module Detail Design per selected logical module.

Inputs: requirements.json, HLD.md, code_graph.json, module catalog.
Output: artifacts/mdd/MDD_{slug}_{ticket}.md per module.

Diagram pipeline (same pattern as HLD):
  LLM generates ```mermaid blocks → sanitize → validate → deterministic fallback.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import get_llm_client
from .mdd_module_catalog import (
    _extract_hld_section,
    _filter_flows_for_module,
    _find_mapping_for_module,
    _requirements_module_by_name,
    _symbols_from_mapping,
    _target_projects_from_mapping,
    build_module_catalog,
    get_catalog_module_names,
    load_module_catalog,
)
from .mdd_template import (
    MDD_QUALITY_RULES,
    MDD_SECTION_CONTRACT,
    MDD_SECTIONS,
    PLAN_JSON_SCHEMA,
    markdown_table_cell,
    mdd_section_has_content,
    normalize_mdd_plan,
    slugify_module_name,
)
from .mdd_mermaid_builders import (
    build_architecture_flowchart,
    build_class_diagram,
    build_sequence_diagram,
    build_use_case_flowchart,
    resolve_class_name,
)
from .mermaid_utils import (
    is_valid_mermaid_block,
    postprocess_mermaid,
    sanitize_mermaid_block,
    validate_diagrams,
)


@dataclass
class MDDModuleResult:
    module_name: str
    slug: str
    artifact_path: str
    plan: Dict[str, Any]
    sections_included: List[str]
    sections_skipped: List[str]


@dataclass
class MDDGenerateResult:
    job_id: str
    ticket: Optional[str]
    started_at: str
    completed_at: str
    generated: List[MDDModuleResult]
    manifest_path: str


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_MDD_SYSTEM = (
    "You are a principal software architect writing a Module Detail Design (MDD) "
    "document per SOP-036 for ONE module only. Ground every claim in the provided "
    "module context bundle. Do NOT invent class names, APIs, or infrastructure. "
    "Use ONLY symbols listed in the bundle. If data is missing, do not add filler. "
    "Output markdown only — no document title or chat preamble."
)

_PLANNER_SYSTEM = (
    "You are deciding which MDD sections to include for one module. "
    "Return STRICT JSON only matching the schema."
)

_MDD_DIAGRAM_SYSTEM = (
    "You are a principal software architect writing Mermaid diagrams for a Module Detail Design "
    "following the same rules as the HLD generator. Ground every node and message in the provided "
    "module context bundle. Do NOT invent class names, methods, or APIs."
    "\nOutput a single ```mermaid fenced block only — no prose before or after."
    "\nCRITICAL RULES (same as HLD):\n"
    "1. sequenceDiagram: participant IDs MUST be exact mapped codebase symbols "
    "(e.g. MealController, FoodController, DiabetesElogWorkflow). "
    "Use aliases: participant MealController as \"Meal Controller\". "
    "Never use logical module names like FoodModule or CGMConnectionService as IDs.\n"
    "2. classDiagram: class names must be valid identifiers (letters/digits/underscore). "
    "Never use method names or symbols starting with '.' as class names. "
    "Limit to mapped classes; show at most 5 key methods per class.\n"
    "3. flowchart TD: do NOT use the 'participant' keyword. "
    "Node IDs must be valid identifiers — use quoted labels for display: "
    "MealController[\"Meal Controller\"].\n"
    "4. Never use bare `.Method()` as a node or class name.\n"
    "5. Quote edge labels that contain parentheses: -->|\"analyzeCarbContent(mealData)\"| Target.\n"
    "6. Use ONLY symbols from the allowlist."
)


def _diagram_context_block(bundle: Dict[str, Any]) -> str:
    """Rich context for diagram LLM prompts (mirrors HLD codebase context pattern)."""
    parts = [
        "=== MODULE CONTEXT ===",
        json.dumps({
            "logical_name": bundle.get("logical_name"),
            "target_projects": bundle.get("target_projects"),
            "primary_symbols": bundle.get("primary_symbols"),
            "dependencies_in": bundle.get("dependencies_in"),
            "capabilities": (bundle.get("requirements_module") or {}).get("capabilities"),
        }, indent=2, ensure_ascii=False)[:4000],
    ]
    hld_excerpt = (bundle.get("hld_excerpt") or "").strip()
    if hld_excerpt:
        parts.extend(["", "=== HLD LOGICAL VIEW EXCERPT ===", hld_excerpt[:4000]])
    flows = bundle.get("filtered_flows") or []
    if flows:
        parts.extend(["", "=== FLOWS ===", json.dumps(flows, indent=2, ensure_ascii=False)[:5000]])
    mapping = bundle.get("mapping") or {}
    cbs = mapping.get("codebase_mappings") or []
    component_evidence = bundle.get("component_evidence") or []
    if component_evidence:
        parts.extend([
            "",
            "=== COMPONENT EVIDENCE ===",
            json.dumps(component_evidence, indent=2, ensure_ascii=False)[:7000],
        ])
    elif cbs:
        parts.extend([
            "",
            "=== COMPONENT MAPPINGS ===",
            json.dumps(cbs, indent=2, ensure_ascii=False)[:6000],
        ])
    return "\n".join(parts)


def _finalize_diagram_block(block: str) -> str:
    """Sanitize a diagram body; return empty string if still invalid."""
    if not block or not block.strip():
        return ""
    cleaned = sanitize_mermaid_block(block)
    return cleaned if is_valid_mermaid_block(cleaned) else ""


def _llm_diagram(
    llm,
    prompt: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2500,
) -> str:
    """Single LLM call → extract mermaid block body."""
    try:
        raw = llm.chat(_MDD_DIAGRAM_SYSTEM, prompt, temperature=temperature, max_tokens=max_tokens)
        return _extract_mermaid_block(raw)
    except Exception:  # noqa: BLE001
        return ""


def _resolve_diagram(
    *,
    llm_block: str,
    hld_block: str = "",
    fallback_block: str = "",
) -> Tuple[str, str]:
    """HLD-style pipeline: prefer module-specific LLM, then HLD reuse, then fallback."""
    for source, block in (
        ("llm", llm_block),
        ("hld", hld_block),
        ("fallback", fallback_block),
    ):
        finalized = _finalize_diagram_block(block)
        if finalized:
            return finalized, source
    return "", "none"


def _class_diagram_has_unexpected_classes(block: str, bundle: Dict[str, Any]) -> bool:
    allowed = {
        item.get("class_name", "")
        for item in bundle.get("component_evidence") or []
        if item.get("class_name")
    }
    for item in bundle.get("component_evidence") or []:
        allowed.update(item.get("base_classes") or [])
        allowed.update(item.get("implemented_interfaces") or [])
    allowed.update({"ControllerBase", "WorkflowBase"})
    allowed_lower = {a.lower() for a in allowed if a}
    for cls in re.findall(r"^\s*class\s+([A-Za-z_]\w*)", block or "", re.MULTILINE):
        if cls.lower() not in allowed_lower:
            return True
    return False


def generate_mdd_diagrams(
    llm,
    bundle: Dict[str, Any],
    include: Dict[str, Any],
    *,
    temperature: float = 0.2,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Generate Mermaid diagrams: LLM first (like HLD), sanitize, validate, deterministic fallback."""
    diagrams: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    allowlist = _allowed_symbols_for_bundle(bundle)
    module = bundle.get("logical_name", "Module")
    flows = bundle.get("filtered_flows") or []
    mapping = bundle.get("mapping") or {}
    has_components = bool(mapping.get("codebase_mappings"))
    context = _diagram_context_block(bundle)
    hld_sequences = bundle.get("sequence_diagrams") or []

    if include.get("module_architecture") and (
        bundle.get("primary_symbols") or bundle.get("target_projects") or has_components
    ):
        # Flowcharts are deterministic by default to avoid unreadable long edge labels.
        block, source = _resolve_diagram(
            llm_block="",
            fallback_block=build_architecture_flowchart(bundle),
        )
        if block:
            diagrams["architecture"] = block
            sources["architecture"] = source

    if include.get("use_case_flow") and flows:
        # Use short numbered edges; full operation signatures are listed in the table below.
        block, source = _resolve_diagram(
            llm_block="",
            fallback_block=build_use_case_flowchart(bundle),
        )
        if block:
            diagrams["use_case"] = block
            sources["use_case"] = source

    if include.get("component_design") and has_components:
        # Deterministic class diagrams preserve valid class names and relationship links.
        block, source = _resolve_diagram(
            llm_block="",
            fallback_block=build_class_diagram(bundle),
        )
        if block:
            diagrams["class"] = block
            sources["class"] = source

    if include.get("sequence_flow"):
        hld_block = hld_sequences[0].strip() if hld_sequences else ""
        llm_block = ""
        if flows or has_components:
            prompt = "\n".join([
                f"Generate a ```mermaid sequenceDiagram``` for module '{module}'.",
                "Use exact mapped codebase symbols as participant IDs with aliases.",
                "Model the primary flow steps as messages between participants.",
                allowlist,
                context,
                "Output only the mermaid fenced block.",
            ])
            llm_block = _llm_diagram(llm, prompt, temperature=temperature)
        block, source = _resolve_diagram(
            llm_block=llm_block,
            hld_block=hld_block,
            fallback_block=build_sequence_diagram(bundle),
        )
        if block:
            diagrams["sequence"] = block
            sources["sequence"] = source

    return diagrams, sources


def _allowed_symbols_for_bundle(bundle: Dict[str, Any]) -> str:
    symbols: List[str] = []
    mapping = bundle.get("mapping") or {}

    def _add(name: str) -> None:
        if name and name not in symbols:
            symbols.append(name)

    for sym in bundle.get("primary_symbols") or []:
        _add(sym)
    for item in bundle.get("component_evidence") or []:
        _add(item.get("normalized_symbol", ""))
        _add(item.get("class_name", ""))
        for rel in item.get("related_symbols") or []:
            _add(rel.get("normalized_symbol", ""))
            _add(rel.get("class_name", ""))
    for cb in mapping.get("codebase_mappings") or []:
        sym = cb.get("codebase_symbol", "")
        src = cb.get("source_file", "")
        _add(sym)
        cls = resolve_class_name(sym, src)
        if cls:
            _add(cls)
    for api in mapping.get("interfaces_and_apis") or []:
        for acb in api.get("codebase_mappings") or []:
            sym = acb.get("codebase_symbol", "")
            src = acb.get("source_file", "")
            _add(sym)
            cls = resolve_class_name(sym, src)
            if cls:
                _add(cls)
    if not symbols:
        return ""
    return "ALLOWED CODEBASE SYMBOLS (use ONLY these):\n" + ", ".join(f"`{s}`" for s in symbols[:40])


def _extract_mermaid_block(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```mermaid\s*\n(.*?)```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    if raw.startswith("sequenceDiagram") or raw.startswith("classDiagram") or raw.startswith("flowchart"):
        return raw.strip()
    return ""


def _mermaid_fence(diagram: str) -> str:
    if not diagram or not diagram.strip():
        return ""
    return "```mermaid\n" + diagram.strip() + "\n```"


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _coerce_json(raw: str) -> Dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(raw)
        if not m:
            raise
        return json.loads(m.group(0))


def _class_from_source_file(source_file: str) -> str:
    if not source_file:
        return ""
    name = Path(source_file.replace("\\", "/")).name
    return name[:-3] if name.endswith(".cs") else name


def _split_composite_symbol(symbol: str) -> List[str]:
    """Split composite mapping labels while preserving simple symbols."""
    if not symbol:
        return []
    parts = re.split(r"\s+\+\s+|\s*,\s*|\s*/\s*", symbol)
    return [p.strip() for p in parts if p.strip()]


def _normalize_code_symbol(symbol: str, source_file: str = "") -> Dict[str, str]:
    """Return generic, MDD-safe symbol metadata for code_graph mappings."""
    raw = (symbol or "").strip()
    source_file = source_file or ""
    source_class = _class_from_source_file(source_file)

    if raw.startswith("."):
        normalized = f"{source_class}{raw}" if source_class else raw.lstrip(".")
        return {
            "raw_symbol": raw,
            "normalized_symbol": normalized,
            "class_name": source_class,
            "method_name": raw.lstrip(".").replace("()", ""),
            "source_file": source_file,
        }

    class_name = resolve_class_name(raw, source_file) or source_class
    method_name = ""
    if "." in raw and not raw.startswith("."):
        method_name = raw.split(".", 1)[1].replace("()", "")

    return {
        "raw_symbol": raw,
        "normalized_symbol": raw,
        "class_name": class_name or raw,
        "method_name": method_name,
        "source_file": source_file,
    }


def _normalize_mapping_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one code_graph mapping entry without assuming a specific feature."""
    raw_symbol = entry.get("codebase_symbol", "")
    source_file = entry.get("source_file", "")
    normalized = _normalize_code_symbol(raw_symbol, source_file)
    if entry.get("normalized_symbol"):
        normalized["normalized_symbol"] = entry.get("normalized_symbol", "")
    if entry.get("class_name"):
        normalized["class_name"] = entry.get("class_name", "")
    if entry.get("method_name"):
        normalized["method_name"] = entry.get("method_name", "")
    related_symbols = []
    for part in _split_composite_symbol(raw_symbol):
        if part == raw_symbol:
            continue
        part_norm = _normalize_code_symbol(part, source_file)
        related_symbols.append(part_norm)

    methods = entry.get("method_impacts") or entry.get("methods") or []
    method_impacts = []
    for method in methods[:12]:
        if isinstance(method, dict):
            raw_method = method.get("method") or method.get("method_name") or method.get("display_name") or ""
            display = method.get("method_name") or method.get("display_name") or raw_method
        else:
            raw_method = str(method)
            display = raw_method
        short = display.split("_")[-1] if "_" in display else display
        method_impacts.append({
            "method": raw_method,
            "display_name": short.replace("()", ""),
        })

    return {
        **normalized,
        "base_classes": entry.get("base_classes") or [],
        "implemented_interfaces": entry.get("implemented_interfaces") or [],
        "methods": method_impacts,
        "dtos": entry.get("dtos") or [],
        "callers": entry.get("callers") or [],
        "callees": entry.get("callees") or [],
        "source_location": entry.get("source_location", ""),
        "note": entry.get("note", ""),
        "mapping_confidence": entry.get("mapping_confidence", ""),
        "is_new_capability": bool(entry.get("is_new_capability")),
        "related_symbols": related_symbols,
    }


def _build_component_evidence(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in mapping.get("codebase_mappings") or []:
        normalized = _normalize_mapping_entry(entry)
        key = normalized.get("normalized_symbol") or normalized.get("raw_symbol")
        if key and key.lower() not in seen:
            evidence.append(normalized)
            seen.add(key.lower())
    return evidence


def _extract_hld_intro(hld_markdown: str) -> Dict[str, Any]:
    m = re.search(
        r"##\s+1\s+Introduction\s*\n(.*?)(?=\n##\s+2\s|\Z)",
        hld_markdown,
        re.DOTALL | re.IGNORECASE,
    )
    return {"raw": m.group(1).strip() if m else ""}


def _extract_architecture_decisions(hld_markdown: str) -> List[str]:
    m = re.search(
        r"###\s+2\.0\s+Architecture Decisions.*?\n(.*?)(?=\n###\s+2\.|\n##\s+|\Z)",
        hld_markdown,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    lines = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if ln.startswith("- "):
            lines.append(ln[2:])
    return lines


def _extract_sequence_diagrams(hld_markdown: str) -> List[str]:
    return re.findall(r"```mermaid\s*\n(.*?)```", hld_markdown, re.DOTALL)


def _diagram_mentions_module(diagram: str, symbols: List[str], module_name: str) -> bool:
    blob = diagram.lower()
    mod_compact = module_name.lower().replace(" ", "")
    if mod_compact and mod_compact in blob.replace(" ", ""):
        return True
    for sym in symbols:
        clean = sym.replace(".", "").replace("()", "").lower()
        if clean and len(clean) > 3 and clean in blob:
            return True
        for token in re.findall(r"[A-Z][A-Za-z0-9]+", sym):
            if len(token) > 3 and token.lower() in blob:
                return True
    return False


def _keyword_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    stop = {
        "module", "service", "controller", "workflow", "api", "data",
        "user", "users", "details", "content", "class", "method",
    }
    for value in values:
        if isinstance(value, dict):
            value = " ".join(str(v) for v in value.values())
        elif isinstance(value, list):
            value = " ".join(str(v) for v in value)
        text = str(value or "")
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text):
            low = token.lower()
            if low not in stop:
                tokens.add(low)
    return tokens


def _module_traceability_tokens(
    module_name: str,
    req_mod: Dict[str, Any],
    component_evidence: List[Dict[str, Any]],
    flows: List[Dict[str, Any]],
) -> set[str]:
    values: List[Any] = [
        module_name,
        req_mod.get("detailed_responsibility", ""),
        req_mod.get("capabilities", []),
        req_mod.get("interfaces_and_apis", []),
    ]
    for item in component_evidence:
        values.extend([
            item.get("raw_symbol", ""),
            item.get("normalized_symbol", ""),
            item.get("class_name", ""),
            item.get("method_name", ""),
            item.get("note", ""),
        ])
    for flow in flows:
        for step in flow.get("step_by_step_sequence") or []:
            if module_name.lower() in " ".join([
                step.get("source_component", ""),
                step.get("destination_component", ""),
            ]).lower():
                values.extend([
                    step.get("source_component", ""),
                    step.get("destination_component", ""),
                    step.get("operation_signature", ""),
                    step.get("payload_description", ""),
                ])
    return _keyword_tokens(*values)


def _resolve_ac_symbol_for_module(
    ac: Dict[str, Any],
    seed_resolutions: List[Dict[str, Any]],
    component_evidence: List[Dict[str, Any]],
    module_tokens: set[str],
) -> Tuple[str, int]:
    bls = ac.get("verifies", [])
    evidence_symbols = []
    evidence_text = []
    for item in component_evidence:
        for key in ("raw_symbol", "normalized_symbol", "class_name", "method_name"):
            if item.get(key):
                evidence_symbols.append(item[key])
        evidence_text.append(item.get("note", ""))

    mod_sym_set = {
        re.sub(r"[^a-z0-9]", "", s.lower())
        for s in evidence_symbols
        if s
    }
    ac_tokens = _keyword_tokens(ac.get("text", ""), ac.get("verifies", []))
    score = len(ac_tokens & module_tokens)
    seed_by_bl: Dict[str, str] = {}
    for s in seed_resolutions:
        note = s.get("note", "")
        node = s.get("node") or {}
        sym = node.get("label") or s.get("name", "")
        for bl in bls:
            if bl in note:
                seed_by_bl[bl] = sym

    for bl in bls:
        if bl in seed_by_bl:
            sym = seed_by_bl[bl]
            sym_clean = re.sub(r"[^a-z0-9]", "", sym.lower())
            if any(part and (part in sym_clean or sym_clean in part) for part in mod_sym_set):
                return sym, score + 4
            note_blob = " ".join(evidence_text).lower()
            if sym.startswith(".") and sym.lower().replace(".", "").replace("()", "") in note_blob:
                return sym, score + 3

    for s in seed_resolutions:
        note = s.get("note", "")
        for bl in bls:
            if bl in note:
                node = s.get("node") or {}
                lbl = node.get("label", "")
                if lbl:
                    lbl_clean = re.sub(r"[^a-z0-9]", "", lbl.lower())
                    if any(part and (part in lbl_clean or lbl_clean in part) for part in mod_sym_set):
                        return lbl, score + 3
    return "", score


def _filter_acs_for_module(
    acceptance_criteria: List[Dict[str, Any]],
    seed_resolutions: List[Dict[str, Any]],
    module_name: str,
    req_mod: Dict[str, Any],
    component_evidence: List[Dict[str, Any]],
    flows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    filtered = []
    module_tokens = _module_traceability_tokens(
        module_name, req_mod, component_evidence, flows,
    )
    for ac in acceptance_criteria:
        sym, score = _resolve_ac_symbol_for_module(
            ac, seed_resolutions, component_evidence, module_tokens,
        )
        if sym and score >= 2:
            filtered.append({**ac, "_mapped_symbol": sym, "_module_score": score})
        elif not sym and score >= 5:
            filtered.append({**ac, "_mapped_symbol": "", "_module_score": score})
    return filtered


def build_module_bundle(
    logical_name: str,
    *,
    requirements: Dict[str, Any],
    hld_markdown: str,
    code_graph: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble deterministic context bundle for one module."""
    req_modules = requirements.get("hld_content", {}).get("2_logical_view", {}).get("modules", [])
    req_mod = _requirements_module_by_name(req_modules, logical_name)
    mapped_modules = code_graph.get("mapping", {}).get("mapped_modules", [])
    mapping = _find_mapping_for_module(logical_name, mapped_modules)
    symbols = _symbols_from_mapping(mapping)
    component_evidence = _build_component_evidence(mapping or {})
    normalized_symbols = []
    for item in component_evidence:
        for value in (
            item.get("normalized_symbol"),
            item.get("class_name"),
            item.get("raw_symbol"),
        ):
            if value and value not in normalized_symbols:
                normalized_symbols.append(value)
    all_target = code_graph.get("target_projects", [])
    target_projects = _target_projects_from_mapping(mapping, all_target)
    all_flows = requirements.get("hld_content", {}).get("2_logical_view", {}).get(
        "interactions_and_flows", []
    )
    filtered_flows = _filter_flows_for_module(all_flows, logical_name, symbols)
    hld_excerpt = _extract_hld_section(hld_markdown, logical_name)
    intro = requirements.get("hld_content", {}).get("1_introduction", {})
    seed_resolutions = code_graph.get("seed_resolutions", [])
    acceptance_criteria = code_graph.get("acceptance_criteria", [])
    filtered_acs = _filter_acs_for_module(
        acceptance_criteria,
        seed_resolutions,
        logical_name,
        req_mod or {},
        component_evidence,
        filtered_flows,
    )

    dependencies_in: List[Dict[str, str]] = []
    dependencies_out: List[Dict[str, str]] = []
    if mapping:
        # 6.1: Inputs this module receives from other modules/systems.
        for dep in mapping.get("dependencies") or []:
            dep_name = dep.get("dependency", "")
            if dep_name:
                dependencies_in.append({
                    "dependency": dep_name,
                    "codebase_symbol": dep.get("codebase_symbol", ""),
                    "source_file": dep.get("source_file", ""),
                })

    # 6.2: Outputs this module provides to modules that depend on it.
    # code_graph maps dependencies as: { dependency: "<other module name>", ... }
    # so we scan for modules that reference the current module as their dependency.
    for other_mod in mapped_modules:
        for dep in other_mod.get("dependencies") or []:
            dep_name = dep.get("dependency", "")
            if dep_name and dep_name.lower() == logical_name.lower():
                consumer_mod_name = other_mod.get("module_name") or dep_name
                dependencies_out.append({
                    "dependency": consumer_mod_name,
                    "codebase_symbol": dep.get("codebase_symbol", ""),
                    "source_file": dep.get("source_file", ""),
                })

    all_diagrams = _extract_sequence_diagrams(hld_markdown)
    module_diagrams = [
        d for d in all_diagrams
        if _diagram_mentions_module(d, symbols, logical_name)
    ]

    arch_decisions = _extract_architecture_decisions(hld_markdown)
    if target_projects and any(
        "jsonrepository" in p.lower() or "infra" in p.lower() for p in target_projects
    ):
        pass  # include arch decisions when persistence layer involved
    elif not any("jsonrepository" in (s or "").lower() for s in symbols):
        arch_decisions = [
            d for d in arch_decisions
            if "persist" not in d.lower() or any("api" in p.lower() for p in target_projects)
        ]

    bundle = {
        "logical_name": logical_name,
        "slug": slugify_module_name(logical_name),
        "ticket": code_graph.get("contract", {}).get("ticket"),
        "requirements_module": req_mod or {},
        "mapping": mapping or {},
        "component_evidence": component_evidence,
        "target_projects": target_projects,
        "primary_symbols": normalized_symbols or symbols,
        "raw_symbols": symbols,
        "filtered_flows": filtered_flows,
        "hld_excerpt": hld_excerpt,
        "hld_intro": intro,
        "architecture_decisions": arch_decisions,
        "filtered_acs": filtered_acs,
        "dependencies_in": dependencies_in,
        "dependencies_out": dependencies_out,
        "sequence_diagrams": module_diagrams,
    }
    return bundle


def _render_component_table(bundle: Dict[str, Any]) -> str:
    evidence = bundle.get("component_evidence") or []
    if not evidence:
        evidence = [
            _normalize_mapping_entry(entry)
            for entry in (bundle.get("mapping") or {}).get("codebase_mappings") or []
        ]
    if not evidence:
        return ""
    lines = [
        "| Symbol | Class | Source File | Base Classes | Existing Methods / Impact Candidates | DTOs | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in evidence:
        sym = item.get("normalized_symbol") or item.get("raw_symbol", "")
        cls = item.get("class_name") or "—"
        src = item.get("source_file") or "external"
        bases = ", ".join(item.get("base_classes") or []) or "—"
        methods = item.get("methods") or []
        method_labels = [m.get("display_name", "") for m in methods[:6] if m.get("display_name")]
        methods_str = ", ".join(method_labels) or "—"
        dtos = ", ".join(item.get("dtos") or []) or "—"
        note = markdown_table_cell(item.get("note", ""))[:150] or "—"
        lines.append(
            f"| `{sym}` | `{cls}` | `{src}` | {bases} | {methods_str} | {dtos} | {note} |"
        )
    return "\n".join(lines)


def _render_api_table(mapping: Dict[str, Any]) -> str:
    apis = mapping.get("interfaces_and_apis") or []
    if not apis:
        return ""
    lines = [
        "| Interface | Protocol | Signature | Mapped Symbol |",
        "| --- | --- | --- | --- |",
    ]
    for api in apis:
        mappings = api.get("codebase_mappings") or []
        sym = mappings[0].get("codebase_symbol", "—") if mappings else "—"
        if mappings:
            src = mappings[0].get("source_file", "")
            sym = _normalize_code_symbol(sym, src).get("normalized_symbol", sym)
        lines.append(
            f"| {api.get('interface_name', '—')} | {api.get('protocol_or_type', '—')} "
            f"| `{api.get('signature', '—')}` | `{sym}` |"
        )
    return "\n".join(lines)


def _render_traceability_table(acs: List[Dict[str, Any]]) -> str:
    if not acs:
        return ""
    lines = [
        "| AC ID | Requirement (full) | Verifies (BL) | Mapped Code Symbol |",
        "| --- | --- | --- | --- |",
    ]
    for ac in acs:
        ac_id = markdown_table_cell(ac.get("id", ""))
        text = markdown_table_cell(ac.get("text", ""))
        bls = ", ".join(ac.get("verifies", []))
        sym = ac.get("_mapped_symbol", "")
        sym_cell = f"`{sym}`" if sym else "Not mapped in code graph"
        lines.append(f"| {ac_id} | {text} | {bls} | {sym_cell} |")
    return "\n".join(lines)


def _render_use_case_flow(flows: List[Dict[str, Any]]) -> str:
    if not flows:
        return ""
    lines: List[str] = []
    for flow in flows:
        lines.append(f"#### {flow.get('flow_name', 'Flow')}")
        for step in flow.get("step_by_step_sequence") or []:
            num = step.get("step_number", "")
            src = step.get("source_component", "")
            dst = step.get("destination_component", "")
            op = step.get("operation_signature", "")
            lines.append(f"{num}. `{src}` → `{dst}` via `{op}`")
        lines.append("")
    return "\n".join(lines)


def _render_section_2(
    bundle: Dict[str, Any],
    architecture_diagram: str = "",
    section_body: str = "",
) -> str:
    mod = bundle.get("requirements_module") or {}
    lines = [
        "## 2 Module Architecture Overview",
        "",
        "### 2.1 Architecture Overview",
        "",
        f"**Module:** {bundle.get('logical_name', '')}",
        "",
    ]
    if section_body:
        lines.append(section_body)
        lines.append("")
    if mod.get("architectural_layer"):
        lines.append(f"**Architectural Layer:** {mod['architectural_layer']}")
        lines.append("")
    if bundle.get("target_projects"):
        lines.append("**Related Target Projects (C#):**")
        for p in bundle["target_projects"]:
            lines.append(f"- {p}")
        lines.append("")
    caps = mod.get("capabilities") or []
    if caps:
        lines.append("**Capabilities:**")
        for c in caps:
            lines.append(f"- {c}")
        lines.append("")
    if bundle.get("architecture_decisions"):
        lines.append("**Relevant Architecture Decisions (from HLD):**")
        for d in bundle["architecture_decisions"]:
            lines.append(f"- {d}")
        lines.append("")
    if architecture_diagram:
        lines.append("**Module structure diagram:**")
        lines.append("")
        lines.append(_mermaid_fence(architecture_diagram))
        lines.append("")
    return "\n".join(lines)


def _render_section_4(bundle: Dict[str, Any], section_body: str = "") -> str:
    mapping = bundle.get("mapping") or {}
    lines = ["## 4 Component and Class Design", ""]
    if section_body:
        lines.append(section_body)
        lines.append("")
    table = _render_component_table(bundle)
    if table:
        lines.append("### Component Evidence")
        lines.append("")
        lines.append(table)
        lines.append("")
    api_table = _render_api_table(mapping)
    if api_table:
        lines.append("### Module Interfaces and APIs")
        lines.append("")
        lines.append(api_table)
        lines.append("")
    return "\n".join(lines)


def _render_section_6(bundle: Dict[str, Any], section_body: str = "") -> str:
    deps_in = bundle.get("dependencies_in") or []
    deps_out = bundle.get("dependencies_out") or []
    if not deps_in and not deps_out and not section_body:
        return ""
    lines = ["## 6 External System/Module Interface Design", ""]
    if section_body:
        lines.append(section_body)
        lines.append("")
    if deps_in:
        lines.extend([
            "### 6.1 Input received from external Systems and Modules",
            "",
            "| Dependency | Codebase Symbol | Source File |",
            "| --- | --- | --- |",
        ])
        for d in deps_in:
            lines.append(
                f"| {d.get('dependency', '')} | `{d.get('codebase_symbol', '')}` "
                f"| `{d.get('source_file', '') or 'external'}` |"
            )
        lines.append("")
    if deps_out:
        lines.extend([
            "### 6.2 Output given to external Systems and Modules",
            "",
            "| Dependency | Codebase Symbol | Source File |",
            "| --- | --- | --- |",
        ])
        for d in deps_out:
            lines.append(
                f"| {d.get('dependency', '')} | `{d.get('codebase_symbol', '')}` "
                f"| `{d.get('source_file', '') or 'external'}` |"
            )
        lines.append("")
    return "\n".join(lines)


def _expected_sop_headings(include: Dict[str, Any]) -> List[str]:
    headings: List[str] = []
    for section in MDD_SECTIONS:
        if include.get(section["key"]):
            headings.append(f"## {section['number']} {section['title']}")
        for sub in section.get("subsections", []):
            if include.get(sub["key"]):
                headings.append(f"### {sub['number']} {sub['title']}")
    return headings


def build_mdd_quality_report(
    doc: str,
    bundle: Dict[str, Any],
    plan: Dict[str, Any],
    diagram_report: Dict[str, Any],
) -> Dict[str, Any]:
    """Non-blocking MDD quality gate report for manifest/plan consumers."""
    include = plan.get("include_sections", {})
    expected = _expected_sop_headings(include)
    missing = [h for h in expected if h not in doc]
    warnings: List[str] = []

    if "HLD Logical View excerpt" in doc:
        warnings.append("raw_hld_excerpt_present")
    if re.search(r"class\s+\.[A-Za-z_]", doc):
        warnings.append("invalid_dot_prefixed_class_present")
    if re.search(r"\bclass\s+\w+\s*\+\s*\w+", doc):
        warnings.append("composite_symbol_used_as_class")
    if re.search(r"\b(Redis|Kafka|AWS|OIDC|Kubernetes)\b", doc, re.IGNORECASE):
        warnings.append("potential_unsourced_infrastructure_reference")
    if include.get("component_design") and not bundle.get("component_evidence"):
        warnings.append("component_design_without_component_evidence")
    if include.get("traceability") and not bundle.get("filtered_acs"):
        warnings.append("traceability_enabled_without_module_acs")
    if diagram_report.get("invalid", 0):
        warnings.append("invalid_mermaid_diagrams")

    allowed_classes = {
        item.get("class_name", "")
        for item in bundle.get("component_evidence") or []
        if item.get("class_name")
    }
    for item in bundle.get("component_evidence") or []:
        allowed_classes.update(item.get("base_classes") or [])
        allowed_classes.update(item.get("implemented_interfaces") or [])
    allowed_classes.update({"ControllerBase", "WorkflowBase"})
    declared_classes = set(re.findall(r"^\s*class\s+([A-Za-z_]\w*)", doc, re.MULTILINE))
    unexpected_classes = sorted(
        c for c in declared_classes
        if c not in allowed_classes and c.lower() not in {a.lower() for a in allowed_classes}
    )
    if unexpected_classes:
        warnings.append(f"unexpected_class_diagram_classes:{','.join(unexpected_classes)}")

    llm_sections = plan.get("llm_sections_generated", [])
    if include.get("component_design") and "component_design" not in llm_sections:
        warnings.append("component_design_llm_body_missing")
    if include.get("architecture_overview") and "architecture_overview" not in llm_sections:
        warnings.append("architecture_overview_llm_body_missing")

    return {
        "valid": not missing and not warnings and diagram_report.get("invalid", 0) == 0,
        "missing_sop_headings": missing,
        "warnings": warnings,
        "diagram_report": diagram_report,
        "llm_sections_generated": llm_sections,
        "component_evidence_count": len(bundle.get("component_evidence") or []),
        "traceability_count": len(bundle.get("filtered_acs") or []),
        "quality_rules": MDD_QUALITY_RULES,
    }


def _planner_prompt(bundle: Dict[str, Any]) -> str:
    return "\n".join([
        f"Plan MDD sections for module '{bundle.get('logical_name', '')}'.",
        "Respond with JSON matching:",
        "```json",
        PLAN_JSON_SCHEMA,
        "```",
        "",
        "=== MODULE BUNDLE (summary) ===",
        json.dumps({
            "logical_name": bundle.get("logical_name"),
            "target_projects": bundle.get("target_projects"),
            "primary_symbols": bundle.get("primary_symbols"),
            "flow_count": len(bundle.get("filtered_flows", [])),
            "component_count": len((bundle.get("mapping") or {}).get("codebase_mappings", [])),
            "ac_count": len(bundle.get("filtered_acs", [])),
            "has_dependencies": bool(bundle.get("dependencies_in")),
        }, indent=2),
        "",
        "Return JSON only.",
    ])


def _section_context_payload(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, generic evidence for MDD section generation."""
    return {
        "module": {
            "logical_name": bundle.get("logical_name"),
            "target_projects": bundle.get("target_projects"),
            "requirements_module": bundle.get("requirements_module"),
            "architecture_decisions": bundle.get("architecture_decisions"),
        },
        "component_evidence": bundle.get("component_evidence") or [],
        "interfaces_and_apis": (bundle.get("mapping") or {}).get("interfaces_and_apis") or [],
        "flows": bundle.get("filtered_flows") or [],
        "dependencies_in": bundle.get("dependencies_in") or [],
        "dependencies_out": bundle.get("dependencies_out") or [],
        "module_relevant_acceptance_criteria": bundle.get("filtered_acs") or [],
        "hld_logical_view_context": (bundle.get("hld_excerpt") or "")[:3000],
    }


def _strip_section_heading(markdown: str) -> str:
    """Keep SOP headings deterministic by removing headings from LLM bodies."""
    markdown = re.sub(r"```mermaid\s*\n.*?```", "", markdown or "", flags=re.DOTALL)
    lines = []
    for line in markdown.strip().splitlines():
        if re.match(r"^\s*#{1,6}\s+", line):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _compact_section_body(markdown: str, *, max_chars: int = 1800) -> str:
    """Keep generated MDD prose readable; evidence tables carry the detail."""
    text = (markdown or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit("\n\n", 1)[0].strip()
    return cut or text[:max_chars].strip()


def _llm_section_body(
    llm,
    section_key: str,
    bundle: Dict[str, Any],
    *,
    temperature: float = 0.2,
    max_tokens: int = 900,
) -> str:
    """Generate only the body under a fixed SOP-036 section heading."""
    contract = MDD_SECTION_CONTRACT.get(section_key, {})
    must_cover = contract.get("must_cover", [])
    prompt = "\n".join([
        f"Generate the body content for SOP-036 MDD section: {contract.get('heading', section_key)}.",
        f"Module: {bundle.get('logical_name', '')}",
        "Return markdown BODY ONLY. Do not include any heading line.",
        "Do not include Mermaid diagrams or fenced code blocks in this prose body.",
        "Keep it concise: 2-4 short paragraphs or 4-7 bullets maximum.",
        "Do not repeat content that will be obvious from evidence tables.",
        "Ground every claim in the provided evidence. Do not invent code, DTOs, APIs, infra, or storage.",
        "If a required detail is not present, write 'To be confirmed' for that detail.",
        "Do not paste raw HLD excerpts. Use the HLD context only to derive MDD-level design statements.",
        "Keep content concise but implementation-level.",
        "",
        "MUST COVER:",
        json.dumps(must_cover, indent=2, ensure_ascii=False),
        "",
        "QUALITY RULES:",
        json.dumps(MDD_QUALITY_RULES, indent=2, ensure_ascii=False),
        "",
        "=== EVIDENCE ===",
        json.dumps(_section_context_payload(bundle), indent=2, ensure_ascii=False)[:12000],
    ])
    try:
        raw = llm.chat(
            system_prompt=_MDD_SYSTEM,
            user_prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _compact_section_body(_strip_section_heading(raw))
    except Exception:  # noqa: BLE001
        return ""


def _generate_section_bodies(
    llm,
    bundle: Dict[str, Any],
    include: Dict[str, Any],
    *,
    temperature: float = 0.2,
) -> Dict[str, str]:
    """Generate MDD prose for applicable SOP sections while headings stay fixed."""
    bodies: Dict[str, str] = {}
    for key in (
        "purpose_and_scope",
        "architecture_overview",
        "use_case_flow",
        "component_design",
        "sequence_overview",
        "external_interfaces",
    ):
        if include.get(key):
            body = _llm_section_body(llm, key, bundle, temperature=temperature)
            if body:
                bodies[key] = body
    return bodies


def _llm_narrative_prompt(section: str, bundle: Dict[str, Any]) -> str:
    return "\n".join([
        f"Write MDD section content for: {section}",
        f"Module: {bundle.get('logical_name', '')}",
        "Use only data from the bundle below. Short, precise prose.",
        "",
        "=== MODULE BUNDLE ===",
        json.dumps({
            "requirements_module": bundle.get("requirements_module"),
            "hld_excerpt": (bundle.get("hld_excerpt") or "")[:3000],
            "primary_symbols": bundle.get("primary_symbols"),
            "target_projects": bundle.get("target_projects"),
        }, indent=2, ensure_ascii=False)[:6000],
        "",
        f"Output markdown starting with the section heading for {section}. No preamble.",
    ])


def _build_mdd_document(
    bundle: Dict[str, Any],
    plan: Dict[str, Any],
    llm,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """Assemble full MDD markdown from plan + deterministic renders + LLM diagrams."""
    include = plan.get("include_sections", {})
    project = bundle.get("logical_name", "Module")
    ticket = bundle.get("ticket") or ""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    diagrams, diagram_sources = generate_mdd_diagrams(
        llm, bundle, include, temperature=temperature,
    )
    section_bodies = _generate_section_bodies(
        llm, bundle, include, temperature=temperature,
    )
    plan["diagrams_generated"] = list(diagrams.keys())
    plan["diagram_sources"] = diagram_sources
    plan["llm_sections_generated"] = list(section_bodies.keys())

    cover = [
        f"# {project} — Module Detail Design",
        "",
        f"**Ticket:** {ticket}" if ticket else "",
        "",
        "## Revision History",
        "",
        "| Date | Revision No. | Author | Comments |",
        "|---|---|---|---|",
        f"| {date_str} | 1.0 | MDD Generation Pipeline | Initial generated MDD for {project}. |",
        "",
        "## Table of Contents",
        "",
    ]
    toc_entries = []
    if include.get("introduction"):
        toc_entries.append("- [1. Introduction](#1-introduction)")
    if include.get("module_architecture"):
        toc_entries.append("- [2. Module Architecture Overview](#2-module-architecture-overview)")
    if include.get("use_case_flow"):
        toc_entries.append("- [3. Use Case Flow](#3-use-case-flow)")
    if include.get("component_design"):
        toc_entries.append("- [4. Component and Class Design](#4-component-and-class-design)")
    if include.get("sequence_flow"):
        toc_entries.append("- [5. Sequence Flow](#5-sequence-flow)")
    if include.get("external_interfaces"):
        toc_entries.append("- [6. External System/Module Interface Design](#6-external-systemmodule-interface-design)")
    if include.get("annexure"):
        toc_entries.append("- [7. Annexure](#7-annexure)")
    cover.extend(toc_entries)
    cover.extend(["", "---", ""])

    sections: List[str] = []

    if include.get("introduction"):
        intro_parts = ["## 1 Introduction", ""]
        if include.get("purpose_and_scope"):
            intro_parts.append("### 1.1 Purpose and Scope")
            intro_parts.append("")
            mod = bundle.get("requirements_module") or {}
            if section_bodies.get("purpose_and_scope"):
                intro_parts.append(section_bodies["purpose_and_scope"])
            elif mod.get("detailed_responsibility"):
                intro_parts.append(mod["detailed_responsibility"])
            else:
                intro_parts.append("To be confirmed.")
            intro_parts.append("")
        if include.get("definitions"):
            intro_parts.append("### 1.2 Definitions and Acronyms")
            intro_parts.append("")
            hld_intro = bundle.get("hld_intro") or {}
            defs = hld_intro.get("1_2_definitions_and_acronyms") or hld_intro.get("definitions_and_acronyms") or []
            if isinstance(defs, list):
                for d in defs:
                    term = d.get("term") or ""
                    expansion = d.get("expansion") or ""
                    definition = d.get("definition") or ""
                    if not term and not definition:
                        continue
                    label = f"**{term}**" if term else ""
                    if expansion:
                        label = f"{label} ({expansion})" if label else f"({expansion})"
                    line = f"- {label}".rstrip()
                    if definition:
                        line = f"{line} — {definition}"
                    intro_parts.append(line)
            elif isinstance(defs, dict):
                terms = defs.get("terms") or []
                if isinstance(terms, list):
                    for d in terms:
                        term = d.get("term") or ""
                        expansion = d.get("expansion") or ""
                        definition = d.get("definition") or ""
                        if not term and not definition:
                            continue
                        label = f"**{term}**" if term else ""
                        if expansion:
                            label = f"{label} ({expansion})" if label else f"({expansion})"
                        line = f"- {label}".rstrip()
                        if definition:
                            line = f"{line} — {definition}"
                        intro_parts.append(line)
            intro_parts.append("")
        if include.get("references"):
            intro_parts.append("### 1.3 References")
            intro_parts.append("")
            hld_intro = bundle.get("hld_intro") or {}
            refs = hld_intro.get("1_3_references") or hld_intro.get("references") or []
            if isinstance(refs, list):
                for r in refs:
                    title = r.get("title") or ""
                    url = r.get("url_or_location") or r.get("url") or ""
                    desc = r.get("relationship_description") or r.get("description") or ""
                    if not title and not url:
                        continue
                    if url and title:
                        line = f"- [{title}]({url})"
                    elif title:
                        line = f"- **{title}**"
                    else:
                        line = f"- {url}"
                    if desc:
                        line = f"{line} — {desc}"
                    intro_parts.append(line)
            elif isinstance(refs, dict):
                documents = refs.get("documents") or []
                if isinstance(documents, list):
                    for r in documents:
                        title = r.get("title") or ""
                        url = r.get("url_or_location") or r.get("url") or ""
                        desc = r.get("relationship_description") or r.get("description") or ""
                        if not title and not url:
                            continue
                        if url and title:
                            line = f"- [{title}]({url})"
                        elif title:
                            line = f"- **{title}**"
                        else:
                            line = f"- {url}"
                        if desc:
                            line = f"{line} — {desc}"
                        intro_parts.append(line)
            intro_parts.append("")
        sections.append("\n".join(intro_parts))

    if include.get("module_architecture"):
        sections.append(_render_section_2(
            bundle,
            diagrams.get("architecture", ""),
            section_bodies.get("architecture_overview", ""),
        ))

    if include.get("use_case_flow"):
        flow_md = _render_use_case_flow(bundle.get("filtered_flows", []))
        if flow_md or diagrams.get("use_case") or section_bodies.get("use_case_flow"):
            uc_parts = ["## 3 Use Case Flow", ""]
            if section_bodies.get("use_case_flow"):
                uc_parts.append(section_bodies["use_case_flow"])
                uc_parts.append("")
            if flow_md:
                uc_parts.append("#### Source Flow Steps")
                uc_parts.append("")
                uc_parts.append(flow_md)
            if diagrams.get("use_case"):
                uc_parts.append("#### Use case flow diagram")
                uc_parts.append("")
                uc_parts.append(_mermaid_fence(diagrams["use_case"]))
                uc_parts.append("")
            sections.append("\n".join(uc_parts))

    if include.get("component_design"):
        sec4 = _render_section_4(bundle, section_bodies.get("component_design", ""))
        if diagrams.get("class"):
            sec4 += "\n### Component class diagram\n\n" + _mermaid_fence(diagrams["class"]) + "\n"
        sections.append(sec4)

    if include.get("sequence_flow"):
        seq_parts = ["## 5 Sequence Flow", "", "### 5.1 Sequence Overview", ""]
        if section_bodies.get("sequence_overview"):
            seq_parts.append(section_bodies["sequence_overview"])
            seq_parts.append("")
        if diagrams.get("sequence"):
            source = diagram_sources.get("sequence", "llm")
            source_label = {
                "hld": "from HLD",
                "llm": "LLM-generated",
                "fallback": "deterministic fallback",
            }.get(source, source)
            seq_parts.append(f"#### Sequence diagram ({source_label})")
            seq_parts.append("")
            seq_parts.append(_mermaid_fence(diagrams["sequence"]))
            seq_parts.append("")
        elif bundle.get("filtered_flows"):
            seq_parts.append(_render_use_case_flow(bundle["filtered_flows"]))
        sections.append("\n".join(seq_parts))

    if include.get("external_interfaces"):
        ext = _render_section_6(bundle, section_bodies.get("external_interfaces", ""))
        if ext:
            sections.append(ext)

    if include.get("annexure") and include.get("traceability"):
        trace = _render_traceability_table(bundle.get("filtered_acs", []))
        if trace:
            sections.append("## 7 Annexure\n\n### 7.1 Requirements Traceability Matrix\n\n" + trace)

    body = "\n\n".join(sections)
    return "\n".join(cover) + "\n\n" + body


def generate_mdd_for_modules(
    selected_modules: List[str],
    *,
    ticket: Optional[str] = None,
    artifact_dir: Optional[str] = None,
    requirements_path: Optional[str] = None,
    hld_path: Optional[str] = None,
    code_graph_path: Optional[str] = None,
    temperature: float = 0.2,
) -> MDDGenerateResult:
    """Generate one MDD markdown file per selected logical module."""
    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.utcnow().isoformat()
    out_dir = artifact_dir or os.getenv("ARTIFACT_DIR", "./artifacts")
    mdd_dir = os.path.join(out_dir, "mdd")
    os.makedirs(mdd_dir, exist_ok=True)

    catalog = load_module_catalog(out_dir)
    valid_names = get_catalog_module_names(catalog)
    if not valid_names:
        build_module_catalog(artifact_dir=out_dir)
        catalog = load_module_catalog(out_dir)
        valid_names = get_catalog_module_names(catalog)

    unknown = [m for m in selected_modules if m not in valid_names]
    if unknown:
        raise ValueError(
            f"Unknown module(s): {unknown}. Valid modules: {valid_names}"
        )

    req_path = requirements_path or os.path.join(out_dir, "requirements.json")
    hld_file = hld_path or os.path.join(out_dir, "HLD.md")
    cg_path = code_graph_path or os.path.join(out_dir, "code_graph.json")

    req_payload = _load_json(req_path)
    requirements = req_payload.get("requirements", req_payload)
    hld_markdown = Path(hld_file).read_text(encoding="utf-8")
    cg_payload = _load_json(cg_path)
    code_graph = cg_payload.get("code_graph", cg_payload)

    resolved_ticket = ticket or catalog.get("ticket") or code_graph.get("contract", {}).get("ticket") or "feature"
    llm = get_llm_client()
    generated: List[MDDModuleResult] = []

    for logical_name in selected_modules:
        print(f"[MDD Pipeline] Generating MDD for module: {logical_name}...")
        bundle = build_module_bundle(
            logical_name,
            requirements=requirements,
            hld_markdown=hld_markdown,
            code_graph=code_graph,
        )

        try:
            plan_raw = llm.chat(
                system_prompt=_PLANNER_SYSTEM,
                user_prompt=_planner_prompt(bundle),
                temperature=0.1,
                max_tokens=1500,
            )
            plan = _coerce_json(plan_raw)
        except Exception:  # noqa: BLE001
            # Planner flakiness should never block deterministic section inclusion.
            plan = {"include_sections": {}, "module_name": logical_name}
        plan = normalize_mdd_plan(plan, bundle)

        mdd_raw = _build_mdd_document(bundle, plan, llm, temperature=temperature)
        mdd_clean = postprocess_mermaid(mdd_raw)
        diagram_report = validate_diagrams(mdd_clean)
        quality_report = build_mdd_quality_report(
            mdd_clean, bundle, plan, diagram_report,
        )

        if not plan.get("diagrams_generated"):
            print(
                f"[MDD Pipeline] WARNING: No Mermaid diagrams for {logical_name}. "
                "Check LLM config or code_graph.mapping for this module."
            )
        else:
            print(
                f"[MDD Pipeline] Diagrams for {logical_name}: "
                f"{plan.get('diagram_sources', {})}"
            )

        slug = slugify_module_name(logical_name)
        plan_path = os.path.join(mdd_dir, f"mdd_plan_{slug}.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    **plan,
                    "diagram_report": diagram_report,
                    "mdd_quality_report": quality_report,
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )

        out_name = f"MDD_{slug}_{resolved_ticket}.md"
        out_path = os.path.join(mdd_dir, out_name)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(mdd_clean)

        generated.append(MDDModuleResult(
            module_name=logical_name,
            slug=slug,
            artifact_path=out_path,
            plan={
                **plan,
                "diagram_report": diagram_report,
                "mdd_quality_report": quality_report,
            },
            sections_included=plan.get("sections_included", []),
            sections_skipped=plan.get("sections_skipped", []),
        ))
        print(f"[MDD Pipeline] Wrote {out_path}")

    completed_at = datetime.utcnow().isoformat()
    manifest_path = os.path.join(out_dir, "mdd_manifest.json")
    manifest = {
        "job_id": job_id,
        "ticket": resolved_ticket,
        "started_at": started_at,
        "completed_at": completed_at,
        "selected_modules": selected_modules,
        "catalog_source": catalog.get("catalog_source", "requirements.json + HLD.md"),
        "generated": [
            {
                "module": r.module_name,
                "slug": r.slug,
                "path": r.artifact_path,
                "sections_included": r.sections_included,
                "sections_skipped": r.sections_skipped,
                "diagrams_generated": r.plan.get("diagrams_generated", []),
                "diagram_sources": r.plan.get("diagram_sources", {}),
                "diagram_report": r.plan.get("diagram_report", {}),
                "llm_sections_generated": r.plan.get("llm_sections_generated", []),
                "mdd_quality_report": r.plan.get("mdd_quality_report", {}),
            }
            for r in generated
        ],
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    return MDDGenerateResult(
        job_id=job_id,
        ticket=resolved_ticket,
        started_at=started_at,
        completed_at=completed_at,
        generated=generated,
        manifest_path=manifest_path,
    )
