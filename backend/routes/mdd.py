"""MDD endpoints: module catalog discovery and per-module generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from models.schemas import (
    MDDGenerateRequest,
    MDDGenerateResponse,
    MDDModuleCatalogResponse,
)
from services.mdd.mdd_generator import generate_mdd_for_modules
from services.mdd.mdd_module_catalog import build_module_catalog, load_module_catalog
from services.artifact_store.artifact_paths import artifact_context

router = APIRouter()


def _artifact_dir(product: str = None, release: str = None) -> str:
    return str(artifact_context(product=product, release=release, create=True).stage_dir("mdd"))


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _catalog_stale(artifact_dir: str, catalog_path: str, product: str = None, release: str = None) -> bool:
    context = artifact_context(product=product, release=release, create=False)
    req_path = _latest_matching(context.stage_dir("hld", create=False), "requirements_*.json")
    hld_path = _latest_matching(context.stage_dir("hld", create=False), "*/HLD_*.json")
    cg_path = _latest_matching(context.stage_dir("codebase", create=False), "code_graph_*.json")

    cached_mtime = _mtime(catalog_path)
    latest_input_mtime = max(_mtime(req_path or ""), _mtime(hld_path or ""), _mtime(cg_path or ""))
    return cached_mtime == 0 or cached_mtime < latest_input_mtime


def _latest_matching(stage_dir: Path, pattern: str) -> Optional[str]:
    matches = [path for path in stage_dir.glob(pattern) if path.is_file()]
    if not matches:
        return None
    return str(max(matches, key=lambda path: path.stat().st_mtime))


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
async def get_modules(product: str = None, release: str = None) -> MDDModuleCatalogResponse:
    """Return the module catalog for MDD selection."""
    artifact_dir = _artifact_dir(product, release)
    catalog_path = _latest_matching(Path(artifact_dir), "mdd_modules_*.json")

    if not catalog_path or _catalog_stale(artifact_dir, catalog_path, product, release):
        try:
            build_module_catalog(product=product, release=release, artifact_dir=artifact_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc))

    catalog = load_module_catalog(artifact_dir=artifact_dir, product=product, release=release)
    return _to_catalog_response(catalog)


@router.post("/modules/refresh", response_model=MDDModuleCatalogResponse)
async def refresh_modules(product: str = None, release: str = None) -> MDDModuleCatalogResponse:
    """Force rebuild of mdd_modules.json from latest artifacts."""
    artifact_dir = _artifact_dir(product, release)
    try:
        build_module_catalog(product=product, release=release, artifact_dir=artifact_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

    catalog = load_module_catalog(artifact_dir=artifact_dir, product=product, release=release)
    return _to_catalog_response(catalog)


@router.post("/generate", response_model=MDDGenerateResponse)
async def generate_mdd(request: MDDGenerateRequest) -> MDDGenerateResponse:
    """Generate one MDD markdown file per selected module."""
    artifact_dir = _artifact_dir(request.product, request.release)
    try:
        result = generate_mdd_for_modules(
            selected_modules=request.selected_modules,
            ticket=request.ticket,
            product=request.product,
            release=request.release,
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
                "plan_path": r.artifact_path,
                "docx_path": r.docx_path,
                "sections_included": r.sections_included,
                "sections_skipped": r.sections_skipped,
            }
            for r in result.generated
        ],
        manifest_path=result.manifest_path,
    )


@router.get("/manifest")
async def get_manifest(product: str = None, release: str = None) -> Dict[str, Any]:
    """Return last MDD generation manifest (paths + included/skipped sections)."""
    artifact_dir = _artifact_dir(product, release)
    manifest_path = _latest_matching(Path(artifact_dir), "mdd_manifest_*.json")
    if not manifest_path:
        raise HTTPException(status_code=404, detail="No MDD manifest found yet")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/{module_slug}")
async def download_mdd(module_slug: str, product: str = None, release: str = None, format: str = "docx"):
    """Download a generated MDD markdown for the given module slug."""
    artifact_dir = _artifact_dir(product, release)
    manifest_path = _latest_matching(Path(artifact_dir), "mdd_manifest_*.json")
    if not manifest_path:
        raise HTTPException(status_code=404, detail="No MDD manifest found yet")

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    selected: Optional[Dict[str, Any]] = None
    for entry in manifest.get("generated", []) or []:
        if (entry.get("slug") or "").lower() == module_slug.lower():
            selected = entry
            break

    if not selected:
        raise HTTPException(status_code=404, detail="No generated MDD found for this module")

    if format.lower() == "md":
        plan_path = selected.get("plan_path") or selected.get("path")
        if not plan_path or not os.path.isfile(plan_path):
            raise HTTPException(status_code=404, detail="MDD plan JSON not found on disk")
        with open(plan_path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
        markdown = plan.get("mdd_markdown")
        if not markdown:
            raise HTTPException(status_code=404, detail="Embedded MDD markdown not found")
        return Response(
            content=markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="MDD_{module_slug}.md"'},
        )

    file_path = selected.get("docx_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="MDD file not found on disk")

    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=os.path.basename(file_path),
    )

