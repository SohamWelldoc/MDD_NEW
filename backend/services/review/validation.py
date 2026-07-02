"""Revision validation and DOCX export for review versions."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, Optional

from services.hld.hld_validator import validate_hld
from services.review.document_loader import load_hld_context
from services.shared.docx_exporter import markdown_to_docx
from services.shared.mermaid_utils import postprocess_mermaid, validate_diagrams


def sanitize_and_validate_revision(
    *,
    markdown: str,
    document_type: str,
    product: Optional[str],
    release: Optional[str],
    old_markdown: Optional[str] = None,
    classification: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    clean = postprocess_mermaid(markdown)
    diagram_report = validate_diagrams(clean)
    report: Dict[str, Any] = {
        "diagram_report": diagram_report,
        "issues": [],
        "warnings": [],
        "blocking": [],
        "quality_score": 100,
        "checklist": {},
    }
    old_markdown = old_markdown or ""
    classification = classification or {}
    tags = set(classification.get("tags", []))
    _validate_required_headings(old_markdown, clean, report)
    _validate_traceability_retained(old_markdown, clean, report)
    if "diagram_change" in tags:
        _validate_diagram_changed(old_markdown, clean, report)
    _validate_exact_intent(old_markdown, clean, classification, report)
    if "source_gap" in tags and "to be confirmed" not in clean.lower():
        report["warnings"].append({
            "type": "source_gap_not_tagged",
            "message": "Feedback indicates a source/evidence gap, but revision does not mention 'To be confirmed'.",
        })
    if document_type == "hld":
        try:
            context = load_hld_context(product, release)
            code_graph = context.get("code_graph") or {}
            requirements = context.get("requirements") or {}
            accuracy = validate_hld(clean, code_graph, requirements=requirements)
            report["accuracy_report"] = accuracy
            report["issues"].extend(accuracy.get("issues", []))
            report["warnings"].extend(accuracy.get("warnings", []))
            _validate_code_symbols(clean, code_graph, report)
        except FileNotFoundError:
            report["warnings"].append({
                "type": "limited_validation_context",
                "message": "Generated HLD context was not found; source/code validation was limited.",
            })
    _validate_mermaid_report(diagram_report, report)
    _validate_nfr_evidence(clean, classification, report)
    _finalize_report(report)
    return clean, report


def _headings(markdown: str) -> set[str]:
    return {m.strip().lower() for m in re.findall(r"^#{1,6}\s+(.+)$", markdown or "", flags=re.MULTILINE)}


def _validate_required_headings(old_markdown: str, new_markdown: str, report: Dict[str, Any]) -> None:
    old = _headings(old_markdown)
    new = _headings(new_markdown)
    important = {h for h in old if any(token in h for token in ("introduction", "logical view", "traceability", "evidence"))}
    missing = sorted(important - new)
    for heading in missing:
        report["issues"].append({
            "type": "required_heading_removed",
            "message": f"Important heading removed: {heading}",
        })


def _validate_traceability_retained(old_markdown: str, new_markdown: str, report: Dict[str, Any]) -> None:
    if "traceability" in old_markdown.lower() and "traceability" not in new_markdown.lower():
        report["issues"].append({
            "type": "traceability_removed",
            "message": "Revision removed traceability content.",
        })
    if "acceptance criteria" in old_markdown.lower() and "acceptance criteria" not in new_markdown.lower():
        report["warnings"].append({
            "type": "acceptance_criteria_removed",
            "message": "Revision may have removed acceptance criteria content.",
        })


def _validate_diagram_changed(old_markdown: str, new_markdown: str, report: Dict[str, Any]) -> None:
    old_blocks = re.findall(r"```mermaid\s*\n(.*?)```", old_markdown or "", flags=re.DOTALL)
    new_blocks = re.findall(r"```mermaid\s*\n(.*?)```", new_markdown or "", flags=re.DOTALL)
    if old_blocks == new_blocks:
        report["issues"].append({
            "type": "diagram_not_changed",
            "message": "Feedback requested a diagram change, but Mermaid blocks did not change.",
        })


def _validate_exact_intent(
    old_markdown: str,
    new_markdown: str,
    classification: Dict[str, Any],
    report: Dict[str, Any],
) -> None:
    exact_intent = classification.get("exact_intent")
    if exact_intent not in {"preserve_existing_and_add_diagram", "add_new_diagram"}:
        return
    old_blocks = re.findall(r"```mermaid\s*\n(.*?)```", old_markdown or "", flags=re.DOTALL)
    new_blocks = re.findall(r"```mermaid\s*\n(.*?)```", new_markdown or "", flags=re.DOTALL)
    old_normalized = {_normalize_mermaid_block(block) for block in old_blocks}
    new_normalized = {_normalize_mermaid_block(block) for block in new_blocks}
    if len(new_blocks) <= len(old_blocks):
        report["issues"].append({
            "type": "diagram_addition_not_performed",
            "message": "Exact intent required adding a new Mermaid diagram, but the diagram count did not increase.",
            "severity": "blocking",
        })
    if exact_intent == "add_new_diagram":
        _validate_old_text_preserved(old_markdown, new_markdown, report)
    _validate_entities_in_added_diagram(old_blocks, new_blocks, classification, report)
    _validate_diagram_type_for_intent(old_markdown, new_blocks, classification, report)
    if exact_intent == "preserve_existing_and_add_diagram" and not old_normalized.issubset(new_normalized):
        report["issues"].append({
            "type": "existing_diagram_not_preserved",
            "message": "Exact intent required preserving existing Mermaid diagrams unchanged.",
            "severity": "blocking",
        })


def _normalize_mermaid_block(block: str) -> str:
    return "\n".join(line.rstrip() for line in (block or "").strip().splitlines())


def _validate_old_text_preserved(old_markdown: str, new_markdown: str, report: Dict[str, Any]) -> None:
    old_without_mermaid = re.sub(r"```mermaid\s*\n.*?```", "", old_markdown or "", flags=re.DOTALL)
    important_lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in old_without_mermaid.splitlines()
        if len(re.sub(r"\s+", " ", line).strip()) > 30
    ][:12]
    new_normalized = re.sub(r"\s+", " ", new_markdown or "")
    missing = [line for line in important_lines if line not in new_normalized]
    if missing:
        report["issues"].append({
            "type": "old_section_text_not_preserved",
            "message": "Exact intent required adding a diagram while preserving existing section prose.",
            "missing_lines": missing[:5],
            "severity": "blocking",
        })


def _validate_entities_in_added_diagram(
    old_blocks: list[str],
    new_blocks: list[str],
    classification: Dict[str, Any],
    report: Dict[str, Any],
) -> None:
    entities = [entity for entity in classification.get("extracted_entities", []) if len(str(entity)) >= 3]
    if not entities or classification.get("exact_intent") not in {"add_new_diagram", "preserve_existing_and_add_diagram"}:
        return
    added_blocks = new_blocks[len(old_blocks):] if len(new_blocks) > len(old_blocks) else new_blocks
    added_text = " ".join(added_blocks).lower()
    matched = [entity for entity in entities if _entity_in_text(entity, added_text)]
    required = min(2, len(entities))
    if len(matched) < required:
        report["issues"].append({
            "type": "diagram_missing_section_entities",
            "message": "Added diagram does not include enough extracted section entities.",
            "expected_entities": entities[:8],
            "matched_entities": matched,
            "severity": "blocking",
        })


def _entity_in_text(entity: str, text: str) -> bool:
    normalized_entity = re.sub(r"[^a-z0-9]+", "", str(entity).lower())
    normalized_text = re.sub(r"[^a-z0-9]+", "", text)
    return normalized_entity in normalized_text


def _validate_diagram_type_for_intent(
    old_markdown: str,
    new_blocks: list[str],
    classification: Dict[str, Any],
    report: Dict[str, Any],
) -> None:
    if classification.get("exact_intent") not in {"add_new_diagram", "preserve_existing_and_add_diagram"}:
        return
    flow_markers = ("step", "steps", "interaction", "interactions", "sends", "retrieves", "displays", "triggered")
    if not any(marker in (old_markdown or "").lower() for marker in flow_markers):
        return
    added_block = new_blocks[-1] if new_blocks else ""
    if added_block and not added_block.strip().lower().startswith("sequencediagram"):
        report["warnings"].append({
            "type": "diagram_type_may_not_match_flow",
            "message": "Section looks like an interaction/step flow; sequenceDiagram is preferred for add-diagram feedback.",
        })


def _validate_mermaid_report(diagram_report: Dict[str, Any], report: Dict[str, Any]) -> None:
    if not isinstance(diagram_report, dict):
        report["checklist"]["mermaid_syntax"] = True
        return
    errors = diagram_report.get("errors") or diagram_report.get("issues") or []
    report["checklist"]["mermaid_syntax"] = not bool(errors)
    for error in errors:
        report["issues"].append({
            "type": "mermaid_validation_failed",
            "message": str(error),
        })


def _validate_code_symbols(markdown: str, code_graph: Dict[str, Any], report: Dict[str, Any]) -> None:
    symbols = set()
    for key in ("classes", "functions", "methods", "apis", "endpoints"):
        value = code_graph.get(key) or []
        for item in value:
            if isinstance(item, dict):
                symbols.add(str(item.get("name") or item.get("path") or item.get("endpoint") or ""))
            else:
                symbols.add(str(item))
    mentioned = re.findall(r"`([A-Za-z_][\w.:/-]{2,})`", markdown or "")
    unknown = [symbol for symbol in mentioned if symbols and symbol not in symbols and "/" not in symbol[:1]]
    report["checklist"]["code_symbols_checked"] = True
    if unknown[:10]:
        report["warnings"].append({
            "type": "unverified_code_symbols",
            "message": "Some referenced code symbols were not found in code graph.",
            "symbols": unknown[:10],
        })


def _validate_nfr_evidence(markdown: str, classification: Dict[str, Any], report: Dict[str, Any]) -> None:
    tags = set(classification.get("tags", []))
    needs_evidence = any(tag in tags for tag in ("security_content", "scalability_content", "infrastructure_content"))
    if not needs_evidence:
        return
    lower = markdown.lower()
    has_evidence = any(token in lower for token in ("source:", "evidence", "requirement", "reviewer-provided", "to be confirmed"))
    report["checklist"]["nfr_evidence"] = has_evidence
    if not has_evidence:
        report["warnings"].append({
            "type": "nfr_without_explicit_evidence",
            "message": "NFR-related feedback was applied without an explicit evidence/source marker.",
        })


def _finalize_report(report: Dict[str, Any]) -> None:
    blocking_types = {
        "required_heading_removed",
        "traceability_removed",
        "diagram_not_changed",
        "mermaid_validation_failed",
        "diagram_addition_not_performed",
        "existing_diagram_not_preserved",
        "old_section_text_not_preserved",
        "diagram_missing_section_entities",
    }
    report["blocking"] = [
        issue for issue in report.get("issues", []) if issue.get("type") in blocking_types or issue.get("severity") == "blocking"
    ]
    warning_count = len(report.get("warnings", []))
    issue_count = len(report.get("issues", []))
    report["quality_score"] = max(0, 100 - issue_count * 15 - warning_count * 5)
    report["checklist"].setdefault("required_sections_retained", not any(i.get("type") == "required_heading_removed" for i in report["issues"]))
    report["checklist"].setdefault("traceability_retained", not any(i.get("type") == "traceability_removed" for i in report["issues"]))


def export_revision_docx(
    *,
    markdown: str,
    output_path: str,
    document_type: str,
    title: Optional[str] = None,
) -> str:
    path = Path(output_path)
    resolved_title = title
    if not resolved_title:
        resolved_title = "High-Level Design" if document_type == "hld" else "Module Detail Design"
    markdown_to_docx(markdown, str(path), document_title=resolved_title)
    if not path.is_file():
        raise RuntimeError(f"DOCX export did not create expected file: {path}")
    return str(path)
