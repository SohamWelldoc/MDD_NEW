"""
HLD Generator
=============

Orchestrates the final High-Level Design document.

Inputs
------
  * `requirements.json`  — produced by `requirements_generator.py`
  * `code_graph.json`    — produced by `codebase_analyzer.py`

Pipeline
--------
  Pass 0 — Section planner. LLM decides which SOP-036 sections apply and
           returns a structured `plan.json`.
  Pass 1 — Generate the HLD markdown using the plan + both artifacts.
  Pass 2 — Sanitize all Mermaid diagrams and run structural validation.
  Pass 3 — Persist `HLD.md`, `plan.json`, and a manifest.

Each pass is a single LLM call so the surface stays small. The HLD is
emitted as Markdown with embedded ```mermaid blocks.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar

ProgressCallback = Callable[[int, str], None]
T = TypeVar("T")


def _run_with_progress_pulse(
    progress: ProgressCallback,
    progress_pct: int,
    message: str,
    action: Callable[[], T],
    *,
    interval_seconds: float = 8.0,
) -> T:
    """Keep the status message fresh while a long-running step is in progress."""
    stop = threading.Event()
    start = time.monotonic()

    def pulse() -> None:
        while not stop.wait(interval_seconds):
            elapsed = int(time.monotonic() - start)
            progress(progress_pct, f"{message} ({elapsed}s elapsed...)")

    progress(progress_pct, message)
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        stop.set()
        thread.join(timeout=0.2)

from services.shared.llm_client import get_llm_client
from services.shared.mermaid_utils import postprocess_mermaid, validate_diagrams
from services.hld.hld_validator import validate_hld
from services.hld.hld_mermaid_builders import build_hld_diagrams
from services.artifact_store.artifact_paths import artifact_context
from services.shared.docx_exporter import markdown_to_docx

# Import query functions from agentic-orchestrator statically
import sys
from pathlib import Path
_ORCHESTRATOR_DIR = str(Path(__file__).resolve().parent.parent.parent / "agentic-orchestrator")
if _ORCHESTRATOR_DIR not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_DIR)

try:
    from query import resolve_symbol, find_neighbors, canonical, load_indexes  # type: ignore
except ImportError:
    resolve_symbol = None
    find_neighbors = None
    canonical = None
    load_indexes = None


# ----------------------------------------------------------------------
# Schemas (documented inline so the LLM has a strict contract)
# ----------------------------------------------------------------------
PLAN_JSON_SCHEMA = """
{
  "project_name": "<string>",
  "include_sections": {
    "introduction": true,
    "definitions_and_acronyms": true,
    "references": true,
    "context": true,
    "logical_view": true,
    "security": true,
    "scalability": true,
    "infrastructure": true
  },
  "modules": [
    { "name": "<string>", "responsibility": "<string>" }
  ],
  "diagrams_required": {
    "top_level_architecture": true,
    "combined_modules": false,
    "infrastructure_topology": true
  },
  "reasoning": "<why these sections were chosen>"
}
""".strip()


@dataclass
class HLDResult:
    job_id: str
    started_at: str
    completed_at: str
    plan: Dict[str, Any]
    hld_markdown: str
    diagram_report: Dict[str, Any]
    artifact_paths: Dict[str, str]


# ----------------------------------------------------------------------
# Artifact loading
# ----------------------------------------------------------------------
def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_default_artifact(artifact_dir: str, kind: str) -> Dict[str, Any]:
    """Load default `<kind>.json` from the artifact dir."""
    path = os.path.join(artifact_dir, f"{kind}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Required artifact missing: {path}. "
            f"Run the {'requirements' if kind == 'requirements' else 'codebase'} step first."
        )
    return _load_json(path)


# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------
_PLANNER_SYSTEM = (
    "You are a senior architect deciding which sections of an SOP-036 HLD "
    "should be included based on requirements and code structure. "
    "Return STRICT JSON only. "
    "NOTE: Sections 3 (security), 4 (scalability), and 5 (infrastructure) are "
    "non-functional requirements — their inclusion is decided automatically from "
    "requirements.json emptiness; do not force them on. "
    "For modules, prefer the target_projects from codebase over inventing new module names."
)


def _section_has_content(section: Dict[str, Any]) -> bool:
    """Return True if any leaf value in a requirements section is non-empty."""
    if not section:
        return False
    if isinstance(section, str):
        return bool(section.strip())
    if isinstance(section, list):
        return any(_section_has_content(item) for item in section)
    if isinstance(section, dict):
        return any(_section_has_content(v) for v in section.values())
    return bool(section)


def _nfr_include_flags(requirements: Dict[str, Any]) -> Dict[str, bool]:
    """NFR sections (§3–§5) are included only when requirements.json has content."""
    hld = requirements.get("hld_content", {})
    return {
        "security": _section_has_content(hld.get("3_security_approach", {})),
        "scalability": _section_has_content(hld.get("4_scalability_view", {})),
        "infrastructure": _section_has_content(hld.get("5_infrastructure_view", {})),
    }


def _document_topic(requirements: Dict[str, Any], plan: Dict[str, Any]) -> str:
    title = os.getenv("HLD_DOCUMENT_TITLE")
    if title:
        return re.sub(r"\s+high[- ]level\s+design$", "", title.strip(), flags=re.IGNORECASE)

    intro = requirements.get("hld_content", {}).get("1_introduction", {})
    scope = intro.get("1_1_purpose_and_scope", {})
    in_scope = scope.get("in_scope") if isinstance(scope, dict) else None
    if isinstance(in_scope, list) and in_scope:
        topic = str(in_scope[0]).strip()
        topic = re.sub(r"\s+feature$", "", topic, flags=re.IGNORECASE)
        if topic:
            return topic

    project = plan.get("project_name") or requirements.get("project_name") or "System"
    return str(project).strip() or "System"


def _mermaid_id(value: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "", "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", value) if part))
    if not base:
        base = "Node"
    if base[0].isdigit():
        base = f"N{base}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}{index}"
        index += 1
    used.add(candidate)
    return candidate


def _clean_mermaid_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace('"', "'")).strip()


def _short_mermaid_label(value: str, limit: int = 58) -> str:
    """Keep diagram labels readable while preserving source wording."""
    label = _clean_mermaid_label(value)
    if len(label) <= limit:
        return label
    return label[: limit - 1].rstrip() + "..."


def _class_label(mapping: Dict[str, Any]) -> str:
    symbol = mapping.get("class_name") or mapping.get("normalized_symbol") or mapping.get("codebase_symbol") or "Component"
    if "." in symbol and not symbol.startswith("."):
        symbol = symbol.split(".", 1)[0]
    if symbol.startswith("."):
        source_file = mapping.get("source_file") or ""
        symbol = Path(source_file.replace("\\", "/")).stem or symbol.lstrip(".")
    methods = []
    for method in mapping.get("method_impacts") or mapping.get("methods") or []:
        if isinstance(method, dict):
            name = method.get("method_name") or method.get("display_name") or method.get("method") or ""
        else:
            name = str(method)
        name = name.strip(".").replace("()", "")
        if name and name not in methods:
            methods.append(name)
        if len(methods) == 2:
            break
    method_text = f"<br/>{', '.join(methods)}" if methods else ""
    return _short_mermaid_label(f"{symbol}{method_text}", 70)


def _module_mapping_by_name(code_graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    modules = code_graph.get("mapping", {}).get("mapped_modules", []) or []
    return {str(module.get("module_name", "")).lower(): module for module in modules if module.get("module_name")}


def _deterministic_context_diagram(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> str:
    intro = requirements.get("hld_content", {}).get("1_introduction", {})
    context = intro.get("1_4_context", {}) if isinstance(intro, dict) else {}
    upstream = context.get("upstream_dependencies", []) if isinstance(context, dict) else []
    downstream = context.get("downstream_consumers", []) if isinstance(context, dict) else []
    topic = _document_topic(requirements, {})
    req_modules = requirements.get("hld_content", {}).get("2_logical_view", {}).get("modules", []) or []
    mapped_by_name = _module_mapping_by_name(code_graph)

    used: set[str] = set()
    system_id = _mermaid_id(topic, used)
    lines = [
        "flowchart LR",
        f'    {system_id}["{_short_mermaid_label(topic)}<br/>High-Level Design scope"]',
    ]

    for idx, item in enumerate(upstream[:4], start=1):
        name = item.get("system_name") or item.get("name") or f"Upstream {idx}"
        trigger = item.get("trigger_event") or item.get("mechanism") or "provides input"
        node_id = _mermaid_id(name, used)
        lines.append(f'    {node_id}["{_short_mermaid_label(name)}"] -->|"{_short_mermaid_label(trigger, 36)}"| {system_id}')

    for idx, module in enumerate(req_modules[:4], start=1):
        name = module.get("module_name") or f"Module {idx}"
        mod_id = _mermaid_id(name, used)
        caps = module.get("capabilities") or []
        cap_label = f"<br/>{_short_mermaid_label(', '.join(caps[:2]), 44)}" if caps else ""
        lines.append(f'    {system_id} --> {mod_id}["{_short_mermaid_label(name)}{cap_label}"]')
        mapped_module = mapped_by_name.get(str(name).lower()) or {}
        for mapping in (mapped_module.get("codebase_mappings") or [])[:3]:
            class_id = _mermaid_id(mapping.get("normalized_symbol") or mapping.get("codebase_symbol") or "Component", used)
            lines.append(f'    {mod_id} --> {class_id}["{_class_label(mapping)}"]')

    target_projects = code_graph.get("target_projects", []) or []
    if target_projects:
        project_group = _mermaid_id("Target Projects", used)
        project_names = ", ".join(Path(str(p).replace("\\", "/")).name for p in target_projects[:4])
        lines.append(f'    {project_group}["Target projects<br/>{_short_mermaid_label(project_names, 68)}"]')
        lines.append(f"    {system_id} -.-> {project_group}")

    for decision in code_graph.get("resolved_at_checkpoint_b", []) or []:
        if "jsonrepository" in str(decision).lower() or "mongo" in str(decision).lower():
            repo_id = _mermaid_id("JSONRepository Mongo", used)
            lines.append(f'    {system_id} -->|"{_clean_mermaid_label("persistence decision")}"| {repo_id}["JSONRepository / Mongo"]')
            break

    for idx, item in enumerate(downstream[:4], start=1):
        name = item.get("system_name") or item.get("name") or f"Downstream {idx}"
        data = item.get("data_transmitted") or item.get("mechanism") or "consumes output"
        node_id = _mermaid_id(name, used)
        lines.append(f'    {system_id} -->|"{_short_mermaid_label(data, 42)}"| {node_id}["{_short_mermaid_label(name)}"]')

    if len(lines) == 2:
        lines.append(f'    User["User"] -->|"uses"| {system_id}')
        lines.append(f'    {system_id} -->|"returns result"| User')

    return "\n".join(lines)


def _deterministic_sequence_diagram(code_graph: Dict[str, Any]) -> str:
    flows = code_graph.get("mapping", {}).get("mapped_flows", []) or []
    flow = flows[0] if flows else {}
    steps = sorted(flow.get("steps", []) or [], key=lambda step: step.get("step_number", 0))
    if not steps:
        return ""

    used_ids: set[str] = set()
    participant_by_label: Dict[str, tuple[str, str]] = {}

    def participant(label: str) -> tuple[str, str]:
        label = _short_mermaid_label(label or "Component", 48)
        if label not in participant_by_label:
            participant_by_label[label] = (_mermaid_id(label, used_ids), label)
        return participant_by_label[label]

    messages: List[tuple[str, str, str]] = []
    for step in steps[:6]:
        src_component = step.get("source_component") or "Source"
        dst_component = step.get("destination_component") or "Destination"
        mappings = step.get("codebase_mappings") or []
        mapped_labels = []
        for mapping in mappings[:4]:
            label = mapping.get("class_name") or mapping.get("normalized_symbol") or mapping.get("codebase_symbol")
            if label and label not in mapped_labels:
                mapped_labels.append(label)
        chain = [src_component] + mapped_labels + [dst_component]
        chain = [item for index, item in enumerate(chain) if item and item not in chain[:index]]
        operation = _short_mermaid_label(step.get("operation_signature") or f"{src_component} to {dst_component}", 70)
        for index in range(len(chain) - 1):
            from_id, _ = participant(chain[index])
            to_id, _ = participant(chain[index + 1])
            label = operation if index == 0 else "mapped call"
            messages.append((from_id, to_id, label))

    lines = ["sequenceDiagram"]
    for pid, label in participant_by_label.values():
        lines.append(f'    participant {pid} as "{_clean_mermaid_label(label)}"')
    for src_id, dst_id, operation in messages:
        lines.append(f"    {src_id}->>{dst_id}: {operation}")
    return "\n".join(lines)


def _replace_mermaid_blocks_with_deterministic(
    markdown: str,
    requirements: Dict[str, Any],
    code_graph: Dict[str, Any],
) -> str:
    context_diagram = _deterministic_context_diagram(requirements, code_graph)
    sequence_diagram = _deterministic_sequence_diagram(code_graph)
    replacements = [context_diagram, sequence_diagram]
    index = 0

    def replace(match: re.Match) -> str:
        nonlocal index
        if index >= len(replacements) or not replacements[index]:
            index += 1
            return match.group(0)
        block = replacements[index]
        index += 1
        return f"```mermaid\n{block}\n```"

    updated = re.sub(r"```mermaid\s*\n(.*?)```", replace, markdown, flags=re.DOTALL)
    if sequence_diagram and sequence_diagram not in updated:
        sequence_block = f"\n```mermaid\n{sequence_diagram}\n```\n"
        flow_heading = re.search(r"(####\s+[^\n]*Flow[^\n]*\n)", updated, flags=re.IGNORECASE)
        if flow_heading:
            insert_at = flow_heading.end()
            updated = updated[:insert_at] + sequence_block + updated[insert_at:]
        else:
            interactions_heading = re.search(r"(###\s+2\.\d+\s+Interactions and Flows[^\n]*\n)", updated, flags=re.IGNORECASE)
            if interactions_heading:
                insert_at = interactions_heading.end()
                updated = updated[:insert_at] + "\n#### Primary Flow\n" + sequence_block + updated[insert_at:]
            else:
                updated += "\n\n### 2.y Interactions and Flows\n#### Primary Flow\n" + sequence_block
    return updated


def _mermaid_fence(block: str) -> str:
    return "```mermaid\n" + block.strip() + "\n```" if block and block.strip() else ""


def _strip_mermaid_blocks(markdown: str) -> str:
    """Remove LLM-authored Mermaid so final diagrams are deterministic builders only."""
    return re.sub(r"\n?```mermaid\s*\n.*?```\n?", "\n\n", markdown, flags=re.DOTALL)


def _insert_after_heading(markdown: str, heading_pattern: str, content: str) -> str:
    if not content.strip():
        return markdown
    match = re.search(heading_pattern, markdown, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return markdown + "\n\n" + content.strip() + "\n"
    insert_at = match.end()
    return markdown[:insert_at] + "\n\n" + content.strip() + "\n" + markdown[insert_at:]


def _insert_before_section(markdown: str, section_pattern: str, content: str) -> str:
    if not content.strip():
        return markdown
    match = re.search(section_pattern, markdown, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return markdown + "\n\n" + content.strip() + "\n"
    insert_at = match.start()
    return markdown[:insert_at].rstrip() + "\n\n" + content.strip() + "\n\n" + markdown[insert_at:]


def _section_two_bounds(markdown: str) -> tuple[int, int] | None:
    section2_match = re.search(r"^##\s+2\s+Logical View[^\n]*$", markdown, flags=re.IGNORECASE | re.MULTILINE)
    if not section2_match:
        return None
    section_start = section2_match.end()
    section_rest = markdown[section_start:]
    section3 = re.search(r"^##\s+3\b", section_rest, flags=re.MULTILINE)
    section_end = section_start + (section3.start() if section3 else len(section_rest))
    return section_start, section_end


def _subsection_end_pos(markdown: str, heading_end: int, section_end: int) -> int:
    after_heading = markdown[heading_end:section_end]
    next_sub = re.search(r"^###\s+", after_heading, flags=re.MULTILINE)
    if next_sub:
        return heading_end + next_sub.start()
    return section_end


def _find_logical_view_diagram_insert_pos(markdown: str) -> int:
    """Insert diagram subsections after contract/evidence blocks (2.0.x), not before them."""
    bounds = _section_two_bounds(markdown)
    if not bounds:
        return -1
    section_start, section_end = bounds
    section_text = markdown[section_start:section_end]
    reserved_patterns = [
        r"^###\s+2\.0\.2\s+Open Questions[^\n]*",
        r"^###\s+2\.0\.1\s+Evidence[^\n]*",
        r"^###\s+2\.0\s+Architecture Decisions[^\n]*",
    ]
    insert_pos = section_start
    for pattern in reserved_patterns:
        match = re.search(pattern, section_text, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            continue
        heading_end = section_start + match.end()
        insert_pos = max(insert_pos, _subsection_end_pos(markdown, heading_end, section_end))
    return insert_pos


def _insert_at_position(markdown: str, insert_pos: int, content: str) -> str:
    if not content.strip() or insert_pos < 0:
        return markdown
    return markdown[:insert_pos].rstrip() + "\n\n" + content.strip() + "\n\n" + markdown[insert_pos:].lstrip()


_RESERVED_SECTION_TWO_TITLES = {
    "architecture decisions (checkpoint b)",
    "evidence and confidence summary",
    "open questions and to be confirmed",
    "feature architecture flow",
    "primary interaction sequence",
    "feature lifecycle",
    "architecture decisions and evidence",
}


def _is_reserved_section_two_heading(title: str) -> bool:
    lowered = title.strip().lower()
    return lowered in _RESERVED_SECTION_TWO_TITLES


def _normalize_section_two_numbering(markdown: str) -> str:
    """Keep 2.0.x and diagram blocks fixed; renumber module/interaction/traceability subsections from 2.5."""
    bounds = _section_two_bounds(markdown)
    if not bounds:
        return markdown
    section_start, section_end = bounds
    section_text = markdown[section_start:section_end]
    heading_re = re.compile(r"^(###)\s+2\.(?:\d+(?:\.\d+)?|[xyz])\s+(.+)$", flags=re.MULTILINE | re.IGNORECASE)
    next_number = 5
    parts: List[str] = []
    last_end = 0
    for match in heading_re.finditer(section_text):
        parts.append(section_text[last_end:match.start()])
        title = match.group(2).strip()
        if _is_reserved_section_two_heading(title):
            parts.append(match.group(0))
        else:
            parts.append(f"### 2.{next_number} {title}")
            next_number += 1
        last_end = match.end()
    parts.append(section_text[last_end:])
    normalized_section = "".join(parts)
    return markdown[:section_start] + normalized_section + markdown[section_end:]


def _insert_deterministic_hld_diagrams(
    markdown: str,
    requirements: Dict[str, Any],
    code_graph: Dict[str, Any],
    plan: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    diagrams, facts = build_hld_diagrams(requirements, code_graph, plan)
    clean = _strip_mermaid_blocks(markdown)

    context_block = ""
    if diagrams.get("hld_context"):
        context_block = "\n\n".join([
            "#### System Context Diagram",
            "This diagram shows the feature boundary, external inputs/outputs, target projects, and major implementation layers.",
            _mermaid_fence(diagrams["hld_context"]),
        ])
    clean = _insert_after_heading(clean, r"^###\s+1\.4\s+Context[^\n]*", context_block)

    logical_blocks: List[str] = []
    if diagrams.get("hld_feature_flow"):
        logical_blocks.extend([
            "### 2.1 Feature Architecture Flow",
            "This diagram explains the end-to-end movement from trigger to outcome using mapped components and contract evidence.",
            _mermaid_fence(diagrams["hld_feature_flow"]),
        ])
    if diagrams.get("hld_primary_sequence"):
        logical_blocks.extend([
            "### 2.2 Primary Interaction Sequence",
            "This diagram shows the primary runtime interaction order between mapped participants.",
            _mermaid_fence(diagrams["hld_primary_sequence"]),
        ])
    if diagrams.get("hld_lifecycle"):
        logical_blocks.extend([
            "### 2.3 Feature Lifecycle",
            "This diagram highlights time-based behavior, eligibility, materialization, and display states found in requirements or contract evidence.",
            _mermaid_fence(diagrams["hld_lifecycle"]),
        ])
    if diagrams.get("hld_decision_view"):
        logical_blocks.extend([
            "### 2.4 Architecture Decisions and Evidence",
            "This diagram summarizes fixed architecture decisions and unresolved areas that influence implementation.",
            _mermaid_fence(diagrams["hld_decision_view"]),
        ])
    logical_block = "\n\n".join(block for block in logical_blocks if block)
    insert_pos = _find_logical_view_diagram_insert_pos(clean)
    if insert_pos >= 0:
        clean = _insert_at_position(clean, insert_pos, logical_block)
    elif logical_block:
        clean = _insert_after_heading(clean, r"^##\s+2\s+Logical View[^\n]*", logical_block)

    if logical_block:
        clean = _normalize_section_two_numbering(clean)

    infra_block = ""
    if diagrams.get("hld_infrastructure"):
        infra_block = "\n\n".join([
            "### 5.5 Infrastructure Topology Diagram",
            "This diagram is limited to target projects and infrastructure/persistence decisions found in source artifacts.",
            _mermaid_fence(diagrams["hld_infrastructure"]),
        ])
        clean = _insert_after_heading(clean, r"^##\s+5\s+Infrastructure View[^\n]*", infra_block)

    if logical_block and "## 2 Logical View" not in clean:
        clean = _insert_before_section(clean, r"^##\s+3\b", "## 2 Logical View\n\n" + logical_block)

    return clean, {
        "sources": {key: "deterministic" for key in diagrams},
        "coverage": facts.get("coverage", {}),
        "diagram_names": list(diagrams.keys()),
    }


def _normalize_plan(
    plan: Dict[str, Any],
    requirements: Dict[str, Any],
    code_graph: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply deterministic rules after LLM planner. NFR sections follow requirements only."""
    include = plan.setdefault("include_sections", {})
    include["introduction"] = True
    include["logical_view"] = True

    nfr = _nfr_include_flags(requirements)
    include["security"] = nfr["security"]
    include["scalability"] = nfr["scalability"]
    include["infrastructure"] = nfr["infrastructure"]

    plan["nfr_sections"] = {
        "source": "requirements.json",
        "included": [k for k, v in nfr.items() if v],
        "skipped": [k for k, v in nfr.items() if not v],
    }

    # Prefer contract target projects for module list
    target_projects = code_graph.get("target_projects", [])
    if target_projects:
        plan["modules"] = [
            {"name": p, "responsibility": f"Welldoc project component for {code_graph.get('contract', {}).get('title', 'this feature')}"}
            for p in target_projects[:8]
        ]

    diagrams = plan.setdefault("diagrams_required", {})
    if not include.get("infrastructure"):
        diagrams["infrastructure_topology"] = False

    return plan


def _planner_user_prompt(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> str:
    hld_content = requirements.get("hld_content", {})
    intro = hld_content.get("1_introduction", {})
    logical = hld_content.get("2_logical_view", {})
    security = hld_content.get("3_security_approach", {})
    scalability = hld_content.get("4_scalability_view", {})
    infrastructure = hld_content.get("5_infrastructure_view", {})

    return "\n".join(
        [
            "Decide which HLD sections to include and which architecture",
            "diagrams are mandatory. Respond with a JSON object matching:",
            "```json",
            PLAN_JSON_SCHEMA,
            "```",
            "",
            "=== REQUIREMENTS (summarized) ===",
            json.dumps(
                {
                    "project_name": requirements.get("project_name", ""),
                    "purpose_and_scope": intro.get("1_1_purpose_and_scope", {}),
                    "modules": logical.get("modules", []),
                    "security": security,
                    "scalability": scalability,
                    "infrastructure": infrastructure,
                },
                indent=2,
                ensure_ascii=False,
            )[:4000],
            "",
            "=== CODEBASE (stats + contract + projects) ===",
            json.dumps(
                {
                    "stats": code_graph.get("stats", {}),
                    "contract": code_graph.get("contract", {}),
                    "target_projects": code_graph.get("target_projects", []),
                    "resolved_at_checkpoint_b": code_graph.get("resolved_at_checkpoint_b", []),
                    "constraints_count": len(code_graph.get("constraints", [])),
                },
                indent=2,
                ensure_ascii=False,
            )[:3000],
            "",
            "Return JSON only.",
        ]
    )


def _get_codebase_context(requirements: Dict[str, Any], code_graph: Optional[Dict[str, Any]] = None) -> str:
    """Build codebase grounding text from code_graph.json (contract seeds + requirements mapping)."""
    lines: List[str] = []

    if code_graph:
        contract_info = code_graph.get("contract", {})
        if contract_info:
            lines.append("### Feature Contract")
            lines.append(f"- **Ticket:** {contract_info.get('ticket', 'n/a')}")
            lines.append(f"- **Title:** {contract_info.get('title', 'n/a')}")
            lines.append("")

        target_projects = code_graph.get("target_projects", [])
        if target_projects:
            lines.append("### Target Projects (from contract)")
            for p in target_projects:
                lines.append(f"- {p}")
            lines.append("")

        decisions = code_graph.get("resolved_at_checkpoint_b", [])
        if decisions:
            lines.append("### Architecture Decisions (Checkpoint B)")
            for d in decisions:
                lines.append(f"- {d}")
            lines.append("")

        out_of_scope = code_graph.get("out_of_scope", [])
        if out_of_scope:
            lines.append("### Out of Scope (from contract)")
            for item in out_of_scope:
                lines.append(f"- {item}")
            lines.append("")

        seed_resolutions = code_graph.get("seed_resolutions", [])
        if seed_resolutions:
            lines.append("### Contract Seed Symbols (resolved in monolith graph)")
            for res in seed_resolutions:
                if not res.get("resolved"):
                    if res.get("is_new_capability"):
                        lines.append(f"- **{res.get('name')}** -> NEW capability (not yet in graph)")
                        if res.get("note"):
                            lines.append(f"  - Note: {res['note'][:300]}")
                    continue
                node = res.get("node") or {}
                lines.append(f"- **{res.get('name')}** -> `{node.get('label', '')}` "
                             f"({node.get('source_file') or res.get('sourceFile') or 'external'})")
                if res.get("note"):
                    lines.append(f"  - Note: {res['note'][:300]}")
                if res.get("callers"):
                    lines.append(f"  - Callers: {', '.join(res['callers'][:5])}")
                if res.get("callees"):
                    lines.append(f"  - Callees: {', '.join(res['callees'][:5])}")
            lines.append("")

        constraints = code_graph.get("constraints", [])
        if constraints:
            lines.append("### Architecture Constraints (from contract)")
            for c in constraints:
                lines.append(f"- {c}")
            lines.append("")

        acs = code_graph.get("acceptance_criteria", [])
        if acs:
            lines.append("### Acceptance Criteria (from contract)")
            for ac in acs[:15]:
                lines.append(f"- **{ac.get('id', '')}:** {ac.get('text', '')[:200]}")
            lines.append("")

    if code_graph and "mapping" in code_graph:
        mapping = code_graph["mapping"]
        mapped_modules = mapping.get("mapped_modules", [])
        mapped_flows = mapping.get("mapped_flows", [])

        lines.append("### Codebase Architecture Grounding (from Pre-mapped Code Graph)")
        lines.append("The following codebase components and method call flows were matched and extracted:")
        lines.append("")

        for mod in mapped_modules:
            lines.append(f"#### Module: {mod['module_name']}")
            cb_list = mod.get("codebase_mappings") or (
                [mod["codebase_mapping"]] if mod.get("codebase_mapping") else []
            )
            for cb in cb_list:
                if not cb:
                    continue
                lines.append(f"- **Codebase Class/Symbol:** `{cb.get('codebase_symbol')}`")
                lines.append(f"- **Source File:** {cb.get('source_file') or '(external/framework)'}")
                if cb.get("note"):
                    lines.append(f"- **Note:** {cb.get('note')}")
                if cb.get("base_classes"):
                    lines.append(f"- **Inherits From:** {', '.join(cb['base_classes'])}")
                if cb.get("implemented_interfaces"):
                    lines.append(f"- **Implements:** {', '.join(cb['implemented_interfaces'])}")
                if cb.get("methods"):
                    lines.append(f"- **Declared Methods:** {', '.join(cb['methods'][:10])}")

            apis = mod.get("interfaces_and_apis", [])
            for api in apis:
                lines.append(f"- **Interface/API:** {api['interface_name']} (`{api['signature']}`)")
                acb_list = api.get("codebase_mappings") or (
                    [api["codebase_mapping"]] if api.get("codebase_mapping") else []
                )
                for acb in acb_list:
                    if not acb:
                        continue
                    lines.append(f"  - **Symbol:** `{acb.get('codebase_symbol')}`")
                    lines.append(f"  - **Defined in:** {acb.get('source_file') or '(external)'}")
                    if acb.get("callers"):
                        lines.append(f"  - **Called By:** {', '.join(acb['callers'][:5])}")
                    if acb.get("callees"):
                        lines.append(f"  - **Calls:** {', '.join(acb['callees'][:5])}")

            deps = mod.get("dependencies", [])
            for dep in deps:
                lines.append(f"- **Depends On:** `{dep.get('dependency')}` -> codebase `{dep.get('codebase_symbol')}`")
            lines.append("")

        for flow in mapped_flows:
            lines.append(f"#### Flow: {flow['flow_name']}")
            for step in flow.get("steps", []):
                scb_list = step.get("codebase_mappings") or (
                    [step["codebase_mapping"]] if step.get("codebase_mapping") else []
                )
                num = step.get("step_number", "")
                src = step.get("source_component", "")
                dst = step.get("destination_component", "")
                op = step.get("operation_signature", "")
                if scb_list:
                    for scb in scb_list:
                        lines.append(
                            f"- Step {num}: `{src}` calls `{dst}` via `{op}` "
                            f"(mapped to codebase `{scb.get('codebase_symbol')}` "
                            f"in file `{scb.get('source_file') or 'external'}`)"
                        )
                        if scb.get("callers"):
                            lines.append(f"  - Callers: {', '.join(scb['callers'][:3])}")
                else:
                    lines.append(f"- Step {num}: `{src}` calls `{dst}` via `{op}` (unresolved codebase match)")
            lines.append("")

        if lines:
            return "\n".join(lines)

    if resolve_symbol is None or find_neighbors is None or load_indexes is None:
        return "Code graph index is unavailable."

    try:
        nodes_index, _ = load_indexes()
    except Exception as e:
        sys.stderr.write(f"Failed to load indexes: {e}\n")
        return "Code graph index is unavailable."

    # 2. Extract potential keywords/symbols from requirements
    hld_content = requirements.get("hld_content", {})
    logical_view = hld_content.get("2_logical_view", {})
    
    symbols_to_query = set()
    
    # Extract from modules
    modules = logical_view.get("modules", [])
    for mod in modules:
        name = mod.get("module_name", "")
        if name:
            symbols_to_query.add(name)
            for part in re.split(r'[\s_\-\.\/]+', name):
                if len(part) > 2:
                    symbols_to_query.add(part)
        
        # Extract from apis
        apis = mod.get("interfaces_and_apis", [])
        for api in apis:
            iname = api.get("interface_name", "")
            if iname:
                symbols_to_query.add(iname)
                for part in re.split(r'[\s_\-\.\/]+', iname):
                    if len(part) > 2:
                        symbols_to_query.add(part)
            sig = api.get("signature", "")
            if sig:
                sig_name = sig.split("(")[0].strip()
                if "." in sig_name:
                    sig_name = sig_name.split(".")[-1]
                if sig_name:
                    symbols_to_query.add(sig_name)

    # Extract from interactions and flows
    flows = logical_view.get("interactions_and_flows", [])
    for flow in flows:
        steps = flow.get("step_by_step_sequence", [])
        for step in steps:
            src = step.get("source_component", "")
            if src:
                symbols_to_query.add(src)
                for part in re.split(r'[\s_\-\.\/]+', src):
                    if len(part) > 2:
                        symbols_to_query.add(part)
            dest = step.get("destination_component", "")
            if dest:
                symbols_to_query.add(dest)
                for part in re.split(r'[\s_\-\.\/]+', dest):
                    if len(part) > 2:
                        symbols_to_query.add(part)
            op = step.get("operation_signature", "")
            if op:
                op_name = op.split("(")[0].strip()
                if "." in op_name:
                    op_name = op_name.split(".")[-1]
                if op_name:
                    symbols_to_query.add(op_name)

    # Clean and filter candidate keywords (discarding common stop words)
    stopwords = {"api", "the", "and", "a", "of", "to", "in", "is", "for", "with", "on", "data", "application", "system", "service", "controller", "endpoint", "endpoints", "model", "prediction", "predictions", "flow", "flows", "event", "events"}
    
    cleaned_keywords = []
    for sym in symbols_to_query:
        cleaned = canonical(sym)
        if cleaned and cleaned not in stopwords and len(cleaned) > 2:
            cleaned_keywords.append(cleaned)
            
    cleaned_keywords = list(set(cleaned_keywords)) # unique keywords

    # Score each node in nodes_index based on how many keywords it matches
    node_scores = {} # node_id -> (score, node_dict)
    
    for key, nds in nodes_index.items():
        key_canon = canonical(key)
        
        matched_count = 0
        unique_matches = 0
        for kw in cleaned_keywords:
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
                
                current_score, _ = node_scores.get(node_id, (0, n))
                # Exponential boost for multi-keyword matches, plus source file preference
                new_score = current_score + (matched_count * (unique_matches ** 2)) + (has_src * 4)
                node_scores[node_id] = (new_score, n)

    # Sort nodes by score descending
    sorted_nodes = sorted(node_scores.values(), key=lambda x: x[0], reverse=True)
    
    resolved_nodes = {}
    for score, n in sorted_nodes[:20]:
        resolved_nodes[n["id"]] = n

    # Limit to top 20 resolved nodes
    selected_node_ids = list(resolved_nodes.keys())[:20]
    
    # 3. Retrieve neighbors and build markdown
    lines = []
    lines.append("### Codebase Architecture Grounding (from Code Graph Index)")
    lines.append("The following matched codebase components and method call flows were retrieved from graphify:")
    lines.append("")

    def get_node_label(nid):
        for k, nds in nodes_index.items():
            for nd in nds:
                if nd["id"] == nid:
                    return nd["label"]
        return nid

    for nid in selected_node_ids:
        node = resolved_nodes[nid]
        res = find_neighbors(node["label"])
        if not res.get("found"):
            continue
            
        lines.append(f"#### Symbol: {node['label']} (ID: {nid})")
        lines.append(f"- **Source File:** {node.get('source_file') or '(external/framework)'}")
        
        uses = res.get("uses", {})
        used_by = res.get("used_by", {})
        
        if "inherits" in uses:
            bases = [p["peer"] for p in uses["inherits"]]
            lines.append(f"- **Inherits From:** {', '.join(bases)}")
            
        if "implements" in uses:
            interfaces = [p["peer"] for p in uses["implements"]]
            lines.append(f"- **Implements:** {', '.join(interfaces)}")
            
        if "implements" in used_by:
            impls = [get_node_label(p["peer"]) for p in used_by["implements"]]
            lines.append(f"- **Implemented By:** {', '.join(impls)}")
            
        if "method" in uses:
            methods = [get_node_label(p["peer"]).replace("()", "") for p in uses["method"]]
            lines.append(f"- **Declared Methods:** {', '.join(methods)}")
            
        # Outgoing calls
        calls_out = []
        if "calls" in uses:
            for p in uses["calls"]:
                peer_label = get_node_label(p["peer"])
                conf = p.get("confidence")
                conf_str = f" (conf: {conf})" if conf is not None else ""
                calls_out.append(f"`{peer_label}`{conf_str}")
        if calls_out:
            lines.append(f"- **Calls:** {', '.join(calls_out[:10])}")
            
        # Incoming calls
        calls_in = []
        if "calls" in used_by:
            for p in used_by["calls"]:
                peer_label = get_node_label(p["peer"])
                calls_in.append(f"`{peer_label}`")
        if calls_in:
            lines.append(f"- **Called By:** {', '.join(calls_in[:10])}")
            
        lines.append("")
        
    return "\n".join(lines)


_CONTEXT_FOOTER = "=== CODEBASE & CONTRACT CONTEXT ==="


def _context_block(codebase_context: str, limit: int = 12000) -> str:
    return "\n\n".join([_CONTEXT_FOOTER, codebase_context[:limit]])


def _render_security_section(code_graph: Dict[str, Any]) -> str:
    """Deterministic §3 when requirements security fields are empty."""
    constraints = code_graph.get("constraints", [])
    phi = [c for c in constraints if c.lower().startswith("phi:")]
    secrets = [c for c in constraints if "no new secrets" in c.lower() or "do not log secret" in c.lower()]
    cyber = [c for c in constraints if "gates answered" in c.lower()]

    lines = [
        "## 3 Security Approach",
        "",
        "### 3.1 Authentication",
        "Not specified in source documentation.",
        "",
        "### 3.2 Authorization",
        "Not specified in source documentation.",
        "",
        "### 3.3 Data Protection",
    ]
    if phi:
        for p in phi:
            lines.append(f"- {p}")
    else:
        lines.append("Not specified in source documentation.")
    lines.extend([
        "",
        "### 3.4 Secrets Handling",
    ])
    if secrets:
        for s in secrets:
            lines.append(f"- {s}")
    else:
        lines.append("Not specified in source documentation.")
    lines.extend([
        "",
        "### 3.5 Compliance and Auditing",
    ])
    if cyber:
        for c in cyber:
            lines.append(f"- {c}")
    else:
        lines.append("Not specified in source documentation.")
    lines.append("")
    return "\n".join(lines)


def _render_architecture_decisions(code_graph: Dict[str, Any]) -> str:
    """Deterministic §2 subsection from contract Checkpoint B decisions."""
    decisions = code_graph.get("resolved_at_checkpoint_b", [])
    if not decisions:
        return ""
    lines = [
        "### 2.0 Architecture Decisions (Checkpoint B)",
        "",
        "The following decisions are fixed from the feature contract. Each row explains why the decision matters to this HLD and where it is grounded.",
        "",
        "| Decision | Source | Design Impact |",
        "| --- | --- | --- |",
    ]
    for d in decisions:
        decision = _markdown_table_cell(str(d))
        impact = _markdown_table_cell(_decision_impact(str(d)))
        lines.append(f"| {decision} | Feature contract / Checkpoint B | {impact} |")
    lines.append("")
    return "\n".join(lines)


def _decision_impact(decision: str) -> str:
    text = decision.lower()
    if "persist" in text or "mongo" in text or "repository" in text:
        return "Defines the persistence boundary and prevents the HLD from assuming an unrelated data store."
    if "compute" in text or "background" in text or "timer" in text:
        return "Defines when the feature behavior is evaluated and whether background processing is required."
    if "reuse" in text or "do not reuse" in text or "new" in text:
        return "Defines whether implementation should extend existing behavior or introduce a new capability."
    if "api" in text or "dto" in text or "contract" in text:
        return "Defines the public contract or integration surface affected by the feature."
    return "Constrains the logical architecture and implementation approach for this feature."


def _render_hld_evidence_summary(requirements: Dict[str, Any], code_graph: Dict[str, Any]) -> str:
    """Deterministic evidence summary so reviewers understand grounding quality."""
    mapping = code_graph.get("mapping", {})
    mapped_modules = mapping.get("mapped_modules", []) or []
    mapped_flows = mapping.get("mapped_flows", []) or []
    seed_resolutions = code_graph.get("seed_resolutions", []) or []
    resolved = [s for s in seed_resolutions if s.get("resolved")]
    new_caps = [s for s in seed_resolutions if s.get("is_new_capability") or s.get("relation") == "new"]
    acs = code_graph.get("acceptance_criteria", []) or []
    target_projects = code_graph.get("target_projects", []) or []
    req_modules = requirements.get("hld_content", {}).get("2_logical_view", {}).get("modules", []) or []

    lines = [
        "### 2.0.1 Evidence and Confidence Summary",
        "",
        "This section summarizes the source evidence used by the HLD so confirmed behavior, inferred mappings, and new capability areas are visible to reviewers.",
        "",
        "| Evidence Area | Count / Status | HLD Usage |",
        "| --- | --- | --- |",
        f"| Contract target projects | {len(target_projects)} | Defines component/project boundaries. |",
        f"| Resolved seed symbols | {len(resolved)} of {len(seed_resolutions)} | Grounds existing classes, methods, and APIs. |",
        f"| New capability seeds | {len(new_caps)} | Identifies behavior not yet represented as existing code. |",
        f"| Requirements modules | {len(req_modules)} | Provides logical feature decomposition. |",
        f"| Mapped code modules | {len(mapped_modules)} | Connects logical modules to implementation symbols. |",
        f"| Mapped flows | {len(mapped_flows)} | Grounds sequence and interaction diagrams. |",
        f"| Acceptance criteria | {len(acs)} | Grounds traceability and testable behavior. |",
        "",
    ]
    return "\n".join(lines)


def _render_open_questions_and_gaps(code_graph: Dict[str, Any]) -> str:
    unresolved = code_graph.get("unresolved", []) or []
    seed_resolutions = code_graph.get("seed_resolutions", []) or []
    unmapped = [
        s for s in seed_resolutions
        if not s.get("resolved") and not s.get("is_new_capability") and s.get("name")
    ]
    if not unresolved and not unmapped:
        return ""

    lines = [
        "### 2.0.2 Open Questions and To Be Confirmed",
        "",
        "The following items should be confirmed before detailed implementation. They are listed explicitly so the HLD does not hide assumptions in generic prose.",
        "",
        "| Item | Source | Impact |",
        "| --- | --- | --- |",
    ]
    for item in unresolved:
        text = item.get("question") if isinstance(item, dict) else str(item)
        if text:
            lines.append(
                f"| { _markdown_table_cell(text) } | Feature contract unresolved list | Confirm before final MDD or implementation. |"
            )
    for seed in unmapped:
        name = _markdown_table_cell(seed.get("name", ""))
        lines.append(
            f"| {name} | Contract seed not resolved in code graph | Treat as not mapped until code evidence is available. |"
        )
    lines.append("")
    return "\n".join(lines)


def _allowed_symbols_list(code_graph: Dict[str, Any]) -> str:
    """Compact allowlist of codebase symbols for LLM grounding."""
    symbols: List[str] = []
    for res in code_graph.get("seed_resolutions", []):
        node = res.get("node") or {}
        label = node.get("label", "")
        if label and res.get("resolved"):
            symbols.append(label)
    for mod in code_graph.get("mapping", {}).get("mapped_modules", []):
        for cb in mod.get("codebase_mappings", []):
            sym = cb.get("codebase_symbol", "")
            if sym and sym not in symbols:
                symbols.append(sym)
    if not symbols:
        return ""
    return "ALLOWED CODEBASE SYMBOLS (use ONLY these in tables and sequence diagrams):\n" + ", ".join(
        f"`{s}`" for s in symbols[:25]
    )


def _markdown_table_cell(value: str) -> str:
    """Escape text for a markdown table cell (full content, no truncation)."""
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(line.strip() for line in text.split("\n") if line.strip())
    return text.replace("|", "/")


def _resolve_ac_symbol(
    ac: Dict[str, Any],
    seed_by_bl: Dict[str, str],
    seeds: List[Dict[str, Any]],
    code_graph: Dict[str, Any],
) -> str:
    """Resolve mapped code symbol for one acceptance criterion."""
    bls = ac.get("verifies", [])
    for bl in bls:
        if bl in seed_by_bl:
            raw = seed_by_bl[bl]
            if raw.startswith("."):
                for s in seeds:
                    node = s.get("node") or {}
                    lbl = node.get("label", "")
                    if lbl == raw or raw in (s.get("name") or ""):
                        src = node.get("source_file") or s.get("sourceFile") or ""
                        cls = _class_from_source_file(src)
                        return f"`{cls}{raw}`" if cls else f"`{raw}`"
            return f"`{raw}`"
    for s in seeds:
        note = s.get("note", "")
        for bl in bls:
            if bl in note:
                node = s.get("node") or {}
                lbl = node.get("label", "")
                if lbl:
                    if lbl.startswith("."):
                        src = node.get("source_file") or s.get("sourceFile") or ""
                        cls = _class_from_source_file(src)
                        return f"`{cls}{lbl}`" if cls else f"`{lbl}`"
                    return f"`{lbl}`"
                name = s.get("name", "")
                if name and not s.get("is_new_capability"):
                    return f"`{name}`"
                if name:
                    return f"`{_markdown_table_cell(name)[:120]}`"

    new_capability = _new_capability_seed(seeds)
    if new_capability:
        return f"NEW capability (contract-defined): `{_markdown_table_cell(new_capability)[:140]}`"

    verifies_text = _markdown_table_cell(ac.get("verifiesText", ""))
    if verifies_text:
        return f"Contract-defined behavior; code symbol not pinned yet ({verifies_text[:160]})"

    return "Contract-defined behavior; code symbol not pinned yet"


def _new_capability_seed(seeds: List[Dict[str, Any]]) -> str:
    preferred: List[str] = []
    fallback: List[str] = []
    for seed in seeds:
        name = seed.get("name", "")
        if not name:
            continue
        lowered = name.lower()
        if seed.get("relation") == "new" or "capability" in lowered or "engine" in lowered or "lifecycle" in lowered:
            preferred.append(name)
        elif seed.get("is_new_capability"):
            fallback.append(name)
    if preferred:
        return preferred[0]
    return fallback[0] if fallback else ""


def _render_traceability_table(code_graph: Dict[str, Any]) -> str:
    """Deterministic traceability table: AC -> seed symbol (full AC text, no truncation)."""
    acs = code_graph.get("acceptance_criteria", [])
    seeds = code_graph.get("seed_resolutions", [])
    if not acs:
        return ""
    seed_by_bl: Dict[str, str] = {}
    for s in seeds:
        note = s.get("note", "")
        node = s.get("node") or {}
        sym = node.get("label") or s.get("name", "")
        for ac in acs:
            for bl in ac.get("verifies", []):
                if bl in note:
                    seed_by_bl[bl] = sym

    lines = [
        "### 2.z Requirements Traceability",
        "",
        "Acceptance criteria from the feature contract (`acceptanceCriteria`), mapped to codebase symbols via business-logic IDs (`verifies` / BL-* references in seed notes).",
        "",
        "| AC ID | Requirement (full) | Verifies (BL) | Mapped Code Symbol |",
        "| --- | --- | --- | --- |",
    ]
    for ac in acs:
        ac_id = _markdown_table_cell(ac.get("id", ""))
        text = _markdown_table_cell(ac.get("text", "") or "")
        bls = ac.get("verifies", [])
        bl_cell = _markdown_table_cell(", ".join(bls) if bls else "—")
        sym = _resolve_ac_symbol(ac, seed_by_bl, seeds, code_graph)
        lines.append(f"| {ac_id} | {text} | {bl_cell} | {sym} |")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Deterministic §2 API tables from code_graph.mapping (no LLM, no hardcoding)
# ----------------------------------------------------------------------
def _class_from_source_file(source_file: str) -> str:
    if not source_file:
        return ""
    name = Path(source_file.replace("\\", "/")).name
    return name[:-3] if name.endswith(".cs") else name


def _tokenize_signature(signature: str) -> List[str]:
    """Split API signatures into comparable tokens (handles camelCase)."""
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", signature or "")
    return [t for t in re.split(r"[\W_]+", expanded.lower()) if len(t) > 2]


def _method_display_name(method_graph_id: str, code_graph: Dict[str, Any]) -> str:
    """Resolve a graph method id to a readable name via seed symbol names/labels."""
    suffix = method_graph_id.split("_")[-1].lower()
    for res in code_graph.get("seed_resolutions", []):
        for token in re.findall(r"[A-Z][a-zA-Z0-9]*", res.get("name", "")):
            if token.lower() == suffix:
                return token
        lbl = (res.get("node") or {}).get("label", "")
        if lbl.startswith("."):
            bare = lbl.strip(".()")
            if bare.lower() == suffix:
                return bare
    part = method_graph_id.split("_")[-1]
    return part[0].upper() + part[1:] if part else ""


def _match_declared_method(signature: str, methods: List[str], code_graph: Dict[str, Any]) -> str:
    """Pick the best declared graph method id for a requirements API signature."""
    if not signature or not methods:
        return ""
    sig_tokens = _tokenize_signature(signature)
    best_id = ""
    best_score = 0
    for m in methods:
        short = m.split("_")[-1].lower()
        if not short:
            continue
        score = sum(1 for t in sig_tokens if t in short)
        if score > best_score:
            best_score = score
            best_id = m
    if best_score <= 0 or not best_id:
        return ""
    return _method_display_name(best_id, code_graph)


def _format_mapped_symbol(
    cb: Dict[str, Any],
    signature: str = "",
    code_graph: Optional[Dict[str, Any]] = None,
) -> str:
    """Format one codebase_mappings entry for the API table symbol column."""
    if not cb:
        return "Not mapped in code graph"
    if cb.get("is_new_capability") and not cb.get("source_file"):
        return "NEW capability (not in monolith graph)"

    sym = (cb.get("codebase_symbol") or "").strip()
    if not sym:
        return "Not mapped in code graph"

    src = cb.get("source_file") or ""
    cls = _class_from_source_file(src)
    cg = code_graph or {}

    if sym.startswith("."):
        return f"`{cls}{sym}`" if cls else f"`{sym}`"

    if "+" in sym or len(sym) > 80:
        return f"`{sym}`"

    matched = _match_declared_method(signature, cb.get("methods") or [], cg)
    if matched:
        return f"`{sym}.{matched}()`"
    return f"`{sym}`"


def _format_api_symbol_cell(
    mappings: List[Dict[str, Any]],
    signature: str,
    code_graph: Dict[str, Any],
) -> str:
    if not mappings:
        return "Not mapped in code graph"
    cells = [_format_mapped_symbol(cb, signature, code_graph) for cb in mappings[:3]]
    return " / ".join(cells)


def _infer_protocol_type(signature: str) -> str:
    sig = (signature or "").strip().upper()
    if sig.startswith(("GET ", "POST ", "PUT ", "PATCH ", "DELETE ")):
        return "HTTP/REST"
    if "GRPC" in sig or "gRPC" in (signature or ""):
        return "gRPC"
    if "/" in signature and not signature.strip().startswith(("def ", "public ", "internal ")):
        return "HTTP/REST"
    return "Internal"


def _mapping_description(cb: Optional[Dict[str, Any]]) -> str:
    if not cb:
        return "Not mapped in code graph"
    note = (cb.get("note") or "").strip()
    if note:
        return note[:120].replace("|", "/").replace("\n", " ")
    src = cb.get("source_file") or ""
    if src:
        return f"Mapped in `{src}`"
    return "Mapped in code graph"


def _render_module_api_table_block(mod: Dict[str, Any], code_graph: Dict[str, Any]) -> str:
    """Build the Interfaces & APIs markdown table for one requirements module."""
    apis = mod.get("interfaces_and_apis") or []
    lines = [
        "- Interfaces & APIs table (sourced from code_graph.json):",
        "  | Interface | Protocol/Type | Signature | Codebase Mapped Class/Method | Description |",
        "  | --- | --- | --- | --- | --- |",
    ]
    for api in apis:
        iface = (api.get("interface_name") or "—").replace("|", "/")
        sig = (api.get("signature") or "—").replace("|", "/")
        mappings = api.get("codebase_mappings") or []
        sym_cell = _format_api_symbol_cell(mappings, sig, code_graph)
        primary = mappings[0] if mappings else None
        desc = _mapping_description(primary)
        proto = _infer_protocol_type(sig)
        lines.append(f"  | {iface} | {proto} | `{sig}` | {sym_cell} | {desc} |")
    return "\n".join(lines)


def _replace_api_table_in_module_content(content: str, table_block: str) -> str:
    """Replace or insert the API table block inside one module subsection."""
    patterns = [
        re.compile(
            r"- Interfaces & APIs table[^\n]*\n(?:\s*\|.*\n)+",
            re.IGNORECASE,
        ),
        re.compile(
            r"- Interfaces & APIs table:.*?(?=\n- Dependencies on other modules:|\n###|\Z)",
            re.DOTALL | re.IGNORECASE,
        ),
    ]
    for pat in patterns:
        if pat.search(content):
            return pat.sub(table_block + "\n", content, count=1)

    dep_pat = re.compile(r"\n(- Dependencies on other modules:)", re.IGNORECASE)
    if dep_pat.search(content):
        return dep_pat.sub(f"\n{table_block}\n\\1", content, count=1)

    return content.rstrip() + "\n\n" + table_block + "\n"


def inject_mapped_api_tables(body: str, code_graph: Dict[str, Any]) -> str:
    """
    Inject/replace per-module API tables from code_graph.mapping.
    Works for any feature — reads mapped_modules only, no symbol hardcoding.
    """
    mapped_modules = code_graph.get("mapping", {}).get("mapped_modules") or []
    if not mapped_modules:
        return body

    result = body
    for mod in mapped_modules:
        mod_name = (mod.get("module_name") or "").strip()
        if not mod_name:
            continue
        table_block = _render_module_api_table_block(mod, code_graph)
        esc_name = re.escape(mod_name)
        section_pat = re.compile(
            rf"(###\s+2\.\d+\s+[^\n]*{esc_name}[^\n]*\n)"
            rf"(.*?)"
            rf"(?=\n###\s+2\.|\n##\s+|\Z)",
            re.DOTALL | re.IGNORECASE,
        )

        def _replacer(match: re.Match, *, block: str = table_block) -> str:
            header = match.group(1)
            content = match.group(2)
            return header + _replace_api_table_in_module_content(content, block)

        new_result, count = section_pat.subn(_replacer, result, count=1)
        if count:
            result = new_result
    return result


_HLD_SYSTEM = (
    "You are a principal system architect writing a High-Level Design (HLD) "
    "document following the SOP-036 standard. Ground every factual claim in the "
    "provided requirements and codebase/contract context. "
    "Do NOT include any top-level title or chat greeting — output is merged section-by-section. "
    "CRITICAL RULES:\n"
    "1. For class names, methods, and file paths, use ONLY symbols listed in the "
    "Codebase & Contract Context. Do NOT invent C# names. "
    "If unmapped, write exactly: Not mapped in code graph.\n"
    "2. If a requirements field is empty, write exactly one sentence: "
    "'Not specified in source documentation.' "
    "Do NOT add examples, typical values, assumptions, or illustrative infrastructure "
    "(no Redis, Kafka, OIDC, AWS, Kubernetes unless explicitly in context).\n"
    "3. In sequenceDiagram blocks, participant IDs MUST be mapped codebase symbols "
    "(e.g. MealController, DiabetesElogWorkflow). Never use logical names like "
    "FoodModule or CGMConnectionService.\n"
    "4. For infrastructure, use ONLY target_projects and persistence decisions from "
    "contract context (e.g. Welldoc.Web.Member.API, JSONRepository/Mongo).\n"
    "5. Mermaid: flowchart TD must NOT use 'participant'. "
    "sequenceDiagram IDs must be single words (use aliases for display names)."
)



def _hld_intro_prompt(plan: Dict[str, Any], requirements: Dict[str, Any], codebase_context: str) -> str:
    project = _document_topic(requirements, plan)
    section_reqs = requirements.get("hld_content", {}).get("1_introduction", {})
    return "\n\n".join([
        f"Generate the Introduction section (Section 1) for the HLD of project '{project}'.",
        "Following SOP-036, generate the following subsections exactly, with rich and exhaustive descriptions:",
        "### 1.1 Purpose and Scope",
        "Explain the business and technical problem statement in a highly detailed multi-paragraph narrative.",
        "List granular, explicit system objectives.",
        "Provide a comprehensive, exhaustive list of in-scope features/modules.",
        "Provide a comprehensive list of out-of-scope boundaries/limitations.",
        "List success criteria and KPIs.",
        "Use out-of-scope items from contract context for boundaries when present in codebase context.",
        "",
        "### 1.2 Definitions and Acronyms",
        "Produce a markdown table of terms and definitions. Scan the requirements for terms, abbreviation expansions, and descriptions. Format: | Term | Expansion | Definition |",
        "",
        "### 1.3 References",
        "List all referenced documents, specifications, or PRDs with their relationship to this design.",
        "",
        "### 1.4 Context",
        "Describe the enterprise context, upstream triggers, downstream consumers, and input/output flows.",
        "Embed a top-level system context diagram using a ```mermaid flowchart TD``` block. "
        "Follow the flowchart TD syntax strictly (do NOT use 'participant' or 'as' keywords).",
        "",
        "=== SPECIFIC REQUIREMENTS ===",
        json.dumps(section_reqs, indent=2, ensure_ascii=False),
        "",
        _context_block(codebase_context),
        "",
        "Output ONLY the markdown text starting directly with '## 1 Introduction'. Do not include any chat preamble."
    ])


def _hld_logical_prompt(plan: Dict[str, Any], requirements: Dict[str, Any], codebase_context: str, code_graph: Dict[str, Any]) -> str:
    project = plan.get("project_name") or requirements.get("project_name") or "System"
    section_reqs = requirements.get("hld_content", {}).get("2_logical_view", {})
    return "\n\n".join([
        f"Generate the Logical View section (Section 2) for the HLD of project '{project}'.",
        "Following SOP-036, generate a detailed logical architecture view.",
        "Include an overview paragraph of the modular design.",
        "Then, generate a dedicated subsection for each high-level module/service identified in the requirements and codebase mappings.",
        "Number module subsections sequentially starting at 2.5 (diagram subsections 2.1-2.4 are reserved).",
        "### 2.5 [Module Name] Logical View",
        "- Architectural Layer and Role.",
        "- Detailed Responsibilities (3-5 sentences).",
        "- Capabilities list.",
        "- Do NOT write an Interfaces & APIs table — it is injected automatically from code_graph.json after generation.",
        "- Dependencies on other modules.",
        "",
        "### 2.N Interactions and Flows",
        "Document step-by-step transaction sequence flows. For each flow, provide steps and a ```mermaid sequenceDiagram```.",
        "CRITICAL: sequenceDiagram participant IDs MUST be exact mapped codebase symbols (e.g. MealController, FoodController, DiabetesElogWorkflow). "
        "Use participant aliases for display: participant MealController as \"Meal Controller\".",
        "Do NOT use logical module names like FoodModule or CGMConnectionService.",
        "",
        _allowed_symbols_list(code_graph) or "",
        "",
        "=== SPECIFIC REQUIREMENTS ===",
        json.dumps(section_reqs, indent=2, ensure_ascii=False),
        "",
        _context_block(codebase_context),
        "",
        "Output ONLY the markdown text starting directly with '## 2 Logical View'. Do not include any chat preamble."
    ])


def _hld_security_prompt(plan: Dict[str, Any], requirements: Dict[str, Any], codebase_context: str) -> str:
    project = plan.get("project_name") or requirements.get("project_name") or "System"
    section_reqs = requirements.get("hld_content", {}).get("3_security_approach", {})
    return "\n\n".join([
        f"Generate Section 3 (Security Approach) of the architectural design for project '{project}'.",
        "Document security controls ONLY as specified in requirements or contract constraints (especially PHI).",
        "Cover: authentication, authorization, data protection, secrets, compliance/auditing.",
        "If a subsection has no source data, write exactly: 'Not specified in source documentation.' "
        "Do NOT add generic OIDC/AWS examples or security diagrams unless requirements specify them.",
        "",
        "=== SPECIFIC REQUIREMENTS ===",
        json.dumps(section_reqs, indent=2, ensure_ascii=False),
        "",
        _context_block(codebase_context),
        "",
        "Output ONLY the markdown text starting directly with '## 3 Security Approach'. Do not include any chat preamble."
    ])


def _hld_scalability_prompt(plan: Dict[str, Any], requirements: Dict[str, Any], codebase_context: str) -> str:
    project = plan.get("project_name") or requirements.get("project_name") or "System"
    section_reqs = requirements.get("hld_content", {}).get("4_scalability_view", {})
    return "\n\n".join([
        f"Generate the Scalability View section (Section 4) for the HLD of project '{project}'.",
        "Document scalability ONLY from requirements. Cover performance targets, bottlenecks, data retention.",
        "If requirements fields are empty, state 'Not specified in source documentation.' for each subsection "
        "and do NOT invent RPS, latency, Redis, Kafka, or caching examples.",
        "Omit infrastructure diagrams in this section.",
        "",
        "=== SPECIFIC REQUIREMENTS ===",
        json.dumps(section_reqs, indent=2, ensure_ascii=False),
        "",
        _context_block(codebase_context),
        "",
        "Output ONLY the markdown text starting directly with '## 4 Scalability View'. Do not include any chat preamble."
    ])


def _hld_infra_prompt(plan: Dict[str, Any], requirements: Dict[str, Any], codebase_context: str) -> str:
    project = plan.get("project_name") or requirements.get("project_name") or "System"
    section_reqs = requirements.get("hld_content", {}).get("5_infrastructure_view", {})
    return "\n\n".join([
        f"Generate the Infrastructure View section (Section 5) for the HLD of project '{project}'.",
        "Cover deployment target, topology components, networking, resilience/DR, and topology diagram.",
        "Use ONLY components from contract target_projects and architecture decisions in context "
        "(e.g. Welldoc.Web.Member.API, Welldoc.Web.Service_DotNetCore, Welldoc.Server.Infra.JSONRepository/Mongo, "
        "Welldoc.Integration.Libre_DotNetCore).",
        "Do NOT mention Redis, Kafka, or other pipeline/vector-store tooling.",
        "If deployment platform is unknown, write 'Not specified in source documentation.' without assuming cloud vendor.",
        "",
        "### 5.1 Deployment Target",
        "### 5.2 Topology Components (table: Component | Type | Technology | HA)",
        "### 5.3 Networking and Connectivity",
        "### 5.4 Resilience and Disaster Recovery",
        "### 5.5 Infrastructure Topology Diagram (```mermaid flowchart TD``` only — no participant keyword)",
        "",
        "=== SPECIFIC REQUIREMENTS ===",
        json.dumps(section_reqs, indent=2, ensure_ascii=False),
        "",
        _context_block(codebase_context),
        "",
        "Output ONLY the markdown text starting directly with '## 5 Infrastructure View'. Do not include any chat preamble."
    ])


# ----------------------------------------------------------------------
# JSON coercion (reused from requirements_generator pattern)
# ----------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


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


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------
def generate_hld(
    *,
    product: Optional[str] = None,
    release: Optional[str] = None,
    requirements_path: Optional[str] = None,
    code_graph_path: Optional[str] = None,
    artifact_dir: Optional[str] = None,
    planner_max_tokens: int = 1500,
    hld_max_tokens: int = 8192,
    temperature: float = 0.2,
    progress_callback: Optional[ProgressCallback] = None,
) -> HLDResult:
    """Generate the HLD Markdown document.

    Both artifact paths default to the `*_latest.json` pointers written
    by the upstream services.
    """
    def progress(progress_pct: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress_pct, message)

    progress(10, "Loading requirements and code graph artifacts...")
    context = artifact_context(product=product, release=release, create=True)
    out_dir = artifact_dir or str(context.stage_dir("hld"))
    os.makedirs(out_dir, exist_ok=True)
    hld_run_dir = Path(out_dir) / context.timestamp
    hld_run_dir.mkdir(parents=True, exist_ok=True)

    codebase_dir = context.stage_dir("codebase")
    default_requirements_path = str(_latest_requirements(out_dir) or Path(out_dir) / "requirements.json")
    default_code_graph_path = str(_latest_code_graph(codebase_dir) or Path(codebase_dir) / "code_graph.json")
    req_payload = _load_json(requirements_path or default_requirements_path)
    code_payload = _load_json(code_graph_path or default_code_graph_path)

    requirements = req_payload.get("requirements", req_payload)
    code_graph = code_payload.get("code_graph", code_payload)

    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.utcnow().isoformat()

    llm = get_llm_client()

    # ---- Pass 0: planner ------------------------------------------------
    plan_raw = _run_with_progress_pulse(
        progress,
        20,
        "Planning HLD sections...",
        lambda: llm.chat(
            system_prompt=_PLANNER_SYSTEM,
            user_prompt=_planner_user_prompt(requirements, code_graph),
            temperature=0.1,
            max_tokens=planner_max_tokens,
        ),
    )
    try:
        plan = _coerce_json(plan_raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[HLD Pipeline] Planner returned invalid JSON; using deterministic fallback plan: {exc}")
        plan = {
            "project_name": requirements.get("project_name") or context.product,
            "include_sections": {
                "introduction": True,
                "definitions_and_acronyms": True,
                "references": True,
                "context": True,
                "logical_view": True,
                "security": False,
                "scalability": False,
                "infrastructure": False,
            },
            "modules": [
                {"name": project, "responsibility": f"Target project for {_document_topic(requirements, {})}"}
                for project in code_graph.get("target_projects", [])[:8]
            ],
            "diagrams_required": {
                "top_level_architecture": True,
                "combined_modules": False,
                "infrastructure_topology": False,
            },
            "reasoning": "Deterministic fallback plan because planner LLM returned invalid JSON.",
        }

    plan = _normalize_plan(plan, requirements, code_graph)

    # ---- Pass 1: HLD markdown (Generated section-by-section) -----------
    progress(30, "HLD plan ready. Generating document sections...")
    codebase_context = _get_codebase_context(requirements, code_graph)
    
    sections_markdown = []
    
    # 1. Introduction
    if plan.get("include_sections", {}).get("introduction", True):
        print("[HLD Pipeline] Generating Section 1: Introduction...")
        intro_raw = _run_with_progress_pulse(
            progress,
            35,
            "Generating Section 1: Introduction...",
            lambda: llm.chat(
                system_prompt=_HLD_SYSTEM,
                user_prompt=_hld_intro_prompt(plan, requirements, codebase_context),
                temperature=temperature,
                max_tokens=hld_max_tokens,
            ),
        )
        sections_markdown.append(intro_raw)
        
    # 2. Logical View
    if plan.get("include_sections", {}).get("logical_view", True):
        print("[HLD Pipeline] Generating Section 2: Logical View...")
        logical_raw = _run_with_progress_pulse(
            progress,
            45,
            "Generating Section 2: Logical View...",
            lambda: llm.chat(
                system_prompt=_HLD_SYSTEM,
                user_prompt=_hld_logical_prompt(plan, requirements, codebase_context, code_graph),
                temperature=temperature,
                max_tokens=hld_max_tokens,
            ),
        )
        # Prepend deterministic contract subsections
        arch_decisions = _render_architecture_decisions(code_graph)
        evidence_summary = _render_hld_evidence_summary(requirements, code_graph)
        open_questions = _render_open_questions_and_gaps(code_graph)
        traceability = _render_traceability_table(code_graph)
        logical_parts = ["## 2 Logical View"]
        if arch_decisions:
            logical_parts.append(arch_decisions)
        if evidence_summary:
            logical_parts.append(evidence_summary)
        if open_questions:
            logical_parts.append(open_questions)
        # Strip duplicate heading if LLM included it
        body = logical_raw.strip()
        if body.startswith("## 2"):
            body = re.sub(r"^##\s+2[^\n]*\n+", "", body, count=1)
        body = inject_mapped_api_tables(body, code_graph)
        logical_parts.append(body)
        if traceability:
            logical_parts.append(traceability)
        sections_markdown.append("\n\n".join(logical_parts))
        
    # 3. Security Approach (NFR — requirements.json only)
    if plan.get("include_sections", {}).get("security"):
        print("[HLD Pipeline] Generating Section 3: Security Approach...")
        security_raw = _run_with_progress_pulse(
            progress,
            55,
            "Generating Section 3: Security Approach...",
            lambda: llm.chat(
                system_prompt=_HLD_SYSTEM,
                user_prompt=_hld_security_prompt(plan, requirements, codebase_context),
                temperature=temperature,
                max_tokens=hld_max_tokens,
            ),
        )
        sections_markdown.append(security_raw)
    else:
        progress(52, "Skipping Section 3: no security content in requirements.")
        print("[HLD Pipeline] Skipping Section 3: no security content in requirements.json")

    # 4. Scalability View (NFR — requirements.json only)
    if plan.get("include_sections", {}).get("scalability"):
        print("[HLD Pipeline] Generating Section 4: Scalability View...")
        scalability_raw = _run_with_progress_pulse(
            progress,
            60,
            "Generating Section 4: Scalability View...",
            lambda: llm.chat(
                system_prompt=_HLD_SYSTEM,
                user_prompt=_hld_scalability_prompt(plan, requirements, codebase_context),
                temperature=temperature,
                max_tokens=hld_max_tokens,
            ),
        )
        sections_markdown.append(scalability_raw)
    else:
        progress(57, "Skipping Section 4: no scalability content in requirements.")
        print("[HLD Pipeline] Skipping Section 4: no scalability content in requirements.json")

    # 5. Infrastructure View (NFR — requirements.json only)
    if plan.get("include_sections", {}).get("infrastructure"):
        print("[HLD Pipeline] Generating Section 5: Infrastructure View...")
        infra_raw = _run_with_progress_pulse(
            progress,
            65,
            "Generating Section 5: Infrastructure View...",
            lambda: llm.chat(
                system_prompt=_HLD_SYSTEM,
                user_prompt=_hld_infra_prompt(plan, requirements, codebase_context),
                temperature=temperature,
                max_tokens=hld_max_tokens,
            ),
        )
        sections_markdown.append(infra_raw)
    else:
        progress(62, "Skipping Section 5: no infrastructure content in requirements.")
        print("[HLD Pipeline] Skipping Section 5: no infrastructure content in requirements.json")
        
    # Assemble cover and Revision History & TOC
    progress(70, "Assembling HLD document...")
    project = _document_topic(requirements, plan)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    cover = [
        f"# {project} — High-Level Design",
        "",
        "## Revision History",
        "",
        "| Date | Revision No. | Author | Comments |",
        "|---|---|---|---|",
        f"| {date_str} | 1.0 | HLD Generation Pipeline | Initial generated HLD document. |",
        "",
        "## Table of Contents",
        "",
    ]
    
    if plan.get("include_sections", {}).get("introduction", True):
        cover.append("- [1. Introduction](#1-introduction)")
        cover.append("  - [1.1 Purpose and Scope](#11-purpose-and-scope)")
        cover.append("  - [1.2 Definitions and Acronyms](#12-definitions-and-acronyms)")
        cover.append("  - [1.3 References](#13-references)")
        cover.append("  - [1.4 Context](#14-context)")
    if plan.get("include_sections", {}).get("logical_view", True):
        cover.append("- [2. Logical View](#2-logical-view)")
    if plan.get("include_sections", {}).get("security"):
        cover.append("- [3. Security Approach](#3-security-approach)")
    if plan.get("include_sections", {}).get("scalability"):
        cover.append("- [4. Scalability View](#4-scalability-view)")
    if plan.get("include_sections", {}).get("infrastructure"):
        cover.append("- [5. Infrastructure View](#5-infrastructure-view)")
        
    cover.append("")
    cover.append("---")
    cover.append("")
    
    hld_raw = "\n".join(cover) + "\n\n" + "\n\n".join(sections_markdown)

    # ---- Pass 2: sanitize + validate diagrams --------------------------
    progress(75, "Processing Mermaid diagrams...")
    hld_clean = postprocess_mermaid(hld_raw)
    hld_clean, hld_diagram_meta = _insert_deterministic_hld_diagrams(
        hld_clean,
        requirements,
        code_graph,
        plan,
    )
    hld_clean = postprocess_mermaid(hld_clean)

    progress(82, "Validating diagrams and HLD accuracy...")
    diagram_report = validate_diagrams(hld_clean)
    diagram_report.update(hld_diagram_meta)
    accuracy_report = validate_hld(hld_clean, code_graph, requirements=requirements)

    # ---- Pass 3: persist -----------------------------------------------
    progress(90, "Exporting HLD DOCX...")
    completed_at = datetime.utcnow().isoformat()
    docx_path = str(hld_run_dir / f"HLD_{context.timestamp}.docx")
    markdown_to_docx(hld_clean, docx_path)
    if not os.path.isfile(docx_path):
        raise RuntimeError(f"DOCX export did not create expected file: {docx_path}")

    manifest = {
        "job_id": job_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "llm": llm.info(),
        "product": context.product,
        "release": context.release,
        "timestamp": context.timestamp,
        "requirements_source": requirements_path or default_requirements_path,
        "code_graph_source": code_graph_path or default_code_graph_path,
        "plan": plan,
        "hld_markdown": hld_clean,
        "docx_path": docx_path,
        "diagram_report": diagram_report,
        "accuracy_report": accuracy_report,
        "nfr_sections": plan.get("nfr_sections", {}),
    }
    progress(95, "Saving HLD manifest...")
    manifest_path = str(hld_run_dir / f"HLD_{context.timestamp}.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    progress(99, "HLD generation complete.")
    return HLDResult(
        job_id=job_id,
        started_at=started_at,
        completed_at=completed_at,
        plan=plan,
        hld_markdown=hld_clean,
        diagram_report=diagram_report,
        artifact_paths={
            "docx": docx_path,
            "hld_json": manifest_path,
        },
    )


def _latest_code_graph(codebase_dir: str | Path) -> Path | None:
    root = Path(codebase_dir)
    matches = [path for path in root.glob("code_graph_*.json") if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _latest_requirements(hld_dir: str | Path) -> Path | None:
    root = Path(hld_dir)
    matches = [path for path in root.glob("requirements_*.json") if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)
