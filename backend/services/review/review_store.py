"""JSON artifact storage for HLD/MDD review sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from services.artifact_store.artifact_paths import artifact_context, safe_segment
from services.review.section_detector import detect_feedback_section


VERSION_RE = re.compile(r"^v(\d+)(?:_draft)?$")
TIMESTAMP_RE = re.compile(r"(\d{14})")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def normalize_timestamp(value: Optional[str] = None) -> str:
    if value:
        match = TIMESTAMP_RE.search(str(value))
        if match:
            return match.group(1)
    return timestamp_now()


def normalize_document_type(document_type: str) -> str:
    value = (document_type or "").strip().lower()
    if value not in {"hld", "mdd"}:
        raise ValueError("document_type must be 'hld' or 'mdd'")
    return value


def normalize_module_slug(module_slug: Optional[str]) -> Optional[str]:
    if module_slug is None:
        return None
    return safe_segment(module_slug, "module")


def review_root(product: Optional[str], release: Optional[str], *, create: bool = True) -> Path:
    context = artifact_context(product=product, release=release, create=create)
    root = context.root_dir / "reviews"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def document_review_dir(
    product: Optional[str],
    release: Optional[str],
    document_type: str,
    module_slug: Optional[str] = None,
    *,
    create: bool = True,
) -> Path:
    document_type = normalize_document_type(document_type)
    root = review_root(product, release, create=create)
    if document_type == "hld":
        path = root / "hld"
    else:
        slug = normalize_module_slug(module_slug)
        if not slug:
            raise ValueError("module_slug is required for MDD reviews")
        path = root / "mdd" / slug
    if create:
        path.mkdir(parents=True, exist_ok=True)
        (path / "versions").mkdir(parents=True, exist_ok=True)
    return path


def version_dir_for_review(review: Dict[str, Any]) -> Path:
    return Path(review["review_dir"]) / "versions"


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def hydrate_review(review: Dict[str, Any]) -> Dict[str, Any]:
    if "review_id" not in review:
        return review
    for entry in review.get("versions", []) or []:
        if entry.get("artifact_name") and entry.get("artifact_timestamp"):
            continue
        source_metadata = entry.get("source_metadata") or {}
        artifact_timestamp = normalize_timestamp(
            entry.get("artifact_timestamp")
            or source_metadata.get("timestamp")
            or entry.get("source_path")
            or entry.get("path")
        )
        entry.setdefault("artifact_timestamp", artifact_timestamp)
        entry.setdefault(
            "artifact_name",
            version_file_stem(
                entry.get("document_type") or review.get("document_type"),
                entry.get("module_slug") or review.get("module_slug"),
                artifact_timestamp=artifact_timestamp,
                draft=str(entry.get("version", "")).endswith("_draft"),
            ),
        )
    return review


def review_file_path(review_id: str, product: Optional[str], release: Optional[str]) -> Path:
    review_id = safe_segment(review_id, "review")
    root = review_root(product, release, create=False)
    matches = list(root.glob(f"**/{review_id}.json"))
    if not matches:
        raise FileNotFoundError(f"Review not found: {review_id}")
    if len(matches) > 1:
        raise RuntimeError(f"Review id is ambiguous: {review_id}")
    return matches[0]


def load_review(review_id: str, product: Optional[str] = None, release: Optional[str] = None) -> Dict[str, Any]:
    review = hydrate_review(read_json(review_file_path(review_id, product, release)))
    try:
        from services.review.review_db import upsert_review

        upsert_review(review)
    except Exception:
        pass
    return review


def save_review(review: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(review["review_path"])
    review["updated_at"] = utc_now()
    atomic_write_json(path, review)
    try:
        from services.review.review_db import upsert_review

        upsert_review(review)
    except Exception:
        pass
    return review


def list_reviews(
    product: Optional[str],
    release: Optional[str],
    *,
    document_type: Optional[str] = None,
    module_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    root = review_root(product, release, create=False)
    if not root.is_dir():
        return []
    if document_type:
        doc_type = normalize_document_type(document_type)
        if doc_type == "hld":
            search_root = root / "hld"
        else:
            search_root = root / "mdd" / normalize_module_slug(module_slug) if module_slug else root / "mdd"
    else:
        search_root = root
    if not search_root.is_dir():
        return []
    reviews: List[Dict[str, Any]] = []
    for path in search_root.glob("**/review_*.json"):
        reviews.append(hydrate_review(read_json(path)))
    return sorted(reviews, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)


def next_review_id(document_type: str, module_slug: Optional[str] = None, existing: Iterable[Path] = ()) -> str:
    document_type = normalize_document_type(document_type)
    prefix = "review_HLD" if document_type == "hld" else f"review_MDD_{normalize_module_slug(module_slug)}"
    max_index = 0
    for path in existing:
        match = re.search(r"_(\d+)\.json$", path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return f"{prefix}_{max_index + 1:03d}"


def create_review(
    *,
    document_type: str,
    product: Optional[str],
    release: Optional[str],
    module_slug: Optional[str],
    created_by: Optional[str],
    source: Dict[str, Any],
) -> Dict[str, Any]:
    document_type = normalize_document_type(document_type)
    review_dir = document_review_dir(product, release, document_type, module_slug, create=True)
    review_id = next_review_id(document_type, module_slug, review_dir.glob("review_*.json"))
    review_path = review_dir / f"{review_id}.json"
    context = artifact_context(product=product, release=release, create=True)
    source_metadata = source.get("metadata", {}) or {}
    source_timestamp = normalize_timestamp(source_metadata.get("timestamp") or source.get("source_path"))
    version = create_version_files(
        review_dir=review_dir,
        document_type=document_type,
        module_slug=module_slug,
        version="v1",
        status="generated",
        markdown=source["markdown"],
        metadata={
            "source_path": source.get("source_path"),
            "source_docx_path": source.get("docx_path"),
            "source_metadata": source_metadata,
            "artifact_timestamp": source_timestamp,
            "created_by": created_by or "default_user",
        },
        docx_source=source.get("docx_path"),
    )
    review = {
        "review_id": review_id,
        "document_type": document_type,
        "product": context.product,
        "release": context.release,
        "module_slug": normalize_module_slug(module_slug),
        "review_dir": str(review_dir),
        "review_path": str(review_path),
        "current_version": "v1",
        "status": "in_review",
        "created_by": created_by or "default_user",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "feedback_items": [],
        "versions": [version],
        "stale": False,
        "audit": [
            {
                "event": "review_created",
                "version": "v1",
                "actor": created_by or "default_user",
                "at": utc_now(),
            }
        ],
    }
    atomic_write_json(review_path, review)
    try:
        from services.review.review_db import upsert_review

        upsert_review(review)
    except Exception:
        pass
    return review


def _version_prefix(document_type: str, module_slug: Optional[str]) -> str:
    if normalize_document_type(document_type) == "hld":
        return "HLD"
    return f"MDD_{normalize_module_slug(module_slug)}"


def version_file_stem(
    document_type: str,
    module_slug: Optional[str],
    *,
    artifact_timestamp: Optional[str] = None,
    draft: bool = False,
) -> str:
    suffix = "_draft" if draft else ""
    return f"{_version_prefix(document_type, module_slug)}_{normalize_timestamp(artifact_timestamp)}{suffix}"


def create_version_files(
    *,
    review_dir: Path,
    document_type: str,
    module_slug: Optional[str],
    version: str,
    status: str,
    markdown: str,
    metadata: Optional[Dict[str, Any]] = None,
    docx_source: Optional[str] = None,
    docx_path: Optional[str] = None,
) -> Dict[str, Any]:
    versions_dir = review_dir / "versions"
    metadata = metadata or {}
    artifact_timestamp = normalize_timestamp(metadata.get("artifact_timestamp"))
    stem = version_file_stem(
        document_type,
        module_slug,
        artifact_timestamp=artifact_timestamp,
        draft=version.endswith("_draft"),
    )
    json_path = versions_dir / f"{stem}.json"
    md_path = versions_dir / f"{stem}.md"
    final_docx = Path(docx_path) if docx_path else versions_dir / f"{stem}.docx"
    atomic_write_text(md_path, markdown)
    if docx_source and Path(docx_source).is_file() and not final_docx.exists():
        shutil.copy2(docx_source, final_docx)
    payload = {
        "version": version,
        "artifact_name": stem,
        "artifact_timestamp": artifact_timestamp,
        "document_type": normalize_document_type(document_type),
        "module_slug": normalize_module_slug(module_slug),
        "status": status,
        "markdown_path": str(md_path),
        "docx_path": str(final_docx) if final_docx.exists() else str(final_docx),
        "created_at": utc_now(),
        **metadata,
    }
    atomic_write_json(json_path, {**payload, "markdown": markdown})
    return {**payload, "path": str(json_path)}


def read_version_markdown(review: Dict[str, Any], version: str) -> str:
    entry = find_version(review, version)
    if not entry:
        raise FileNotFoundError(f"Version not found: {version}")
    path = Path(entry["markdown_path"])
    return path.read_text(encoding="utf-8")


def find_version(review: Dict[str, Any], version: str) -> Optional[Dict[str, Any]]:
    for entry in review.get("versions", []):
        if entry.get("version") == version:
            return entry
    return None


def accepted_version_number(version: str) -> int:
    match = VERSION_RE.match(version or "")
    if not match:
        return 0
    if version.endswith("_draft"):
        return 0
    return int(match.group(1))


def next_accepted_version(review: Dict[str, Any]) -> str:
    max_version = max((accepted_version_number(v.get("version", "")) for v in review.get("versions", [])), default=0)
    return f"v{max_version + 1}"


def next_draft_version(review: Dict[str, Any]) -> str:
    return f"{next_accepted_version(review)}_draft"


def add_feedback(
    review: Dict[str, Any],
    *,
    feedback: str,
    target_section: Optional[str],
    change_type: Optional[str] = None,
    priority: Optional[str] = None,
    target_kind: Optional[str] = None,
    reviewer_expectation: Optional[str] = None,
    base_version: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    current = review.get("current_version", "v1")
    base = base_version or current
    status = "open" if base == current else "conflict"
    section_detection: Optional[Dict[str, Any]] = None
    resolved_target_section = target_section
    if not resolved_target_section:
        try:
            current_markdown = read_version_markdown(review, current)
            section_detection = detect_feedback_section(
                markdown=current_markdown,
                feedback=feedback,
                change_type=change_type,
                target_kind=target_kind,
            )
            resolved_target_section = section_detection.get("target_section")
        except Exception:
            section_detection = {
                "target_section": None,
                "confidence": 0.0,
                "reason": "Section detection failed; full document will be used.",
            }
    item = {
        "feedback_id": f"fb_{uuid.uuid4().hex[:8]}",
        "base_version": base,
        "target_section": resolved_target_section,
        "section_detection": section_detection,
        "change_type": change_type,
        "priority": priority or "medium",
        "target_kind": target_kind or ("section" if resolved_target_section else "full_document"),
        "reviewer_expectation": reviewer_expectation,
        "feedback": feedback,
        "status": status,
        "reviewer": reviewer or "default_user",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    review.setdefault("feedback_items", []).append(item)
    review.setdefault("audit", []).append({
        "event": "feedback_submitted",
        "feedback_id": item["feedback_id"],
        "base_version": base,
        "actor": reviewer or "default_user",
        "at": utc_now(),
        "status": status,
    })
    if status == "open":
        review["status"] = "changes_requested"
    save_review(review)
    return item


def find_feedback(review: Dict[str, Any], feedback_id: str) -> Dict[str, Any]:
    for item in review.get("feedback_items", []):
        if item.get("feedback_id") == feedback_id:
            return item
    raise FileNotFoundError(f"Feedback not found: {feedback_id}")


def append_version(review: Dict[str, Any], version_entry: Dict[str, Any]) -> Dict[str, Any]:
    review.setdefault("versions", []).append(version_entry)
    return save_review(review)


def mark_mdd_reviews_stale(product: str, release: str, hld_version: str, actor: str) -> List[str]:
    marked: List[str] = []
    for review in list_reviews(product, release, document_type="mdd"):
        if review.get("status") == "approved":
            review["status"] = "stale_due_to_hld_change"
        review["stale"] = True
        review["stale_reason"] = f"HLD changed to {hld_version}"
        review.setdefault("audit", []).append({
            "event": "marked_stale_due_to_hld_change",
            "hld_version": hld_version,
            "actor": actor,
            "at": utc_now(),
        })
        save_review(review)
        marked.append(review["review_id"])
    return marked
