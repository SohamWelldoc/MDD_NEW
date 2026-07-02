"""Approve or reject review draft versions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from services.review.review_store import (
    append_version,
    create_version_files,
    find_feedback,
    find_version,
    read_version_markdown,
    save_review,
    timestamp_now,
    utc_now,
    version_file_stem,
)
from services.review.cascade import mark_affected_mdd_stale
from services.review.validation import export_revision_docx


def _accepted_from_draft(draft_version: str) -> str:
    match = re.match(r"^(v\d+)_draft$", draft_version or "")
    if not match:
        raise ValueError("draft_version must look like v2_draft")
    return match.group(1)


def approve_draft(
    review: Dict[str, Any],
    *,
    draft_version: str,
    feedback_id: Optional[str],
    decided_by: Optional[str],
    reason: Optional[str] = None,
    role: Optional[str] = None,
    source_ip: Optional[str] = None,
) -> Dict[str, Any]:
    draft_entry = find_version(review, draft_version)
    if not draft_entry or draft_entry.get("status") != "draft":
        raise FileNotFoundError(f"Draft version not found: {draft_version}")
    accepted_version = _accepted_from_draft(draft_version)
    if find_version(review, accepted_version):
        raise RuntimeError(f"Accepted version already exists: {accepted_version}")
    validation = draft_entry.get("validation_report") or {}
    blocking = validation.get("blocking") or validation.get("issues") or []
    if blocking:
        raise RuntimeError(f"Draft has blocking validation issues: {blocking}")

    markdown = read_version_markdown(review, draft_version)
    review_dir = Path(review["review_dir"])
    artifact_timestamp = timestamp_now()
    stem = version_file_stem(
        review["document_type"],
        review.get("module_slug"),
        artifact_timestamp=artifact_timestamp,
    )
    docx_path = review_dir / "versions" / f"{stem}.docx"
    export_revision_docx(
        markdown=markdown,
        output_path=str(docx_path),
        document_type=review["document_type"],
        title="High-Level Design" if review["document_type"] == "hld" else f"{review.get('module_slug')} Module Detail Design",
    )
    accepted_entry = create_version_files(
        review_dir=review_dir,
        document_type=review["document_type"],
        module_slug=review.get("module_slug"),
        version=accepted_version,
        status="approved_revision",
        markdown=markdown,
        metadata={
            "base_version": draft_entry.get("base_version"),
            "feedback_id": feedback_id or draft_entry.get("feedback_id"),
            "approved_from": draft_version,
            "approved_by": decided_by or "default_user",
            "approval_reason": reason,
            "artifact_timestamp": artifact_timestamp,
        },
        docx_path=str(docx_path),
    )
    append_version(review, accepted_entry)
    draft_entry["status"] = "approved_draft"
    review["current_version"] = accepted_version
    review["status"] = "approved"
    review["stale"] = False

    resolved_feedback_id = feedback_id or draft_entry.get("feedback_id")
    if resolved_feedback_id:
        feedback = find_feedback(review, resolved_feedback_id)
        feedback["status"] = "applied"
        feedback["applied_in_version"] = accepted_version
        feedback["updated_at"] = utc_now()

    stale_reviews = []
    affected_modules = []
    if review["document_type"] == "hld":
        cascade_result = mark_affected_mdd_stale(
            review["product"],
            review["release"],
            review,
            accepted_version,
            decided_by or "default_user",
        )
        stale_reviews = cascade_result.get("review_ids", [])
        affected_modules = cascade_result.get("modules", [])
    review.setdefault("audit", []).append({
        "event": "draft_approved",
        "draft_version": draft_version,
        "version": accepted_version,
        "feedback_id": resolved_feedback_id,
        "actor": decided_by or "default_user",
        "role": role or "reviewer",
        "source_ip": source_ip,
        "reason": reason,
        "validation_warnings": validation.get("warnings", []),
        "marked_stale_reviews": stale_reviews,
        "affected_modules": affected_modules,
        "at": utc_now(),
    })
    save_review(review)
    return {
        "review": review,
        "version": accepted_version,
        "draft_version": draft_version,
    }


def reject_draft(
    review: Dict[str, Any],
    *,
    draft_version: str,
    feedback_id: Optional[str],
    decided_by: Optional[str],
    reason: Optional[str] = None,
    role: Optional[str] = None,
    source_ip: Optional[str] = None,
) -> Dict[str, Any]:
    draft_entry = find_version(review, draft_version)
    if not draft_entry:
        raise FileNotFoundError(f"Draft version not found: {draft_version}")
    draft_entry["status"] = "rejected_draft"
    resolved_feedback_id = feedback_id or draft_entry.get("feedback_id")
    if resolved_feedback_id:
        feedback = find_feedback(review, resolved_feedback_id)
        feedback["status"] = "rejected"
        feedback["rejected_draft"] = draft_version
        feedback["rejection_reason"] = reason
        feedback["updated_at"] = utc_now()
    review["status"] = "in_review"
    review.setdefault("audit", []).append({
        "event": "draft_rejected",
        "draft_version": draft_version,
        "feedback_id": resolved_feedback_id,
        "actor": decided_by or "default_user",
        "role": role or "reviewer",
        "source_ip": source_ip,
        "reason": reason,
        "at": utc_now(),
    })
    save_review(review)
    return {
        "review": review,
        "draft_version": draft_version,
    }
