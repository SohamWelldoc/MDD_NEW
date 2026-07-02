"""LLM-backed draft revision generation for review feedback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from services.review.diff_builder import build_diff
from services.review.document_loader import load_hld_context, load_mdd_context
from services.review.feedback_classifier import classify_feedback
from services.review.change_planner import build_change_plan, build_evidence_summary
from services.review.review_store import (
    append_version,
    create_version_files,
    find_feedback,
    next_draft_version,
    read_version_markdown,
    save_review,
    utc_now,
)
from services.review.section_detector import detect_feedback_section
from services.review.validation import sanitize_and_validate_revision
from services.shared.llm_client import get_llm_client


_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

_REVISION_SYSTEM = (
    "You are a principal software architecture reviewer revising generated HLD/MDD markdown from human feedback. "
    "Apply the feedback as a real document-review change, not as a shallow paraphrase. Return markdown only. "
    "Do not include chat preamble. Preserve the document's SOP-style structure, heading levels, traceability, "
    "tables, and Mermaid fences unless the feedback requires changing them. Ground every technical claim in "
    "the supplied requirements/code/MDD context or explicitly label it as reviewer-provided input. Do not invent "
    "unsupported systems, APIs, classes, databases, security controls, scalability numbers, or infrastructure."
)

_DIAGRAM_REVISION_SYSTEM = (
    "You are a principal architecture reviewer specializing in Mermaid diagrams inside HLD/MDD documents. "
    "The reviewer asked for diagram improvement. Return markdown only, with valid Mermaid fences. "
    "Do not make a cosmetic/no-op rewrite. Improve the diagram's explanatory value while staying grounded "
    "in the supplied requirements, code graph, and current document text. Prefer concrete labels, explicit "
    "states/steps/edges, and clearer grouping over vague nodes. Do not invent new systems, APIs, classes, "
    "or facts that are not supported by the context."
)

_PROMPT_TEMPLATES = {
    "correction": "Correct inaccurate content and remove conflicting old claims in the target scope.",
    "addition": "Add only the missing reviewer-requested detail where it belongs, with source grounding.",
    "diagram": "Materially improve Mermaid structure, labels, and edges while keeping valid syntax.",
    "formatting": "Improve readability/formatting without changing technical meaning.",
    "security": "Add security detail only from evidence or reviewer-provided feedback; mark gaps as To be confirmed.",
    "scalability": "Add scalability/performance detail only from evidence or reviewer-provided feedback; avoid invented metrics.",
    "infrastructure": "Add infrastructure detail only from evidence or reviewer-provided feedback; avoid invented services.",
    "source_gap": "Call out unsupported or missing evidence explicitly instead of making authoritative claims.",
    "full_rewrite": "Rewrite the target section comprehensively while preserving SOP structure and traceability.",
    "preserve_existing_and_add_diagram": "Keep existing Mermaid blocks unchanged and append a new Mermaid block.",
    "add_new_diagram": "Add a new Mermaid block while preserving existing diagrams unless replacement was requested.",
    "modify_existing_diagram": "Modify the existing Mermaid block only; do not add another diagram unless necessary.",
    "replace_diagram": "Replace the relevant Mermaid block and avoid contradictory old diagram content.",
}


def _diagram_review_strategy(classification: Dict[str, Any]) -> Dict[str, Any]:
    if "diagram_change" not in classification.get("tags", []):
        return {}
    exact_intent = classification.get("exact_intent")
    if exact_intent == "preserve_existing_and_add_diagram":
        return {
            "goal": "Preserve the existing Mermaid diagram and add a second Mermaid diagram.",
            "requirements": [
                "Keep every existing Mermaid block exactly as provided.",
                "Append one new Mermaid block after the existing diagram in the same target section.",
                "The new diagram must add value and must not be a copy of the old diagram.",
                "Do not rewrite, reorder, or remove existing Mermaid blocks.",
                "Use only source-grounded labels and relationships.",
            ],
            "quality_bar": [
                "The Mermaid block count must increase by at least one.",
                "The old Mermaid text must still appear unchanged in the revised section.",
            ],
        }
    if exact_intent == "add_new_diagram":
        return {
            "goal": "Add a new Mermaid diagram without treating the request as a generic diagram improvement.",
            "requirements": [
                "Add one new Mermaid block in the target section.",
                "Preserve existing Mermaid blocks unless the reviewer explicitly asked to replace them.",
                "The new diagram must explain the reviewer-requested concept using source-grounded labels.",
            ],
            "quality_bar": [
                "The Mermaid block count should increase.",
                "Existing diagrams should remain intact unless replacement was requested.",
            ],
        }
    return {
        "goal": "Produce a materially clearer Mermaid diagram, not a prose-only or cosmetic edit.",
        "requirements": [
            "Keep the same section heading and surrounding explanation.",
            "Revise Mermaid blocks in the target section; add a Mermaid block only if the section has none.",
            "Use flowchart for lifecycle/architecture/decision diagrams and sequenceDiagram for interactions.",
            "Use specific labels from requirements/code evidence: components, APIs, states, timers, decisions, and data outcomes.",
            "Add meaningful edge labels when they explain trigger, condition, payload, timing, or outcome.",
            "Avoid generic node names such as System, Service, Component A, Component B, Process, or Step unless they already exist as source terms.",
            "Keep Mermaid syntax simple: no styling/classDef/click events; quote labels with spaces or punctuation.",
            "Ensure flowcharts have at least 4 useful nodes and 3 useful edges when evidence supports it.",
        ],
        "quality_bar": [
            "The new diagram should make the reviewer understand order, responsibility, and outcome better than before.",
            "If feedback is vague like 'make diagram better', infer improvement from the target section and source evidence.",
            "If evidence is insufficient, make the existing diagram clearer using only known labels and state what remains to be confirmed in prose.",
        ],
    }


def _general_review_strategy(classification: Dict[str, Any]) -> Dict[str, Any]:
    tags = set(classification.get("tags", []))
    strategy = {
        "goal": "Satisfy the reviewer feedback with a clear, reviewable document revision.",
        "universal_rules": [
            "Make a meaningful change that directly addresses the feedback.",
            "Preserve correct existing content that is unrelated to the feedback.",
            "Keep the same markdown/SOP structure unless the feedback asks for structure changes.",
            "Use precise engineering language and avoid vague filler.",
            "Do not remove traceability, evidence, acceptance criteria, or diagrams unless requested.",
            "If the feedback is vague, infer the most likely reviewer intent from target_section and context.",
            "If requested information is missing from source evidence, add a short 'To be confirmed' or reviewer-provided note rather than hallucinating.",
        ],
        "change_plan": [
            "Identify what part of the current section/document fails the feedback.",
            "Revise the smallest sufficient scope: targeted paragraph/table/diagram first, full section if needed.",
            "Add or update details using only supplied context and existing document terminology.",
            "Ensure the final markdown reads as a polished HLD/MDD section, not as a response to a chat message.",
        ],
    }
    if "content_correction" in tags:
        strategy["content_correction_rules"] = [
            "Replace the incorrect statement wherever it appears in the target scope.",
            "Keep dependent wording consistent after the correction.",
            "Do not preserve both old and corrected claims if they conflict.",
        ]
    if "content_addition" in tags:
        strategy["content_addition_rules"] = [
            "Add the missing detail in the most relevant subsection/table/list.",
            "Connect the added detail to existing modules, APIs, flows, decisions, or acceptance criteria when evidence supports it.",
            "Avoid generic best-practice content unless explicitly requested by the reviewer.",
        ]
    if any(tag in tags for tag in ("security_content", "scalability_content", "infrastructure_content")):
        strategy["nfr_rules"] = [
            "Only add NFR details that exist in source context or reviewer feedback.",
            "If NFR evidence is absent, create a concise 'Not specified / To be confirmed' entry instead of inventing controls or metrics.",
        ]
    if "source_gap" in tags:
        strategy["evidence_rules"] = [
            "Call out source/evidence gaps explicitly.",
            "Do not convert missing-source feedback into authoritative architecture claims.",
        ]
    return strategy


def _selected_prompt_templates(classification: Dict[str, Any]) -> Dict[str, str]:
    tags = set(classification.get("tags", []))
    selected: Dict[str, str] = {}
    exact_intent = classification.get("exact_intent")
    if exact_intent in _PROMPT_TEMPLATES:
        selected[exact_intent] = _PROMPT_TEMPLATES[exact_intent]
    if "content_correction" in tags:
        selected["correction"] = _PROMPT_TEMPLATES["correction"]
    if "content_addition" in tags:
        selected["addition"] = _PROMPT_TEMPLATES["addition"]
    if "diagram_change" in tags:
        selected["diagram"] = _PROMPT_TEMPLATES["diagram"]
    if "formatting_change" in tags:
        selected["formatting"] = _PROMPT_TEMPLATES["formatting"]
    if "security_content" in tags:
        selected["security"] = _PROMPT_TEMPLATES["security"]
    if "scalability_content" in tags:
        selected["scalability"] = _PROMPT_TEMPLATES["scalability"]
    if "infrastructure_content" in tags:
        selected["infrastructure"] = _PROMPT_TEMPLATES["infrastructure"]
    if "source_gap" in tags:
        selected["source_gap"] = _PROMPT_TEMPLATES["source_gap"]
    if classification.get("target_kind") == "full_document":
        selected["full_rewrite"] = _PROMPT_TEMPLATES["full_rewrite"]
    return selected or {"general": "Apply the feedback with the smallest sufficient document change."}


def _needs_clarification(feedback: Dict[str, Any], classification: Dict[str, Any]) -> bool:
    text = (feedback.get("feedback") or "").strip().lower()
    vague_terms = {"fix it", "improve", "make better", "update this", "change this"}
    return text in vague_terms and not feedback.get("target_section") and classification.get("priority") == "medium"


def _self_review(
    *,
    old_markdown: str,
    new_markdown: str,
    feedback: Dict[str, Any],
    validation_report: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "feedback_addressed": old_markdown.strip() != new_markdown.strip(),
        "blocking_issues": validation_report.get("blocking", []),
        "warnings": validation_report.get("warnings", []),
        "quality_score": validation_report.get("quality_score", 0),
        "reviewer_expectation": feedback.get("reviewer_expectation"),
    }


def _strip_markdown_response(raw: str) -> str:
    text = (raw or "").strip()
    fenced = re.match(r"^```(?:markdown|md)?\s*\n(.*?)```$", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text


def _section_bounds(markdown: str, target_section: Optional[str]) -> Optional[Tuple[int, int]]:
    if not target_section:
        return None
    escaped = re.escape(target_section.strip())
    patterns = [
        re.compile(rf"^(?P<hashes>#+)\s+.*{escaped}.*$", re.IGNORECASE | re.MULTILINE),
        re.compile(rf"^(?P<hashes>#+)\s+{escaped}\s*$", re.IGNORECASE | re.MULTILINE),
    ]
    match = None
    for pattern in patterns:
        match = pattern.search(markdown)
        if match:
            break
    if not match:
        return None
    level = len(match.group("hashes"))
    next_heading = re.compile(rf"^#{{1,{level}}}\s+", re.MULTILINE)
    next_match = next_heading.search(markdown, match.end())
    end = next_match.start() if next_match else len(markdown)
    return match.start(), end


def _replace_section(markdown: str, target_section: Optional[str], revised_section: str) -> str:
    bounds = _section_bounds(markdown, target_section)
    if not bounds:
        return revised_section
    start, end = bounds
    return markdown[:start].rstrip() + "\n\n" + revised_section.strip() + "\n\n" + markdown[end:].lstrip()


def _mermaid_summary(markdown: str) -> Dict[str, Any]:
    blocks = _MERMAID_BLOCK_RE.findall(markdown or "")
    summaries = []
    for index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        diagram_type = lines[0].split()[0] if lines else "unknown"
        summaries.append({
            "index": index,
            "type": diagram_type,
            "line_count": len(lines),
            "preview": "\n".join(lines[:12]),
        })
    return {"count": len(blocks), "diagrams": summaries}


def _text_change_ratio(old: str, new: str) -> float:
    old_norm = re.sub(r"\s+", " ", old or "").strip()
    new_norm = re.sub(r"\s+", " ", new or "").strip()
    if not old_norm and not new_norm:
        return 0.0
    if old_norm == new_norm:
        return 0.0
    old_set = set(old_norm.split())
    new_set = set(new_norm.split())
    if not old_set and not new_set:
        return 1.0
    overlap = len(old_set & new_set)
    total = max(len(old_set | new_set), 1)
    return 1 - (overlap / total)


def _is_weak_diagram_revision(old_section: str, new_section: str, classification: Dict[str, Any]) -> bool:
    if "diagram_change" not in classification.get("tags", []):
        return False
    old_blocks = _MERMAID_BLOCK_RE.findall(old_section or "")
    new_blocks = _MERMAID_BLOCK_RE.findall(new_section or "")
    exact_intent = classification.get("exact_intent")
    if exact_intent == "preserve_existing_and_add_diagram":
        old_preserved = all(block.strip() in [new_block.strip() for new_block in new_blocks] for block in old_blocks)
        return not old_preserved or len(new_blocks) <= len(old_blocks)
    if exact_intent == "add_new_diagram":
        return len(new_blocks) <= len(old_blocks)
    if not new_blocks:
        return True
    if old_blocks and old_blocks == new_blocks:
        return True
    return _text_change_ratio("\n".join(old_blocks), "\n".join(new_blocks)) < 0.08


def _is_weak_general_revision(old_section: str, new_section: str, classification: Dict[str, Any]) -> bool:
    if "diagram_change" in classification.get("tags", []):
        return False
    old_norm = re.sub(r"\s+", " ", old_section or "").strip()
    new_norm = re.sub(r"\s+", " ", new_section or "").strip()
    if not new_norm or old_norm == new_norm:
        return True
    return _text_change_ratio(old_section, new_section) < 0.03


def _context_for_prompt(review: Dict[str, Any]) -> Dict[str, Any]:
    if review.get("document_type") == "hld":
        try:
            context = load_hld_context(review.get("product"), review.get("release"))
        except FileNotFoundError:
            return {"requirements": {}, "code_graph_summary": {}, "context_status": "limited"}
        return {
            "requirements": context.get("requirements", {}),
            "code_graph_summary": {
                "target_projects": (context.get("code_graph") or {}).get("target_projects", []),
                "stats": (context.get("code_graph") or {}).get("stats", {}),
                "acceptance_criteria": (context.get("code_graph") or {}).get("acceptance_criteria", [])[:20],
            },
        }
    try:
        context = load_mdd_context(review)
    except FileNotFoundError:
        return {"mdd_plan": {}, "hld_summary": {}, "context_status": "limited"}
    return {
        "mdd_plan": context.get("mdd_plan", {}),
        "hld_summary": {
            "plan": (context.get("hld") or {}).get("plan", {}),
            "nfr_sections": (context.get("hld") or {}).get("nfr_sections", {}),
        },
    }


def create_draft_revision(
    review: Dict[str, Any],
    *,
    feedback_id: str,
    requested_by: Optional[str],
    progress_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    def progress(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message)

    progress(10, "Loading current review version...")
    feedback = find_feedback(review, feedback_id)
    current_version = review.get("current_version", "v1")
    if feedback.get("base_version") != current_version:
        feedback["status"] = "conflict"
        feedback["updated_at"] = utc_now()
        save_review(review)
        raise RuntimeError(
            f"Feedback {feedback_id} targets {feedback.get('base_version')} but current version is {current_version}"
        )

    old_markdown = read_version_markdown(review, current_version)
    if not feedback.get("target_section"):
        detection = detect_feedback_section(
            markdown=old_markdown,
            feedback=feedback.get("feedback", ""),
            change_type=feedback.get("change_type"),
            target_kind=feedback.get("target_kind"),
        )
        if detection.get("target_section"):
            feedback["target_section"] = detection["target_section"]
            feedback["section_detection"] = detection
            feedback["target_kind"] = feedback.get("target_kind") or "section"
            feedback["updated_at"] = utc_now()
            save_review(review)
    progress(20, "Classifying feedback and building change strategy...")
    classification = classify_feedback(
        feedback=feedback["feedback"],
        target_section=feedback.get("target_section"),
        document_type=review["document_type"],
        change_type=feedback.get("change_type"),
        priority=feedback.get("priority"),
        target_kind=feedback.get("target_kind"),
        reviewer_expectation=feedback.get("reviewer_expectation"),
    )
    if _needs_clarification(feedback, classification):
        feedback["status"] = "clarification_needed"
        feedback["clarification_reason"] = "Feedback is too broad to revise safely without a target section or expected outcome."
        feedback["updated_at"] = utc_now()
        save_review(review)
        raise RuntimeError("Clarification needed before revision: please provide the target section and expected change.")
    change_plan = build_change_plan(review, feedback_id, requested_by)
    evidence_summary = build_evidence_summary(review)
    extracted_entities = change_plan.get("extracted_entities", [])
    classification["extracted_entities"] = extracted_entities
    section_bounds = _section_bounds(old_markdown, feedback.get("target_section"))
    section_markdown = old_markdown[section_bounds[0]:section_bounds[1]] if section_bounds else old_markdown
    diagram_strategy = _diagram_review_strategy(classification)
    general_strategy = _general_review_strategy(classification)
    prompt = {
        "document_type": review["document_type"],
        "product": review.get("product"),
        "release": review.get("release"),
        "module_slug": review.get("module_slug"),
        "current_version": current_version,
        "target_section": feedback.get("target_section"),
        "classification": classification,
        "exact_intent_contract": {
            "intent": classification.get("exact_intent"),
            "constraints": classification.get("intent_constraints", []),
            "extracted_entities": extracted_entities,
            "planned_action": change_plan.get("planned_action"),
            "must_follow_exactly": True,
        },
        "change_plan": change_plan,
        "reviewer_feedback": feedback["feedback"],
        "context": _context_for_prompt(review),
        "review_strategy": general_strategy,
        "prompt_templates": _selected_prompt_templates(classification),
        "rewrite_mode": "full" if feedback.get("target_kind") == "full_document" and not section_bounds else "minimal",
        "diagram_review_strategy": diagram_strategy,
        "existing_mermaid_summary": _mermaid_summary(section_markdown),
        "extracted_entities": extracted_entities,
        "markdown_to_revise": section_markdown,
        "instruction": (
            "Revise only the provided section and return that section as markdown."
            if section_bounds
            else "Revise the full document and return the full markdown document."
        ),
    }
    llm = get_llm_client()
    progress(45, "Calling LLM to create revised draft...")
    raw = llm.chat(
        _DIAGRAM_REVISION_SYSTEM if diagram_strategy else _REVISION_SYSTEM,
        json.dumps(prompt, indent=2, ensure_ascii=False)[:50000],
        temperature=0.25 if diagram_strategy else 0.2,
        max_tokens=7000 if diagram_strategy else 6000,
    )
    revised_piece = _strip_markdown_response(raw)
    if _is_weak_diagram_revision(section_markdown, revised_piece, classification):
        progress(60, "Initial diagram revision was weak; retrying with stricter instructions...")
        retry_prompt = {
            **prompt,
            "previous_revision_was_too_weak": True,
            "previous_revision": revised_piece,
            "retry_instruction": (
                "The previous answer did not materially improve the Mermaid diagram. "
                "Rewrite the target section again while following exact_intent_contract. "
                "If the exact intent is to add a diagram, the Mermaid block count must increase. "
                "If the exact intent says to preserve existing diagrams, do not modify old Mermaid blocks."
            ),
        }
        raw = llm.chat(
            _DIAGRAM_REVISION_SYSTEM,
            json.dumps(retry_prompt, indent=2, ensure_ascii=False)[:55000],
            temperature=0.35,
            max_tokens=7500,
        )
        revised_piece = _strip_markdown_response(raw)
    elif _is_weak_general_revision(section_markdown, revised_piece, classification):
        progress(60, "Initial revision was too small; retrying with stronger review instructions...")
        retry_prompt = {
            **prompt,
            "previous_revision_was_too_weak": True,
            "previous_revision": revised_piece,
            "retry_instruction": (
                "The previous answer did not materially satisfy the reviewer feedback. "
                "Revise again with a clearer, concrete change. Update the relevant prose/table/list/section "
                "using the review_strategy, while preserving unrelated content and source grounding."
            ),
        }
        raw = llm.chat(
            _REVISION_SYSTEM,
            json.dumps(retry_prompt, indent=2, ensure_ascii=False)[:55000],
            temperature=0.3,
            max_tokens=7000,
        )
        revised_piece = _strip_markdown_response(raw)
    revised_markdown = _replace_section(old_markdown, feedback.get("target_section"), revised_piece) if section_bounds else revised_piece
    progress(75, "Validating revised markdown and Mermaid diagrams...")
    clean_markdown, validation_report = sanitize_and_validate_revision(
        markdown=revised_markdown,
        document_type=review["document_type"],
        product=review.get("product"),
        release=review.get("release"),
        old_markdown=old_markdown,
        classification=classification,
    )
    if validation_report.get("blocking"):
        progress(82, "Validation found blocking issues; retrying revision once...")
        retry_prompt = {
            **prompt,
            "previous_revision": clean_markdown,
            "validation_failures": validation_report.get("blocking", []),
            "retry_instruction": "Fix the blocking validation failures while preserving the reviewer-requested change.",
        }
        raw = llm.chat(
            _DIAGRAM_REVISION_SYSTEM if diagram_strategy else _REVISION_SYSTEM,
            json.dumps(retry_prompt, indent=2, ensure_ascii=False)[:55000],
            temperature=0.2,
            max_tokens=7500,
        )
        revised_piece = _strip_markdown_response(raw)
        revised_markdown = _replace_section(old_markdown, feedback.get("target_section"), revised_piece) if section_bounds else revised_piece
        clean_markdown, validation_report = sanitize_and_validate_revision(
            markdown=revised_markdown,
            document_type=review["document_type"],
            product=review.get("product"),
            release=review.get("release"),
            old_markdown=old_markdown,
            classification=classification,
        )
    draft_version = next_draft_version(review)
    review_dir = Path(review["review_dir"])
    progress(88, "Saving draft revision and building diff...")
    diff = build_diff(old_markdown, clean_markdown, target_section=feedback.get("target_section"))
    self_review = _self_review(
        old_markdown=old_markdown,
        new_markdown=clean_markdown,
        feedback=feedback,
        validation_report=validation_report,
    )
    version_entry = create_version_files(
        review_dir=review_dir,
        document_type=review["document_type"],
        module_slug=review.get("module_slug"),
        version=draft_version,
        status="draft",
        markdown=clean_markdown,
        metadata={
            "base_version": current_version,
            "feedback_id": feedback_id,
            "classification": classification,
            "change_plan": change_plan,
            "evidence_summary": evidence_summary,
            "validation_report": validation_report,
            "quality_score": validation_report.get("quality_score"),
            "self_review": self_review,
            "prompt_templates": _selected_prompt_templates(classification),
            "diff": diff,
            "created_by": requested_by or "default_user",
        },
    )
    append_version(review, version_entry)
    feedback["status"] = "drafted"
    feedback["draft_version"] = draft_version
    feedback["classification"] = classification
    feedback["updated_at"] = utc_now()
    review["status"] = "revision_proposed"
    review.setdefault("audit", []).append({
        "event": "draft_revision_created",
        "feedback_id": feedback_id,
        "draft_version": draft_version,
        "base_version": current_version,
        "actor": requested_by or "default_user",
        "at": utc_now(),
    })
    save_review(review)
    progress(100, "Draft revision completed.")
    return {
        "review": review,
        "feedback": feedback,
        "draft_version": draft_version,
        "classification": classification,
        "change_plan": change_plan,
        "evidence_summary": evidence_summary,
        "validation_report": validation_report,
        "quality_score": validation_report.get("quality_score"),
        "self_review": self_review,
        "diff": diff,
    }
