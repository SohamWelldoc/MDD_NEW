"""HLD generation endpoints — combines requirements + code_graph into HLD.md."""

import os

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
from services.codebase_analyzer import analyze_codebase
from services.hld_generator import generate_hld
from services.requirements_generator import generate_requirements

router = APIRouter()


@router.post("/generate", response_model=HLDGenerationResponse)
async def generate(request: HLDGenerationRequest) -> HLDGenerationResponse:
    """Generate HLD from the latest (or supplied) requirements + code_graph artifacts."""
    try:
        result = generate_hld(
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
async def get_latest_hld() -> str:
    """Return the most recently generated HLD as raw markdown."""
    artifact_dir = os.getenv("ARTIFACT_DIR", "./artifacts")
    path = os.path.join(artifact_dir, "HLD.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No HLD generated yet")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@router.post("/run", response_model=PipelineRunResponse)
async def run_full_pipeline(request: PipelineRunRequest) -> PipelineRunResponse:
    """End-to-end: requirements -> codebase -> HLD.

    Assumes Confluence ingestion has already populated the vector store
    via `/api/ingestion/start`.
    """
    try:
        req = generate_requirements(
            product=request.confluence_product,
            n_results=request.n_results,
        )
        code = analyze_codebase(
            contract_path=request.contract_path,
            ticket=request.ticket,
            graph_path=request.graph_path,
            source_path=request.source_path,
        )
        hld = generate_hld()

        return PipelineRunResponse(
            requirements=RequirementsGenerationResponse(
                job_id=req.job_id,
                product=req.product,
                artifact_path=req.artifact_path,
                started_at=req.started_at,
                completed_at=req.completed_at,
                requirements=req.requirements,
            ),
            codebase=CodebaseAnalysisResponse(
                job_id=code.job_id,
                source_path=code.source_path,
                contract_path=code.contract_path,
                artifact_path=code.artifact_path,
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
