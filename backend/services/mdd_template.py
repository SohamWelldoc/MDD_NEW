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
        {"key": "purpose_and_scope", "number": "1.1", "title": "Purpose and Scope"},
        {"key": "definitions", "number": "1.2", "title": "Definitions and Acronyms"},
        {"key": "references", "number": "1.3", "title": "References"},
    ]},
    {"key": "module_architecture", "number": "2", "title": "Module Architecture Overview", "subsections": [
        {"key": "architecture_overview", "number": "2.1", "title": "Architecture Overview"},
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
    {"key": "annexure", "number": "7", "title": "Annexure", "subsections": [
        {"key": "traceability", "number": "7.1", "title": "Requirements Traceability Matrix"},
    ]},
]

PLAN_JSON_SCHEMA = """
{
  "module_name": "<string>",
  "include_sections": {
    "introduction": true,
    "purpose_and_scope": true,
    "definitions": true,
    "references": true,
    "module_architecture": true,
    "architecture_overview": true,
    "use_case_flow": true,
    "component_design": true,
    "sequence_flow": true,
    "sequence_overview": true,
    "external_interfaces": true,
    "inputs": true,
    "outputs": true,
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

    if section_key == "purpose_and_scope":
        mod = bundle.get("requirements_module") or {}
        return _has_text(mod.get("detailed_responsibility")) or _has_text(bundle.get("hld_excerpt"))

    if section_key == "definitions":
        intro = bundle.get("hld_intro") or {}
        defs = intro.get("1_2_definitions_and_acronyms") or intro.get("definitions_and_acronyms") or {}
        if isinstance(defs, dict):
            return _has_text(defs.get("terms")) or _has_text(defs)
        # `defs` is often already a list of {term, expansion, definition}
        return _has_text(defs)

    if section_key == "references":
        intro = bundle.get("hld_intro") or {}
        refs = intro.get("1_3_references") or intro.get("references") or {}
        if isinstance(refs, dict):
            return _has_text(refs.get("documents")) or _has_text(refs)
        # `refs` is often already a list of {title, url_or_location, relationship_description}
        return _has_text(refs)

    if section_key == "introduction":
        return any(
            mdd_section_has_content(k, bundle)
            for k in ("purpose_and_scope", "definitions", "references")
        )

    if section_key == "use_case_flow":
        return bool(bundle.get("filtered_flows"))

    if section_key == "component_design":
        mapping = bundle.get("mapping") or {}
        return bool(mapping.get("codebase_mappings"))

    if section_key == "sequence_overview":
        return bool(bundle.get("filtered_flows") or bundle.get("sequence_diagrams"))

    if section_key == "sequence_flow":
        return mdd_section_has_content("sequence_overview", bundle)

    if section_key == "inputs":
        return bool(bundle.get("dependencies_in"))

    if section_key == "outputs":
        return bool(bundle.get("dependencies_out"))

    if section_key == "external_interfaces":
        return mdd_section_has_content("inputs", bundle) or mdd_section_has_content("outputs", bundle)

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
        "purpose_and_scope", "definitions", "references", "introduction",
        "architecture_overview", "module_architecture",
        "use_case_flow", "component_design",
        "sequence_overview", "sequence_flow",
        "inputs", "outputs", "external_interfaces",
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
