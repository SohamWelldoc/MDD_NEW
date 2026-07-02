"""Version compare, restore, finalize, and audit helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from services.review.diff_builder import build_diff
from services.review.review_store import (
    append_version,
    create_version_files,
    find_version,
    next_accepted_version,
    read_version_markdown,
    save_review,
    utc_now,
)


def compare_versions(review: Dict[str, Any], from_version: str, to_version: str) -> Dict[str, Any]:
    old_markdown = read_version_markdown(review, from_version)
    new_markdown = read_version_markdown(review, to_version)
    return build_diff(old_markdown, new_markdown)


def restore_version(
    review: Dict[str, Any],
    *,
    version: str,
    restored_by: Optional[str],
    reason: Optional[str],
) -> Dict[str, Any]:
    source_entry = find_version(review, version)
    if not source_entry:
        raise FileNotFoundError(f"Version not found: {version}")
    markdown = read_version_markdown(review, version)
    restored_version = next_accepted_version(review)
    entry = create_version_files(
        review_dir=Path(review["review_dir"]),
        document_type=review["document_type"],
        module_slug=review.get("module_slug"),
        version=restored_version,
        status="restored",
        markdown=markdown,
        metadata={
            "restored_from": version,
            "restored_by": restored_by or "default_user",
            "restore_reason": reason,
        },
        docx_source=source_entry.get("docx_path"),
    )
    append_version(review, entry)
    review["current_version"] = restored_version
    review["status"] = "approved"
    review.setdefault("audit", []).append({
        "event": "version_restored",
        "version": restored_version,
        "restored_from": version,
        "actor": restored_by or "default_user",
        "reason": reason,
        "at": utc_now(),
    })
    save_review(review)
    return {"review": review, "version": restored_version}


def finalize_review(
    review: Dict[str, Any],
    *,
    version: Optional[str],
    finalized_by: Optional[str],
    role: Optional[str],
    comment: Optional[str],
) -> Dict[str, Any]:
    selected_version = version or review.get("current_version", "v1")
    entry = find_version(review, selected_version)
    if not entry:
        raise FileNotFoundError(f"Version not found: {selected_version}")
    if review.get("document_type") == "mdd" and review.get("stale"):
        raise RuntimeError("Cannot finalize stale MDD review. Regenerate or approve against latest HLD first.")
    entry["official"] = True
    entry["status"] = "final"
    review["current_version"] = selected_version
    review["status"] = "finalized"
    review.setdefault("audit", []).append({
        "event": "review_finalized",
        "version": selected_version,
        "actor": finalized_by or "default_user",
        "role": role or "architect",
        "comment": comment,
        "at": utc_now(),
    })
    save_review(review)
    return {"review": review, "version": selected_version}


def export_review(review: Dict[str, Any], audit_events: list[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "review": review,
        "audit_events": audit_events,
        "exported_at": utc_now(),
    }
