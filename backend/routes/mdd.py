"""MDD endpoints: module catalog discovery and per-module generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from models.schemas import (
    MDDGenerateRequest,
    MDDGenerateResponse,
    MDDModuleCatalogResponse,
)
from services.mdd_generator import generate_mdd_for_modules
from services.mdd_module_catalog import build_module_catalog, load_module_catalog

router = APIRouter()


def _artifact_dir() -> str:
    return os.getenv("ARTIFACT_DIR", "./artifacts")


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _catalog_stale(artifact_dir: str, catalog_path: str) -> bool:
    req_path = os.path.join(artifact_dir, "requirements.json")
    hld_path = os.path.join(artifact_dir, "HLD.md")
    cg_path = os.path.join(artifact_dir, "code_graph.json")

    cached_mtime = _mtime(catalog_path)
    latest_input_mtime = max(_mtime(req_path), _mtime(hld_path), _mtime(cg_path))
    return cached_mtime == 0 or cached_mtime < latest_input_mtime


def _to_catalog_response(catalog: Dict[str, Any]) -> MDDModuleCatalogResponse:
    return MDDModuleCatalogResponse(
        job_id=catalog.get("job_id", ""),
        ticket=catalog.get("ticket"),
        catalog_source=catalog.get("catalog_source", "requirements.json + HLD.md"),
        catalog_warnings=catalog.get("catalog_warnings", []),
        hld_path=catalog.get("hld_path"),
        module_count=catalog.get("module_count", len(catalog.get("modules", []))),
        modules=catalog.get("modules", []),
    )


@router.get("/modules", response_model=MDDModuleCatalogResponse)
async def get_modules() -> MDDModuleCatalogResponse:
    """Return the module catalog for MDD selection."""
    artifact_dir = _artifact_dir()
    catalog_path = os.path.join(artifact_dir, "mdd_modules.json")

    if not os.path.isfile(catalog_path) or _catalog_stale(artifact_dir, catalog_path):
        try:
            build_module_catalog(artifact_dir=artifact_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc))

    catalog = load_module_catalog(artifact_dir=artifact_dir)
    return _to_catalog_response(catalog)


@router.post("/modules/refresh", response_model=MDDModuleCatalogResponse)
async def refresh_modules() -> MDDModuleCatalogResponse:
    """Force rebuild of mdd_modules.json from latest artifacts."""
    artifact_dir = _artifact_dir()
    try:
        build_module_catalog(artifact_dir=artifact_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    catalog = load_module_catalog(artifact_dir=artifact_dir)
    return _to_catalog_response(catalog)


@router.post("/generate", response_model=MDDGenerateResponse)
async def generate_mdd(request: MDDGenerateRequest) -> MDDGenerateResponse:
    """Generate one MDD markdown file per selected module."""
    artifact_dir = _artifact_dir()
    try:
        result = generate_mdd_for_modules(
            selected_modules=request.selected_modules,
            ticket=request.ticket,
            artifact_dir=artifact_dir,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    return MDDGenerateResponse(
        job_id=result.job_id,
        ticket=result.ticket,
        started_at=result.started_at,
        completed_at=result.completed_at,
        generated=[
            {
                "module": r.module_name,
                "slug": r.slug,
                "path": r.artifact_path,
                "sections_included": r.sections_included,
                "sections_skipped": r.sections_skipped,
            }
            for r in result.generated
        ],
        manifest_path=result.manifest_path,
    )


@router.get("/manifest")
async def get_manifest() -> Dict[str, Any]:
    """Return last MDD generation manifest (paths + included/skipped sections)."""
    artifact_dir = _artifact_dir()
    manifest_path = os.path.join(artifact_dir, "mdd_manifest.json")
    if not os.path.isfile(manifest_path):
        raise HTTPException(status_code=404, detail="No MDD manifest found yet")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/{module_slug}")
async def download_mdd(module_slug: str) -> FileResponse:
    """Download a generated MDD markdown for the given module slug."""
    artifact_dir = _artifact_dir()
    manifest_path = os.path.join(artifact_dir, "mdd_manifest.json")
    if not os.path.isfile(manifest_path):
        raise HTTPException(status_code=404, detail="No MDD manifest found yet")

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    selected: Optional[Dict[str, Any]] = None
    for entry in manifest.get("generated", []) or []:
        if (entry.get("slug") or "").lower() == module_slug.lower():
            selected = entry
            break

    if not selected or not selected.get("path"):
        raise HTTPException(status_code=404, detail="No generated MDD found for this module")

    file_path = selected["path"]
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="MDD file not found on disk")

    return FileResponse(
        path=file_path,
        media_type="text/markdown",
        filename=os.path.basename(file_path),
    )

