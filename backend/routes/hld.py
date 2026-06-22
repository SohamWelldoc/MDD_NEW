"""HLD generation endpoints — combines requirements + code_graph into HLD.md."""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from models.schemas import (
    HLDGenerationRequest,
    HLDGenerationResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    RequirementsGenerationResponse,
    CodebaseAnalysisResponse,
)
from services.codebase.codebase_analyzer import analyze_codebase
from services.hld.hld_generator import generate_hld
from services.requirements.requirements_generator import generate_requirements
from services.artifact_store.artifact_paths import artifact_context

router = APIRouter()


@router.post("/generate", response_model=HLDGenerationResponse)
async def generate(request: HLDGenerationRequest) -> HLDGenerationResponse:
    """Generate HLD from the latest (or supplied) requirements + code_graph artifacts."""
    try:
        result = generate_hld(
            product=request.product,
            release=request.release,
            requirements_path=request.requirements_path,
            code_graph_path=request.code_graph_path,
        )
        return HLDGenerationResponse(
            job_id=result.job_id,
            plan=result.plan,
            diagram_report=result.diagram_report,
            artifact_paths=result.artifact_paths,
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/latest", response_class=PlainTextResponse)
async def get_latest_hld(product: str = None, release: str = None) -> str:
    """Return the most recently generated HLD as raw markdown."""
    context = artifact_context(product=product, release=release, create=False)
    hld_dir = context.stage_dir("hld", create=False)
    latest = _latest_hld_json(Path(hld_dir))
    path = str(latest) if latest else os.path.join(hld_dir, "HLD.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No HLD generated yet")
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    markdown = payload.get("hld_markdown")
    if not markdown:
        raise HTTPException(status_code=404, detail="Latest HLD JSON does not contain hld_markdown")
    return markdown


def _latest_hld_json(hld_dir: Path) -> Path | None:
    matches = [path for path in hld_dir.glob("*/HLD_*.json") if path.is_file()]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


@router.post("/run", response_model=PipelineRunResponse)
async def run_full_pipeline(request: PipelineRunRequest) -> PipelineRunResponse:
    """End-to-end: requirements -> codebase -> HLD.

    Assumes Confluence ingestion has already populated the vector store
    via `/api/ingestion/start`.
    """
    try:
        req = generate_requirements(
            product=request.confluence_product,
            release=request.release,
            n_results=request.n_results,
        )
        code = analyze_codebase(
            product=request.confluence_product,
            release=request.release,
            contract_path=request.contract_path,
            ticket=request.ticket,
            graph_path=request.graph_path,
            source_path=request.source_path,
        )
        hld = generate_hld(product=request.confluence_product, release=request.release)

        return PipelineRunResponse(
            requirements=RequirementsGenerationResponse(
                job_id=req.job_id,
                product=req.product,
                release=req.release,
                artifact_path=req.artifact_path,
                artifact_paths={"requirements": req.artifact_path},
                started_at=req.started_at,
                completed_at=req.completed_at,
                requirements=req.requirements,
            ),
            codebase=CodebaseAnalysisResponse(
                job_id=code.job_id,
                source_path=code.source_path,
                contract_path=code.contract_path,
                artifact_path=code.artifact_path,
                artifact_paths={"code_graph": code.artifact_path},
                stats=code.code_graph.get("stats", {}),
                started_at=code.started_at,
                completed_at=code.completed_at,
            ),
            hld=HLDGenerationResponse(
                job_id=hld.job_id,
                plan=hld.plan,
                diagram_report=hld.diagram_report,
                artifact_paths=hld.artifact_paths,
                started_at=hld.started_at,
                completed_at=hld.completed_at,
            ),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
