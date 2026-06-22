"""
SOP-036 MDD template schema and dynamic section inclusion helpers.

Section outline derived from:
  SOP-036_Rev_04_attachments_Attachment 2 MDD template_SOP036.docx
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


MDD_SECTIONS = [
    {"key": "introduction", "number": "1", "title": "Introduction", "subsections": [
        {"key": "purpose", "number": "1.1", "title": "Purpose"},
        {"key": "target_audience", "number": "1.2", "title": "Target Audience"},
        {"key": "scope", "number": "1.3", "title": "Scope"},
        {"key": "definitions", "number": "1.4", "title": "Definitions and Acronyms"},
        {"key": "conventions", "number": "1.5", "title": "Conventions and Standards Followed"},
    ]},
    {"key": "module_architecture", "number": "2", "title": "Module Architecture Overview", "subsections": [
        {"key": "architecture_overview", "number": "2.1", "title": "Architecture Overview"},
        {"key": "assumptions_design_decisions", "number": "2.2", "title": "Assumptions and Design Decisions Made"},
    ]},
    {"key": "use_case_flow", "number": "3", "title": "Use Case Flow", "subsections": []},
    {"key": "component_design", "number": "4", "title": "Component and Class Design", "subsections": []},
    {"key": "sequence_flow", "number": "5", "title": "Sequence Flow", "subsections": [
        {"key": "sequence_overview", "number": "5.1", "title": "Sequence Overview"},
    ]},
    {"key": "external_interfaces", "number": "6", "title": "External System/Module Interface Design", "subsections": [
        {"key": "inputs", "number": "6.1", "title": "Input received from external Systems and Modules"},
        {"key": "outputs", "number": "6.2", "title": "Output given to external Systems and Modules"},
    ]},
    {"key": "data_model_design", "number": "7", "title": "Data Model Design", "subsections": []},
    {"key": "annexure", "number": "8", "title": "Annexure", "subsections": [
        {"key": "traceability", "number": "8.1", "title": "Requirements Traceability Matrix"},
    ]},
]

MDD_SECTION_CONTRACT = {
    "purpose": {
        "heading": "### 1.1 Purpose",
        "must_cover": [
            "module purpose",
            "source requirements and mapped module context",
            "business or system capability supported by the module",
        ],
    },
    "scope": {
        "heading": "### 1.3 Scope",
        "must_cover": [
            "in-scope responsibilities",
            "out-of-scope responsibilities or missing scope called out as To be confirmed",
            "source requirements and mapped module context",
        ],
    },
    "target_audience": {
        "heading": "### 1.2 Target Audience",
        "must_cover": ["intended technical reviewers and implementation teams"],
    },
    "definitions": {
        "heading": "### 1.4 Definitions and Acronyms",
        "must_cover": ["terms and acronyms present in source documentation"],
    },
    "conventions": {
        "heading": "### 1.5 Conventions and Standards Followed",
        "must_cover": [
            "naming, traceability, and documentation conventions used in this generated MDD",
            "standards inferred from source artifacts",
        ],
    },
    "architecture_overview": {
        "heading": "### 2.1 Architecture Overview",
        "must_cover": [
            "module role in the HLD architecture",
            "target projects and mapped code symbols",
            "existing components touched",
            "new or modified behavior required by the feature",
            "dependencies and HLD design decisions",
        ],
    },
    "assumptions_design_decisions": {
        "heading": "### 2.2 Assumptions and Design Decisions Made",
        "must_cover": [
            "HLD design decisions relevant to the module",
            "assumptions caused by missing implementation evidence",
            "explicit To be confirmed statements for unknown details",
        ],
    },
    "use_case_flow": {
        "heading": "## 3 Use Case Flow",
        "must_cover": [
            "trigger and preconditions",
            "main success path",
            "alternate or exception paths when present",
            "business rules mapped to source AC/BL IDs when available",
            "classes or APIs participating in each step",
        ],
    },
    "component_design": {
        "heading": "## 4 Component and Class Design",
        "must_cover": [
            "existing classes and source files",
            "role of each class in this module",
            "existing methods used or modified",
            "new or modified methods / DTOs / contracts, or To be confirmed",
            "validation and error handling considerations",
        ],
    },
    "sequence_overview": {
        "heading": "### 5.1 Sequence Overview",
        "must_cover": [
            "implementation-level interaction sequence",
            "participant responsibilities",
            "data passed between participants",
            "module-specific behavior, not only HLD summary",
        ],
    },
    "external_interfaces": {
        "heading": "## 6 External System/Module Interface Design",
        "must_cover": [
            "input interfaces and provider modules",
            "output interfaces and consuming modules",
            "request/response payload expectations when available",
            "mapped symbols and source files",
            "validation, error, and missing-data behavior when available",
        ],
    },
    "data_model_design": {
        "heading": "## 7 Data Model Design",
        "must_cover": [
            "DTOs, entities, request/response payloads, and contracts available from code graph evidence",
            "source files and mapped symbols for each data model",
            "To be confirmed when model structure is unavailable",
        ],
    },
    "traceability": {
        "heading": "### 8.1 Requirements Traceability Matrix",
        "must_cover": [
            "module-relevant AC/BL mappings only",
            "mapped code symbol or Not mapped in code graph",
        ],
    },
}

MDD_QUALITY_RULES = [
    "Keep the SOP-036 headings and order fixed.",
    "Do not paste raw HLD excerpts into the final MDD.",
    "Do not invent classes, APIs, DTOs, infrastructure, or persistence stores.",
    "Use normalized code symbols from code_graph.mapping only.",
    "Use 'To be confirmed' for implementation detail not present in source artifacts.",
    "Keep traceability scoped to the selected module.",
    "Keep factual tables and diagrams grounded in requirements, HLD, contract, and code_graph evidence.",
    "Every Mermaid block must pass structural validation.",
]

PLAN_JSON_SCHEMA = """
{
  "module_name": "<string>",
  "include_sections": {
    "introduction": true,
    "purpose": true,
    "target_audience": true,
    "scope": true,
    "definitions": true,
    "conventions": true,
    "module_architecture": true,
    "architecture_overview": true,
    "assumptions_design_decisions": true,
    "use_case_flow": true,
    "component_design": true,
    "sequence_flow": true,
    "sequence_overview": true,
    "external_interfaces": true,
    "inputs": true,
    "outputs": true,
    "data_model_design": true,
    "annexure": true,
    "traceability": true
  },
  "reasoning": "<string>"
}
""".strip()


def slugify_module_name(name: str) -> str:
    """Food Module -> Food_Module (safe for filenames)."""
    slug = re.sub(r"[^\w\s-]", "", name.strip())
    slug = re.sub(r"[\s-]+", "_", slug)
    return slug.strip("_") or "module"


def _has_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_text(item) for item in value)
    if isinstance(value, dict):
        return any(_has_text(v) for v in value.values())
    return bool(value)


def mdd_section_has_content(section_key: str, bundle: Dict[str, Any]) -> bool:
    """Return True when upstream bundle has enough data to include an MDD section."""
    if section_key == "architecture_overview":
        return bool(
            bundle.get("logical_name")
            or bundle.get("requirements_module")
            or bundle.get("hld_excerpt")
        )

    if section_key == "module_architecture":
        return mdd_section_has_content("architecture_overview", bundle)

    if section_key == "purpose":
        mod = bundle.get("requirements_module") or {}
        return _has_text(mod.get("detailed_responsibility")) or _has_text(bundle.get("hld_excerpt"))

    if section_key == "scope":
        mod = bundle.get("requirements_module") or {}
        return _has_text(mod.get("detailed_responsibility")) or _has_text(mod.get("capabilities"))

    if section_key == "target_audience":
        return True

    if section_key == "definitions":
        intro = bundle.get("hld_intro") or {}
        defs = intro.get("1_2_definitions_and_acronyms") or intro.get("definitions_and_acronyms") or {}
        defs = intro.get("1_4_definitions_and_acronyms") or defs
        if isinstance(defs, dict):
            return _has_text(defs.get("terms")) or _has_text(defs)
        # `defs` is often already a list of {term, expansion, definition}
        return _has_text(defs)

    if section_key == "conventions":
        return True

    if section_key == "assumptions_design_decisions":
        return bool(bundle.get("assumptions_and_decisions") or bundle.get("architecture_decisions"))

    if section_key == "introduction":
        return any(
            mdd_section_has_content(k, bundle)
            for k in ("purpose", "target_audience", "scope", "definitions", "conventions")
        )

    if section_key == "use_case_flow":
        return bool(bundle.get("use_cases") or bundle.get("filtered_flows"))

    if section_key == "component_design":
        mapping = bundle.get("mapping") or {}
        return bool(mapping.get("codebase_mappings"))

    if section_key == "sequence_overview":
        return bool(bundle.get("sequence_flows") or bundle.get("filtered_flows") or bundle.get("sequence_diagrams"))

    if section_key == "sequence_flow":
        return mdd_section_has_content("sequence_overview", bundle)

    if section_key == "inputs":
        return bool(bundle.get("dependencies_in"))

    if section_key == "outputs":
        return bool(bundle.get("dependencies_out"))

    if section_key == "external_interfaces":
        return mdd_section_has_content("inputs", bundle) or mdd_section_has_content("outputs", bundle)

    if section_key == "data_model_design":
        return bool(bundle.get("data_models"))

    if section_key == "traceability":
        return bool(bundle.get("filtered_acs"))

    if section_key == "annexure":
        return mdd_section_has_content("traceability", bundle)

    return False


def _mdd_section_has_content(section_key: str, bundle: Dict[str, Any]) -> bool:
    """
    Shared helper for dynamic section omission.

    Kept as a thin wrapper because the plan expects this exact helper name.
    """
    return mdd_section_has_content(section_key, bundle)


def normalize_mdd_plan(plan: Dict[str, Any], bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Override LLM planner with deterministic section inclusion from bundle content."""
    include = plan.setdefault("include_sections", {})

    section_keys = [
        "purpose", "target_audience", "scope", "definitions", "conventions", "introduction",
        "architecture_overview", "assumptions_design_decisions", "module_architecture",
        "use_case_flow", "component_design",
        "sequence_overview", "sequence_flow",
        "inputs", "outputs", "external_interfaces",
        "data_model_design",
        "traceability", "annexure",
    ]
    for key in section_keys:
        include[key] = _mdd_section_has_content(key, bundle)

    included = [k for k in section_keys if include.get(k)]
    skipped = [k for k in section_keys if not include.get(k)]
    plan["sections_included"] = included
    plan["sections_skipped"] = skipped
    plan["module_name"] = bundle.get("logical_name", plan.get("module_name", ""))
    return plan


def markdown_table_cell(value: str) -> str:
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(line.strip() for line in text.split("\n") if line.strip())
    return text.replace("|", "/")
