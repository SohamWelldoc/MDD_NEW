"""Demo helper endpoints for the static HLD/MDD showcase UI."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from models.schemas import IngestionRequest, IngestionResponse, IngestionStatus
from routes.ingestion import ingestion_jobs, run_ingestion
from services.artifact_store.artifact_paths import (
    ArtifactContext,
    artifact_base_dir,
    artifact_context,
    latest_artifact_root,
    latest_matching,
    safe_segment,
)

router = APIRouter()


class DemoIngestionRequest(BaseModel):
    confluence_page_url: str = Field(..., min_length=10, max_length=1000)
    page_id: Optional[str] = Field(None, max_length=100)
    product: Optional[str] = Field(None, max_length=100)
    release: Optional[str] = Field(None, max_length=100)


class DemoContractRequest(BaseModel):
    contract: Dict[str, Any]
    product: Optional[str] = Field(None, max_length=100)
    release: Optional[str] = Field(None, max_length=100)
    ticket: Optional[str] = Field(None, max_length=100)


class DemoArtifactStatus(BaseModel):
    product: str
    release: str
    artifact_root: str
    has_ingestion: bool
    has_requirements: bool
    has_codebase: bool
    has_hld: bool
    paths: Dict[str, Optional[str]]


def _env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{name} is missing in .env")
    return value


def _infer_ticket(contract: Dict[str, Any], fallback: Optional[str]) -> str:
    for key in ("ticket", "ticket_id", "id", "feature_id", "jira"):
        value = contract.get(key)
        if value:
            return safe_segment(str(value), "contract")
    return safe_segment(fallback or os.getenv("TICKET") or "contract", "contract")


def _normalize_page_url(value: str) -> str:
    url = (value or "").strip()
    while url.lower().startswith("https://https://"):
        url = "https://" + url[16:]
    while url.lower().startswith("http://http://"):
        url = "http://" + url[14:]
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    if not parsed.netloc or parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Enter a valid Confluence page URL.")
    return url


def _normalize_confluence_base_url(value: str) -> str:
    url = _normalize_page_url(value)
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    base_path = "/wiki" if "wiki" in [part.lower() for part in path_parts] else ""
    return f"{parsed.scheme}://{parsed.netloc}{base_path}"


def _resolved_context(product: Optional[str], release: Optional[str]):
    root = latest_artifact_root(product, release)
    if root:
        return ArtifactContext(
            product=safe_segment(product or root.parent.name, "default"),
            release=safe_segment(release or root.name, "default"),
            timestamp="",
            root_dir=root,
        )
    return artifact_context(product=product, release=release, create=True)


@router.get("/artifacts/status", response_model=DemoArtifactStatus)
async def artifact_status(product: str = None, release: str = None) -> DemoArtifactStatus:
    """Return latest reusable artifacts for the demo fast mode."""
    context = _resolved_context(product, release)
    confluence_dir = context.stage_dir("confluence", create=False)
    hld_dir = context.stage_dir("hld", create=False)
    codebase_dir = context.stage_dir("codebase", create=False)

    chunks = latest_matching(confluence_dir, "chunks_*.jsonl")
    embeddings = latest_matching(confluence_dir, "embeddings_*.jsonl")
    requirements = latest_matching(hld_dir, "requirements_*.json")
    code_graph = latest_matching(codebase_dir, "code_graph_*.json")
    contract = latest_matching(codebase_dir, "contract_*.json")
    hld_json = latest_matching(hld_dir, "*/HLD_*.json")
    hld_docx = latest_matching(hld_dir, "*/HLD_*.docx")

    return DemoArtifactStatus(
        product=context.product,
        release=context.release,
        artifact_root=str(context.root_dir),
        has_ingestion=bool(chunks and embeddings),
        has_requirements=bool(requirements),
        has_codebase=bool(code_graph),
        has_hld=bool(hld_json),
        paths={
            "chunks": str(chunks) if chunks else None,
            "embeddings": str(embeddings) if embeddings else None,
            "requirements": str(requirements) if requirements else None,
            "code_graph": str(code_graph) if code_graph else None,
            "contract": str(contract) if contract else None,
            "hld_json": str(hld_json) if hld_json else None,
            "hld_docx": str(hld_docx) if hld_docx else None,
        },
    )


@router.post("/ingest", response_model=IngestionResponse)
async def start_demo_ingestion(
    request: DemoIngestionRequest,
    background_tasks: BackgroundTasks,
) -> IngestionResponse:
    """Start Confluence ingestion using credentials from .env for demo safety."""
    job_id = str(uuid.uuid4())
    ingestion_jobs[job_id] = IngestionStatus(
        job_id=job_id,
        status="pending",
        progress=0,
        pages_processed=0,
        chunks_created=0,
        product=request.product,
        release=request.release,
        started_at=datetime.now().isoformat(),
    )
    background_tasks.add_task(
        run_ingestion,
        job_id,
        IngestionRequest(
            confluence_url=_normalize_confluence_base_url(
                os.getenv("CONFLUENCE_URL", "https://welldoc.atlassian.net/wiki")
            ),
            confluence_page_url=_normalize_page_url(request.confluence_page_url),
            username=_env_required("CONFLUENCE_USERNAME"),
            api_token=_env_required("CONFLUENCE_API_TOKEN"),
            page_id=request.page_id,
            product=request.product,
            release=request.release,
            clear_existing=True,
            clear_product_only=True,
        ),
    )
    return IngestionResponse(
        job_id=job_id,
        status="started",
        message="Demo ingestion started",
    )


@router.post("/contract")
async def save_demo_contract(request: DemoContractRequest) -> Dict[str, Any]:
    """Persist pasted/uploaded contract JSON into the current product/release artifacts."""
    context = artifact_context(product=request.product, release=request.release, create=True)
    ticket = _infer_ticket(request.contract, request.ticket)
    contract_dir = context.stage_dir("ref")
    path = contract_dir / f"contract_{ticket}_{context.timestamp}.json"
    path.write_text(json.dumps(request.contract, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "product": context.product,
        "release": context.release,
        "ticket": ticket,
        "contract_path": str(path),
    }


@router.get("/artifact")
async def download_artifact(path: str = Query(..., min_length=1)) -> FileResponse:
    """Download a generated artifact by absolute path, restricted to ARTIFACT_BASE_DIR."""
    artifact_root = artifact_base_dir().resolve()
    requested = Path(path).resolve()
    try:
        requested.relative_to(artifact_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Artifact path is outside artifact storage") from exc
    if not requested.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path=str(requested), filename=requested.name)
