"""Human-in-the-loop review endpoints for HLD/MDD artifacts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, Response

from models.schemas import (
    ReviewCreateRequest,
    ReviewChangePlanRequest,
    ReviewChangePlanResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewDiffResponse,
    ReviewFeedbackRequest,
    ReviewFeedbackResponse,
    ReviewListResponse,
    ReviewResponse,
    ReviewRestoreRequest,
    ReviewReviseRequest,
    ReviewJobActionRequest,
    ReviewFinalizeRequest,
    ReviewRevisionJobResponse,
    ReviewRevisionStatusResponse,
    ReviewVersionsResponse,
)
from services.review.approval_workflow import approve_draft, reject_draft
from services.review.change_planner import build_change_plan
from services.review.diff_builder import build_diff
from services.review.document_loader import load_generated_document
from services.review.document_reviser import create_draft_revision
from services.review.review_store import (
    add_feedback,
    create_review,
    find_version,
    list_reviews,
    load_review,
    read_version_markdown,
    save_review,
    utc_now,
)
from services.review.review_db import find_active_job, list_audit, load_job, save_job
from services.review.versioning import compare_versions, export_review, finalize_review, restore_version

router = APIRouter()
revision_jobs: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_or_404(review_id: str, product: Optional[str], release: Optional[str]) -> Dict[str, Any]:
    try:
        return load_review(review_id, product=product, release=release)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/create", response_model=ReviewResponse)
async def create_review_session(request: ReviewCreateRequest) -> ReviewResponse:
    try:
        source = load_generated_document(
            document_type=request.document_type,
            product=request.product,
            release=request.release,
            module_slug=request.module_slug,
        )
        review = create_review(
            document_type=request.document_type,
            product=request.product,
            release=request.release,
            module_slug=request.module_slug,
            created_by=request.created_by,
            source=source,
        )
        return ReviewResponse(review=review)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=ReviewListResponse)
async def get_reviews(
    product: Optional[str] = None,
    release: Optional[str] = None,
    document_type: Optional[str] = None,
    module_slug: Optional[str] = None,
) -> ReviewListResponse:
    try:
        return ReviewListResponse(
            reviews=list_reviews(product, release, document_type=document_type, module_slug=module_slug)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{review_id}", response_model=ReviewResponse)
async def get_review(
    review_id: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewResponse:
    return ReviewResponse(review=_load_or_404(review_id, product, release))


@router.post("/{review_id}/feedback", response_model=ReviewFeedbackResponse)
async def submit_feedback(
    review_id: str,
    request: ReviewFeedbackRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewFeedbackResponse:
    review = _load_or_404(review_id, product, release)
    try:
        feedback = add_feedback(
            review,
            feedback=request.feedback,
            target_section=request.target_section,
            change_type=request.change_type,
            priority=request.priority,
            target_kind=request.target_kind,
            reviewer_expectation=request.reviewer_expectation,
            base_version=request.base_version,
            reviewer=request.reviewer,
        )
        return ReviewFeedbackResponse(review_id=review["review_id"], feedback=feedback, review=review)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{review_id}/plan-change", response_model=ReviewChangePlanResponse)
async def plan_review_change(
    review_id: str,
    request: ReviewChangePlanRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewChangePlanResponse:
    review = _load_or_404(review_id, product, release)
    try:
        plan = build_change_plan(review, request.feedback_id, request.requested_by)
        return ReviewChangePlanResponse(
            review_id=review["review_id"],
            feedback_id=request.feedback_id,
            change_plan=plan,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


def _run_revision_job(
    job_id: str,
    review_id: str,
    request: ReviewReviseRequest,
    product: Optional[str],
    release: Optional[str],
) -> None:
    job = revision_jobs[job_id]

    def update(progress: int, message: str) -> None:
        job.update(progress=progress, message=message)
        save_job(product, release, job)

    try:
        job.update(status="processing", progress=5, message="Starting revision job...")
        save_job(product, release, job)
        review = load_review(review_id, product=product, release=release)
        if job.get("cancelled"):
            job.update(status="cancelled", progress=100, message="Revision job cancelled.", completed_at=_now())
            save_job(product, release, job)
            return
        result = create_draft_revision(
            review,
            feedback_id=request.feedback_id,
            requested_by=request.requested_by,
            progress_callback=update,
        )
        job.update(
            status="completed",
            progress=100,
            message="Draft revision completed.",
            completed_at=_now(),
            result={
                "review_id": review["review_id"],
                "feedback_id": request.feedback_id,
                "draft_version": result["draft_version"],
                "classification": result["classification"],
                "change_plan": result.get("change_plan", {}),
                "evidence_summary": result.get("evidence_summary", {}),
                "validation_report": result["validation_report"],
                "diff": result["diff"],
                "review": result["review"],
            },
        )
        save_job(product, release, job)
    except Exception as exc:  # noqa: BLE001
        job.update(
            status="failed",
            progress=100,
            message="Draft revision failed.",
            error=str(exc),
            completed_at=_now(),
        )
        save_job(product, release, job)


@router.post("/{review_id}/revise", response_model=ReviewRevisionJobResponse)
async def revise_review(
    review_id: str,
    request: ReviewReviseRequest,
    background_tasks: BackgroundTasks,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewRevisionJobResponse:
    review = _load_or_404(review_id, product, release)
    active = find_active_job(product, release, review["review_id"], request.feedback_id)
    if active:
        return ReviewRevisionJobResponse(
            review_id=review["review_id"],
            job_id=active["job_id"],
            status=active["status"],
            message="An active revision job already exists for this feedback.",
        )
    job_id = str(uuid.uuid4())
    revision_jobs[job_id] = {
        "review_id": review["review_id"],
        "job_id": job_id,
        "status": "pending",
        "feedback_id": request.feedback_id,
        "progress": 0,
        "message": "Revision queued.",
        "started_at": _now(),
        "completed_at": None,
    }
    save_job(product, release, revision_jobs[job_id])
    background_tasks.add_task(_run_revision_job, job_id, review_id, request, product, release)
    return ReviewRevisionJobResponse(
        review_id=review["review_id"],
        job_id=job_id,
        status="pending",
        message="Revision job started.",
    )


@router.get("/{review_id}/revise/status/{job_id}", response_model=ReviewRevisionStatusResponse)
async def get_revision_status(
    review_id: str,
    job_id: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewRevisionStatusResponse:
    _load_or_404(review_id, product, release)
    job = revision_jobs.get(job_id)
    if not job:
        job = load_job(product, release, job_id)
    if not job or job.get("review_id") != review_id:
        raise HTTPException(status_code=404, detail="Revision job not found")
    return ReviewRevisionStatusResponse(**job)


@router.post("/{review_id}/revise/{job_id}/cancel", response_model=ReviewRevisionStatusResponse)
async def cancel_revision_job(
    review_id: str,
    job_id: str,
    request: ReviewJobActionRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewRevisionStatusResponse:
    _load_or_404(review_id, product, release)
    job = revision_jobs.get(job_id) or load_job(product, release, job_id)
    if not job or job.get("review_id") != review_id:
        raise HTTPException(status_code=404, detail="Revision job not found")
    if job.get("status") in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Job already finished")
    job.update(status="cancelled", cancelled=True, progress=100, message=request.reason or "Cancelled by user", completed_at=_now())
    revision_jobs[job_id] = job
    save_job(product, release, job)
    return ReviewRevisionStatusResponse(**job)


@router.post("/{review_id}/revise/{job_id}/retry", response_model=ReviewRevisionJobResponse)
async def retry_revision_job(
    review_id: str,
    job_id: str,
    request: ReviewJobActionRequest,
    background_tasks: BackgroundTasks,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewRevisionJobResponse:
    _load_or_404(review_id, product, release)
    job = revision_jobs.get(job_id) or load_job(product, release, job_id)
    if not job or job.get("review_id") != review_id:
        raise HTTPException(status_code=404, detail="Revision job not found")
    feedback_id = job.get("feedback_id")
    if not feedback_id:
        raise HTTPException(status_code=400, detail="Original feedback id missing")
    if job.get("status") in {"pending", "processing"}:
        raise HTTPException(status_code=409, detail="Cannot retry an active revision job")
    retry_request = ReviewReviseRequest(feedback_id=feedback_id, requested_by=request.requested_by)
    return await revise_review(review_id, retry_request, background_tasks, product, release)


@router.get("/{review_id}/diff/{draft_version}", response_model=ReviewDiffResponse)
async def get_review_diff(
    review_id: str,
    draft_version: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewDiffResponse:
    review = _load_or_404(review_id, product, release)
    try:
        draft = find_version(review, draft_version)
        if not draft:
            raise FileNotFoundError(f"Draft version not found: {draft_version}")
        base_version = draft.get("base_version") or review.get("current_version", "v1")
        old_markdown = read_version_markdown(review, base_version)
        new_markdown = read_version_markdown(review, draft_version)
        target_section = None
        feedback_id = draft.get("feedback_id")
        if feedback_id:
            for item in review.get("feedback_items", []):
                if item.get("feedback_id") == feedback_id:
                    target_section = item.get("target_section")
                    break
        return ReviewDiffResponse(
            review_id=review["review_id"],
            base_version=base_version,
            draft_version=draft_version,
            diff=build_diff(old_markdown, new_markdown, target_section=target_section),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{review_id}/approve", response_model=ReviewDecisionResponse)
async def approve_review_draft(
    review_id: str,
    request: ReviewDecisionRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewDecisionResponse:
    review = _load_or_404(review_id, product, release)
    try:
        result = approve_draft(
            review,
            draft_version=request.draft_version,
            feedback_id=request.feedback_id,
            decided_by=request.decided_by,
            reason=request.reason,
            role=request.role,
            source_ip=request.source_ip,
        )
        return ReviewDecisionResponse(
            review_id=review["review_id"],
            version=result.get("version"),
            draft_version=request.draft_version,
            review=result["review"],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{review_id}/reject", response_model=ReviewDecisionResponse)
async def reject_review_draft(
    review_id: str,
    request: ReviewDecisionRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewDecisionResponse:
    review = _load_or_404(review_id, product, release)
    try:
        result = reject_draft(
            review,
            draft_version=request.draft_version,
            feedback_id=request.feedback_id,
            decided_by=request.decided_by,
            reason=request.reason,
            role=request.role,
            source_ip=request.source_ip,
        )
        return ReviewDecisionResponse(
            review_id=review["review_id"],
            draft_version=request.draft_version,
            review=result["review"],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{review_id}/versions", response_model=ReviewVersionsResponse)
async def get_versions(
    review_id: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewVersionsResponse:
    review = _load_or_404(review_id, product, release)
    return ReviewVersionsResponse(
        review_id=review["review_id"],
        current_version=review.get("current_version", "v1"),
        versions=review.get("versions", []),
    )


@router.get("/{review_id}/compare")
async def compare_review_versions(
    review_id: str,
    from_version: str = Query(..., alias="from"),
    to_version: str = Query(..., alias="to"),
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    review = _load_or_404(review_id, product, release)
    try:
        return {
            "review_id": review["review_id"],
            "from_version": from_version,
            "to_version": to_version,
            "diff": compare_versions(review, from_version, to_version),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{review_id}/restore", response_model=ReviewDecisionResponse)
async def restore_review_version(
    review_id: str,
    request: ReviewRestoreRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewDecisionResponse:
    review = _load_or_404(review_id, product, release)
    try:
        result = restore_version(
            review,
            version=request.version,
            restored_by=request.restored_by,
            reason=request.reason,
        )
        return ReviewDecisionResponse(
            review_id=review["review_id"],
            version=result["version"],
            draft_version=request.version,
            review=result["review"],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{review_id}/finalize")
async def finalize_review_endpoint(
    review_id: str,
    request: ReviewFinalizeRequest,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    review = _load_or_404(review_id, product, release)
    try:
        return finalize_review(
            review,
            version=request.version,
            finalized_by=request.finalized_by,
            role=request.role,
            comment=request.comment,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{review_id}/audit")
async def get_review_audit(
    review_id: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    review = _load_or_404(review_id, product, release)
    db_events = list_audit(product, release, review_id)
    return {"review_id": review["review_id"], "audit": db_events or review.get("audit", [])}


@router.get("/{review_id}/export")
async def export_review_endpoint(
    review_id: str,
    format: str = "json",
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    if format.lower() != "json":
        raise HTTPException(status_code=400, detail="Only JSON export is supported")
    review = _load_or_404(review_id, product, release)
    return export_review(review, list_audit(product, release, review_id) or review.get("audit", []))


@router.post("/{review_id}/comments")
async def add_review_comment(
    review_id: str,
    payload: Dict[str, Any],
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> ReviewResponse:
    review = _load_or_404(review_id, product, release)
    comment = {
        "comment_id": str(uuid.uuid4()),
        "section": payload.get("section"),
        "comment": payload.get("comment", ""),
        "actor": payload.get("actor") or "default_user",
        "status": payload.get("status") or "open",
        "created_at": utc_now(),
    }
    review.setdefault("comments", []).append(comment)
    review.setdefault("audit", []).append({"event": "comment_added", **comment, "at": utc_now()})
    save_review(review)
    return ReviewResponse(review=review)


@router.get("/{review_id}/search")
async def search_review(
    review_id: str,
    q: str,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Dict[str, Any]:
    review = _load_or_404(review_id, product, release)
    needle = (q or "").lower()
    matches = []
    for collection in ("feedback_items", "comments", "versions", "audit"):
        for item in review.get(collection, []) or []:
            if needle in str(item).lower():
                matches.append({"collection": collection, "item": item})
    return {"review_id": review_id, "query": q, "matches": matches}


@router.get("/{review_id}/download")
async def download_version(
    review_id: str,
    version: Optional[str] = None,
    format: str = "docx",
    product: Optional[str] = None,
    release: Optional[str] = None,
):
    review = _load_or_404(review_id, product, release)
    selected_version = version or review.get("current_version", "v1")
    entry = find_version(review, selected_version)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Version not found: {selected_version}")
    if format.lower() == "md":
        path = Path(entry.get("markdown_path", ""))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Markdown version not found")
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )
    path = Path(entry.get("docx_path", ""))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="DOCX version not found")
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )
