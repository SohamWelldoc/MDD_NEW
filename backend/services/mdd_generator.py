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
    if cbs:
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
    """HLD-style pipeline: prefer validated HLD reuse, then LLM, then deterministic fallback."""
    for source, block in (
        ("hld", hld_block),
        ("llm", llm_block),
        ("fallback", fallback_block),
    ):
        finalized = _finalize_diagram_block(block)
        if finalized:
            return finalized, source
    return "", "none"


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
        prompt = "\n".join([
            f"Generate a ```mermaid flowchart TD``` showing internal structure of module '{module}'.",
            "Show mapped codebase classes as nodes with valid IDs and quoted labels.",
            "Show dependencies on other modules with dashed edges.",
            allowlist,
            context,
            "Output only the mermaid fenced block.",
        ])
        llm_block = _llm_diagram(llm, prompt, temperature=temperature)
        block, source = _resolve_diagram(
            llm_block=llm_block,
            fallback_block=build_architecture_flowchart(bundle),
        )
        if block:
            diagrams["architecture"] = block
            sources["architecture"] = source

    if include.get("use_case_flow") and flows:
        prompt = "\n".join([
            f"Generate a ```mermaid flowchart TD``` for use-case flow(s) in module '{module}'.",
            "Model each flow step as nodes; quote edge labels that contain parentheses.",
            allowlist,
            context,
            "Output only the mermaid fenced block.",
        ])
        llm_block = _llm_diagram(llm, prompt, temperature=temperature)
        block, source = _resolve_diagram(
            llm_block=llm_block,
            fallback_block=build_use_case_flowchart(bundle),
        )
        if block:
            diagrams["use_case"] = block
            sources["use_case"] = source

    if include.get("component_design") and has_components:
        prompt = "\n".join([
            f"Generate a ```mermaid classDiagram``` for module '{module}'.",
            "Include mapped classes, up to 5 key methods each, and inheritance where known.",
            "Do NOT create classes for method-only symbols (e.g. .GetFooduModuleInsight()).",
            allowlist,
            context,
            "Output only the mermaid fenced block.",
        ])
        llm_block = _llm_diagram(llm, prompt, temperature=temperature)
        block, source = _resolve_diagram(
            llm_block=llm_block,
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
    symbols: List[str] = list(bundle.get("primary_symbols") or [])
    mapping = bundle.get("mapping") or {}

    def _add(name: str) -> None:
        if name and name not in symbols:
            symbols.append(name)

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


def _resolve_ac_symbol_for_module(
    ac: Dict[str, Any],
    seed_resolutions: List[Dict[str, Any]],
    module_symbols: List[str],
) -> str:
    bls = ac.get("verifies", [])
    mod_sym_set = {s.lower().replace(".", "") for s in module_symbols}
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
            if any(part in sym.lower() for part in mod_sym_set if len(part) > 3):
                return sym
            if sym.startswith("."):
                return sym
            for ms in module_symbols:
                if ms.lower() in sym.lower() or sym.lower() in ms.lower():
                    return sym
            return sym

    for s in seed_resolutions:
        note = s.get("note", "")
        for bl in bls:
            if bl in note:
                node = s.get("node") or {}
                lbl = node.get("label", "")
                if lbl:
                    for ms in module_symbols:
                        if ms.lower().replace(".", "") in lbl.lower().replace(".", ""):
                            return lbl
    return ""


def _filter_acs_for_module(
    acceptance_criteria: List[Dict[str, Any]],
    seed_resolutions: List[Dict[str, Any]],
    module_symbols: List[str],
) -> List[Dict[str, Any]]:
    filtered = []
    for ac in acceptance_criteria:
        sym = _resolve_ac_symbol_for_module(ac, seed_resolutions, module_symbols)
        if sym:
            filtered.append({**ac, "_mapped_symbol": sym})
        else:
            bl_text = " ".join(ac.get("verifies", []))
            for s in seed_resolutions:
                note = s.get("note", "")
                if any(bl in note for bl in ac.get("verifies", [])):
                    node = s.get("node") or {}
                    lbl = node.get("label", "")
                    if lbl and any(
                        lbl.lower().replace(".", "") in ms.lower().replace(".", "")
                        for ms in module_symbols
                    ):
                        filtered.append({**ac, "_mapped_symbol": lbl})
                        break
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
    filtered_acs = _filter_acs_for_module(acceptance_criteria, seed_resolutions, symbols)

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
        "target_projects": target_projects,
        "primary_symbols": symbols,
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


def _render_component_table(mapping: Dict[str, Any]) -> str:
    lines = [
        "| Symbol | Source File | Base Classes | Key Methods |",
        "| --- | --- | --- | --- |",
    ]
    for cb in mapping.get("codebase_mappings") or []:
        sym = cb.get("codebase_symbol", "")
        src = cb.get("source_file", "") or "external"
        bases = ", ".join(cb.get("base_classes") or []) or "—"
        methods = cb.get("methods") or []
        method_labels = []
        for m in methods[:6]:
            short = m.split("_")[-1] if "_" in m else m
            method_labels.append(short)
        methods_str = ", ".join(method_labels) or "—"
        if sym.startswith("."):
            cls = _class_from_source_file(src)
            sym = f"{cls}{sym}" if cls else sym
        lines.append(
            f"| `{sym}` | `{src}` | {bases} | {methods_str} |"
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
        if sym.startswith("."):
            src = mappings[0].get("source_file", "") if mappings else ""
            cls = _class_from_source_file(src)
            sym = f"{cls}{sym}" if cls else sym
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


def _render_section_2(bundle: Dict[str, Any], architecture_diagram: str = "") -> str:
    mod = bundle.get("requirements_module") or {}
    lines = [
        "## 2 Module Architecture Overview",
        "",
        "### 2.1 Architecture Overview",
        "",
        f"**Module:** {bundle.get('logical_name', '')}",
        "",
    ]
    if mod.get("architectural_layer"):
        lines.append(f"**Architectural Layer:** {mod['architectural_layer']}")
        lines.append("")
    if mod.get("detailed_responsibility"):
        lines.append(mod["detailed_responsibility"])
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
    if bundle.get("hld_excerpt"):
        lines.append("**HLD Logical View excerpt:**")
        lines.append("")
        lines.append(bundle["hld_excerpt"][:4000])
        lines.append("")
    if architecture_diagram:
        lines.append("**Module structure diagram:**")
        lines.append("")
        lines.append(_mermaid_fence(architecture_diagram))
        lines.append("")
    return "\n".join(lines)


def _render_section_4(bundle: Dict[str, Any]) -> str:
    mapping = bundle.get("mapping") or {}
    lines = ["## 4 Component and Class Design", ""]
    table = _render_component_table(mapping)
    if table:
        lines.append(table)
        lines.append("")
    api_table = _render_api_table(mapping)
    if api_table:
        lines.append("### Module Interfaces and APIs")
        lines.append("")
        lines.append(api_table)
        lines.append("")
    return "\n".join(lines)


def _render_section_6(bundle: Dict[str, Any]) -> str:
    deps_in = bundle.get("dependencies_in") or []
    deps_out = bundle.get("dependencies_out") or []
    if not deps_in and not deps_out:
        return ""
    lines = ["## 6 External System/Module Interface Design", ""]
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
    plan["diagrams_generated"] = list(diagrams.keys())
    plan["diagram_sources"] = diagram_sources

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
            if mod.get("detailed_responsibility"):
                intro_parts.append(mod["detailed_responsibility"])
            elif bundle.get("hld_excerpt"):
                intro_parts.append(bundle["hld_excerpt"][:1500])
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
        sections.append(_render_section_2(bundle, diagrams.get("architecture", "")))

    if include.get("use_case_flow"):
        flow_md = _render_use_case_flow(bundle.get("filtered_flows", []))
        if flow_md or diagrams.get("use_case"):
            uc_parts = ["## 3 Use Case Flow", ""]
            if flow_md:
                uc_parts.append(flow_md)
            if diagrams.get("use_case"):
                uc_parts.append("#### Use case flow diagram")
                uc_parts.append("")
                uc_parts.append(_mermaid_fence(diagrams["use_case"]))
                uc_parts.append("")
            sections.append("\n".join(uc_parts))

    if include.get("component_design"):
        sec4 = _render_section_4(bundle)
        if diagrams.get("class"):
            sec4 += "\n### Component class diagram\n\n" + _mermaid_fence(diagrams["class"]) + "\n"
        sections.append(sec4)

    if include.get("sequence_flow"):
        seq_parts = ["## 5 Sequence Flow", "", "### 5.1 Sequence Overview", ""]
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
        ext = _render_section_6(bundle)
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
            json.dump({**plan, "diagram_report": diagram_report}, fh, indent=2, ensure_ascii=False)

        out_name = f"MDD_{slug}_{resolved_ticket}.md"
        out_path = os.path.join(mdd_dir, out_name)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(mdd_clean)

        generated.append(MDDModuleResult(
            module_name=logical_name,
            slug=slug,
            artifact_path=out_path,
            plan={**plan, "diagram_report": diagram_report},
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
