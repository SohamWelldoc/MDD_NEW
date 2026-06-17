"""Codebase analysis endpoints."""

from fastapi import APIRouter, HTTPException

from models.schemas import (
    CodebaseAnalysisRequest,
    CodebaseAnalysisResponse,
)
from services.codebase_analyzer import analyze_codebase

router = APIRouter()


@router.post("/analyze", response_model=CodebaseAnalysisResponse)
async def analyze(request: CodebaseAnalysisRequest) -> CodebaseAnalysisResponse:
    """Resolve a feature contract against the monolith graph and requirements."""
    try:
        result = analyze_codebase(
            contract_path=request.contract_path,
            ticket=request.ticket,
            graph_path=request.graph_path,
            source_path=request.source_path,
        )
        return CodebaseAnalysisResponse(
            job_id=result.job_id,
            source_path=result.source_path,
            contract_path=result.contract_path,
            artifact_path=result.artifact_path,
            stats=result.code_graph.get("stats", {}),
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
