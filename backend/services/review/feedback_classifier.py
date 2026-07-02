"""Small feedback classifier for review revision routing."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def classify_feedback(
    *,
    feedback: str,
    target_section: Optional[str],
    document_type: str,
    change_type: Optional[str] = None,
    priority: Optional[str] = None,
    target_kind: Optional[str] = None,
    reviewer_expectation: Optional[str] = None,
) -> Dict[str, Any]:
    text = f"{feedback or ''} {reviewer_expectation or ''}".lower()
    target = (target_section or "").lower()
    tags = []
    diagram_words = (
        "diagram", "mermaid", "flowchart", "sequence", "visual", "lifecycle",
        "architecture flow", "make it better", "improve", "improvise", "clearer",
        "more clear", "better diagram",
    )
    if any(word in text for word in diagram_words) or any(word in target for word in ("diagram", "lifecycle", "flow")):
        tags.append("diagram_change")
    if any(word in text for word in ("security", "auth", "authorization", "authentication", "encrypt")):
        tags.append("security_content")
    if any(word in text for word in ("scalability", "performance", "latency", "throughput", "load")):
        tags.append("scalability_content")
    if any(word in text for word in ("infra", "infrastructure", "deployment", "server", "cluster")):
        tags.append("infrastructure_content")
    if any(word in text for word in ("wrong", "incorrect", "change", "replace", "should be")):
        tags.append("content_correction")
    if any(word in text for word in ("add", "include", "missing", "more detail", "elaborate")):
        tags.append("content_addition")
    if any(word in text for word in ("source", "confluence", "requirement", "prd", "evidence")):
        tags.append("source_gap")
    if document_type == "hld" and any(word in text for word in ("mdd", "module detail", "module design")):
        tags.append("mdd_cascade")

    explicit_map = {
        "correction": "content_correction",
        "addition": "content_addition",
        "diagram": "diagram_change",
        "missing_evidence": "source_gap",
        "formatting": "formatting_change",
        "full_rewrite": "full_rewrite",
    }
    explicit_tag = explicit_map.get(change_type or "")
    if explicit_tag and explicit_tag not in tags:
        tags.insert(0, explicit_tag)

    if target_kind == "full_document" or change_type == "full_rewrite":
        scope = "document"
    elif target_kind:
        scope = target_kind
    else:
        scope = "section" if target_section else "document"
    if "diagram_change" in tags and not target_section:
        scope = "document"
    if any(tag in tags for tag in ("security_content", "scalability_content", "infrastructure_content")):
        scope = "document" if not target_section else "section"

    classification = tags[0] if tags else "content_correction"
    exact_intent = _detect_exact_intent(
        text=text,
        change_type=change_type,
        target_kind=target_kind,
        tags=tags or [classification],
    )
    return {
        "classification": classification,
        "exact_intent": exact_intent,
        "intent_constraints": _intent_constraints(exact_intent),
        "tags": tags or [classification],
        "scope": scope,
        "target_section": target_section,
        "change_type": change_type,
        "priority": priority or "medium",
        "target_kind": target_kind or scope,
        "reviewer_expectation": reviewer_expectation,
        "document_type": document_type,
        "requires_evidence_review": "source_gap" in tags,
        "may_affect_mdd": document_type == "hld" and ("mdd_cascade" in tags or scope == "document"),
    }


def _detect_exact_intent(
    *,
    text: str,
    change_type: Optional[str],
    target_kind: Optional[str],
    tags: List[str],
) -> str:
    is_diagram = "diagram_change" in tags or change_type == "diagram" or target_kind == "diagram"
    wants_add = any(word in text for word in ("add", "another", "new", "additional", "include one more"))
    wants_step_diagram = is_diagram and any(phrase in text for phrase in ("these steps", "the steps", "steps", "flow", "interactions"))
    wants_preserve = any(
        phrase in text
        for phrase in (
            "keep old", "keep the old", "keep existing", "keep the existing",
            "preserve existing", "preserve the existing", "do not change existing",
            "don't change existing", "same diagram", "old diagram",
        )
    )
    wants_replace = any(word in text for word in ("replace", "instead of", "remove old", "remove the old"))
    wants_modify = any(word in text for word in ("improve", "improvise", "clearer", "modify", "update", "change"))

    if is_diagram and wants_preserve and wants_add:
        return "preserve_existing_and_add_diagram"
    if is_diagram and wants_replace:
        return "replace_diagram"
    if is_diagram and (wants_add or wants_step_diagram):
        return "add_new_diagram"
    if is_diagram and wants_modify:
        return "modify_existing_diagram"
    if "formatting_change" in tags:
        return "format_only"
    if "source_gap" in tags:
        return "mark_source_gap"
    if "content_addition" in tags:
        return "add_content"
    if "content_correction" in tags:
        return "correct_content"
    if "full_rewrite" in tags:
        return "rewrite_scope"
    return "revise_content"


def _intent_constraints(exact_intent: str) -> List[str]:
    constraints = {
        "preserve_existing_and_add_diagram": [
            "Keep every existing Mermaid block unchanged.",
            "Add at least one new Mermaid block after the existing diagram in the target section.",
            "Do not replace or rewrite the existing diagram.",
        ],
        "add_new_diagram": [
            "Add at least one new Mermaid block.",
            "Preserve existing diagrams unless the reviewer explicitly asked to replace them.",
        ],
        "modify_existing_diagram": [
            "Modify the existing Mermaid block to satisfy the feedback.",
            "Do not add a separate diagram unless needed for the requested change.",
        ],
        "replace_diagram": [
            "Replace the relevant Mermaid block.",
            "Do not keep contradictory old diagram content.",
        ],
        "format_only": [
            "Only change formatting/readability.",
            "Do not change technical meaning.",
        ],
        "mark_source_gap": [
            "Mark unsupported information as To be confirmed.",
            "Do not invent authoritative claims.",
        ],
        "add_content": [
            "Add the requested content in the target scope.",
            "Preserve unrelated existing content.",
        ],
        "correct_content": [
            "Correct the inaccurate content.",
            "Remove or update conflicting old statements.",
        ],
    }
    return constraints.get(exact_intent, ["Apply only the reviewer-requested change."])
