"""Change planning and evidence summaries for review revisions."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List

from services.review.document_loader import load_hld_context, load_mdd_context
from services.review.entity_extractor import extract_section_entities
from services.review.feedback_classifier import classify_feedback
from services.review.review_store import find_feedback, read_version_markdown
from services.review.section_detector import extract_sections


def build_evidence_summary(review: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize source artifacts used to ground a review revision."""
    current = next((v for v in review.get("versions", []) if v.get("version") == review.get("current_version", "v1")), {})
    if review.get("document_type") == "hld":
        try:
            context = load_hld_context(review.get("product"), review.get("release"))
        except FileNotFoundError:
            source_metadata = current.get("source_metadata") or {}
            return {
                "document_type": "hld",
                "hld_source": current.get("source_path"),
                "requirements_source": source_metadata.get("requirements_source"),
                "code_graph_source": source_metadata.get("code_graph_source"),
                "source_artifact": Path(current.get("source_path", "")).name if current.get("source_path") else None,
                "evidence_status": "limited",
                "source_gap_report": ["Generated HLD context was not found; using stored source metadata only."],
                "citations": _citations_from_metadata(source_metadata),
            }
        hld = context.get("hld", {})
        return {
            "document_type": "hld",
            "hld_source": context.get("hld_path"),
            "requirements_source": hld.get("requirements_source"),
            "code_graph_source": hld.get("code_graph_source"),
            "source_artifact": Path(context.get("hld_path", "")).name if context.get("hld_path") else None,
            "evidence_status": "available" if context.get("requirements") or context.get("code_graph") else "limited",
            "source_gap_report": _source_gap_report(context.get("requirements"), context.get("code_graph")),
            "citations": _citations_from_metadata(hld),
        }
    try:
        context = load_mdd_context(review)
    except FileNotFoundError:
        return {
            "document_type": "mdd",
            "module_slug": review.get("module_slug"),
            "mdd_plan_source": current.get("source_path"),
            "hld_source": None,
            "requirements_source": None,
            "code_graph_source": None,
            "evidence_status": "limited",
            "source_gap_report": ["Generated MDD/HLD context was not found; using stored source metadata only."],
            "citations": _citations_from_metadata(current.get("source_metadata") or {}),
        }
    hld = context.get("hld", {})
    return {
        "document_type": "mdd",
        "module_slug": review.get("module_slug"),
        "mdd_plan_source": context.get("source_path"),
        "hld_source": context.get("hld_path"),
        "requirements_source": hld.get("requirements_source"),
        "code_graph_source": hld.get("code_graph_source"),
        "evidence_status": "available" if context.get("mdd_plan") or context.get("hld") else "limited",
        "source_gap_report": _source_gap_report(context.get("mdd_plan"), context.get("hld")),
        "citations": _citations_from_metadata(hld),
    }


def _citations_from_metadata(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations = []
    for key in ("requirements_source", "code_graph_source", "hld_source", "mdd_plan_source", "source_path"):
        value = metadata.get(key)
        if value:
            citations.append({"kind": key, "path": value})
    return citations


def _source_gap_report(*sources: Any) -> List[str]:
    gaps = []
    for index, source in enumerate(sources, start=1):
        if not source:
            gaps.append(f"Source context {index} is missing or empty.")
    return gaps


def detect_unsupported_claims(feedback_text: str, evidence: Dict[str, Any]) -> List[str]:
    risky_terms = re.findall(r"\b(?:encrypt|oauth|redis|kafka|postgres|cache|latency|sla|autoscale|iam|jwt)\b", feedback_text or "", flags=re.I)
    if risky_terms and evidence.get("evidence_status") != "available":
        return sorted(set(term.lower() for term in risky_terms))
    return []


def build_change_plan(review: Dict[str, Any], feedback_id: str, requested_by: str | None = None) -> Dict[str, Any]:
    feedback = find_feedback(review, feedback_id)
    classification = classify_feedback(
        feedback=feedback.get("feedback", ""),
        target_section=feedback.get("target_section"),
        document_type=review.get("document_type", "hld"),
        change_type=feedback.get("change_type"),
        priority=feedback.get("priority"),
        target_kind=feedback.get("target_kind"),
        reviewer_expectation=feedback.get("reviewer_expectation"),
    )
    target_scope = classification.get("scope")
    evidence = build_evidence_summary(review)
    selected_section_text = _selected_section_text(review, feedback.get("target_section"))
    entities = extract_section_entities(
        selected_section_text,
        code_graph=_code_graph_for_entities(review),
    )
    unsupported_claims = detect_unsupported_claims(feedback.get("feedback", ""), evidence)
    changes = []
    tags = set(classification.get("tags", []))
    if "content_correction" in tags:
        changes.append("Correct conflicting or inaccurate statements in the target scope.")
    if "content_addition" in tags:
        changes.append("Add missing reviewer-requested details in the most relevant section/table/list.")
    if "diagram_change" in tags:
        changes.append("Revise Mermaid diagram content and labels in the target scope.")
    if "source_gap" in tags:
        changes.append("Mark unsupported or missing evidence as To be confirmed rather than authoritative.")
    if "formatting_change" in tags:
        changes.append("Improve formatting while preserving technical meaning.")
    if not changes:
        changes.append("Revise the target content to directly satisfy the reviewer feedback.")
    preserved = [
        "Existing unrelated HLD/MDD structure and headings",
        "Traceability and acceptance-criteria content unless directly targeted",
        "Source-grounded architecture facts",
        "Generated artifact history and prior review versions",
    ]
    plan = {
        "feedback_id": feedback_id,
        "requested_by": requested_by or "default_user",
        "classification": classification,
        "exact_intent": classification.get("exact_intent"),
        "intent_constraints": classification.get("intent_constraints", []),
        "target_section": feedback.get("target_section"),
        "section_detection": feedback.get("section_detection"),
        "router_source": (feedback.get("section_detection") or {}).get("router_source") or ("clicked_or_manual" if feedback.get("target_section") else "none"),
        "section_confidence": (feedback.get("section_detection") or {}).get("confidence"),
        "target_scope": target_scope,
        "extracted_entities": entities,
        "planned_action": _planned_action(classification, entities),
        "intended_changes": changes,
        "preserve": preserved,
        "evidence_summary": evidence,
        "requires_evidence_review": classification.get("requires_evidence_review", False),
        "unsupported_claims": unsupported_claims,
        "traceability": {
            "feedback_id": feedback_id,
            "base_version": feedback.get("base_version"),
            "current_version": review.get("current_version", "v1"),
        },
        "ready_for_revision": feedback.get("status") in {"open", "drafted"},
    }
    feedback["change_plan"] = plan
    return plan


def _selected_section_text(review: Dict[str, Any], target_section: str | None) -> str:
    try:
        markdown = read_version_markdown(review, review.get("current_version", "v1"))
    except Exception:
        return ""
    if not target_section:
        return markdown
    for section in extract_sections(markdown):
        if section.get("heading") == target_section:
            return section.get("text", "")
    return markdown


def _code_graph_for_entities(review: Dict[str, Any]) -> Dict[str, Any]:
    if review.get("document_type") != "hld":
        return {}
    try:
        return load_hld_context(review.get("product"), review.get("release")).get("code_graph") or {}
    except Exception:
        return {}


def _planned_action(classification: Dict[str, Any], entities: List[str]) -> str:
    exact_intent = classification.get("exact_intent")
    if exact_intent == "add_new_diagram":
        return "Add a new Mermaid diagram using extracted section entities while preserving existing text."
    if exact_intent == "preserve_existing_and_add_diagram":
        return "Keep existing Mermaid diagrams unchanged and add one new Mermaid diagram."
    if exact_intent == "modify_existing_diagram":
        return "Modify the existing Mermaid diagram to satisfy the reviewer feedback."
    if exact_intent == "add_content":
        return "Add requested content in the selected section."
    if exact_intent == "correct_content":
        return "Correct inaccurate content in the selected section."
    return "Apply only the reviewer-requested change."
