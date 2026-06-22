"""Requirements generation endpoints."""

from fastapi import APIRouter, HTTPException

from models.schemas import (
    RequirementsGenerationRequest,
    RequirementsGenerationResponse,
)
from services.requirements.requirements_generator import generate_requirements

router = APIRouter()


@router.post("/generate", response_model=RequirementsGenerationResponse)
async def generate(request: RequirementsGenerationRequest) -> RequirementsGenerationResponse:
    """Extract a structured requirements document from the ingested Confluence corpus."""
    try:
        result = generate_requirements(
            product=request.product,
            release=request.release,
            n_results=request.n_results,
        )
        return RequirementsGenerationResponse(
            job_id=result.job_id,
            product=result.product,
            release=result.release,
            artifact_path=result.artifact_path,
            artifact_paths={
                "requirements": result.artifact_path,
            },
            started_at=result.started_at,
            completed_at=result.completed_at,
            requirements=result.requirements,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
