"""Load generated HLD/MDD artifacts into review sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from services.artifact_store.artifact_paths import artifact_context, latest_matching, safe_segment


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _latest_hld_json(product: Optional[str], release: Optional[str]) -> Path:
    context = artifact_context(product=product, release=release, create=False)
    hld_dir = context.stage_dir("hld", create=False)
    path = latest_matching(hld_dir, "*/HLD_*.json")
    if not path:
        raise FileNotFoundError("No generated HLD found. Run /api/hld/generate first.")
    return path


def _latest_mdd_manifest(product: Optional[str], release: Optional[str]) -> Path:
    context = artifact_context(product=product, release=release, create=False)
    mdd_dir = context.stage_dir("mdd", create=False)
    path = latest_matching(mdd_dir, "mdd_manifest_*.json")
    if not path:
        raise FileNotFoundError("No generated MDD manifest found. Run /api/mdd/generate first.")
    return path


def load_generated_document(
    *,
    document_type: str,
    product: Optional[str],
    release: Optional[str],
    module_slug: Optional[str] = None,
) -> Dict[str, Any]:
    document_type = (document_type or "").lower()
    if document_type == "hld":
        return load_generated_hld(product=product, release=release)
    if document_type == "mdd":
        return load_generated_mdd(product=product, release=release, module_slug=module_slug)
    raise ValueError("document_type must be 'hld' or 'mdd'")


def load_generated_hld(product: Optional[str], release: Optional[str]) -> Dict[str, Any]:
    path = _latest_hld_json(product, release)
    payload = _load_json(path)
    markdown = payload.get("hld_markdown")
    if not markdown:
        raise FileNotFoundError(f"HLD JSON does not contain hld_markdown: {path}")
    return {
        "markdown": markdown,
        "source_path": str(path),
        "docx_path": payload.get("docx_path"),
        "metadata": {
            "job_id": payload.get("job_id"),
            "timestamp": payload.get("timestamp"),
            "requirements_source": payload.get("requirements_source"),
            "code_graph_source": payload.get("code_graph_source"),
            "diagram_report": payload.get("diagram_report", {}),
            "accuracy_report": payload.get("accuracy_report", {}),
            "plan": payload.get("plan", {}),
        },
    }


def load_generated_mdd(
    *,
    product: Optional[str],
    release: Optional[str],
    module_slug: Optional[str],
) -> Dict[str, Any]:
    if not module_slug:
        raise ValueError("module_slug is required for MDD review creation")
    slug = safe_segment(module_slug, "module")
    manifest_path = _latest_mdd_manifest(product, release)
    manifest = _load_json(manifest_path)
    selected = None
    for entry in manifest.get("generated", []) or []:
        if safe_segment(entry.get("slug"), "module").lower() == slug.lower():
            selected = entry
            break
    if not selected:
        raise FileNotFoundError(f"No generated MDD found for module_slug: {module_slug}")
    plan_path = Path(selected.get("plan_path") or selected.get("path") or "")
    if not plan_path.is_file():
        raise FileNotFoundError(f"MDD plan JSON not found: {plan_path}")
    plan = _load_json(plan_path)
    markdown = plan.get("mdd_markdown")
    if not markdown:
        raise FileNotFoundError(f"MDD plan JSON does not contain mdd_markdown: {plan_path}")
    return {
        "markdown": markdown,
        "source_path": str(plan_path),
        "docx_path": selected.get("docx_path"),
        "metadata": {
            "manifest_path": str(manifest_path),
            "module": selected.get("module"),
            "slug": selected.get("slug"),
            "ticket": manifest.get("ticket"),
            "timestamp": manifest.get("timestamp"),
            "diagram_report": selected.get("diagram_report", {}),
            "mdd_quality_report": selected.get("mdd_quality_report", {}),
            "sections_included": selected.get("sections_included", []),
            "sections_skipped": selected.get("sections_skipped", []),
        },
    }


def load_hld_context(product: Optional[str], release: Optional[str]) -> Dict[str, Any]:
    """Return latest HLD, requirements, and code graph context for revision validation."""
    hld_path = _latest_hld_json(product, release)
    hld = _load_json(hld_path)
    context: Dict[str, Any] = {"hld": hld, "hld_path": str(hld_path)}
    requirements_path = hld.get("requirements_source")
    code_graph_path = hld.get("code_graph_source")
    if requirements_path and Path(requirements_path).is_file():
        context["requirements"] = _load_json(Path(requirements_path))
        if "requirements" in context["requirements"]:
            context["requirements"] = context["requirements"]["requirements"]
    if code_graph_path and Path(code_graph_path).is_file():
        context["code_graph"] = _load_json(Path(code_graph_path))
    return context


def load_mdd_context(review: Dict[str, Any]) -> Dict[str, Any]:
    version = review.get("current_version", "v1")
    current = next((v for v in review.get("versions", []) if v.get("version") == version), {})
    source = current.get("source_path")
    context: Dict[str, Any] = {"source_path": source}
    if source and Path(source).is_file():
        context["mdd_plan"] = _load_json(Path(source))
    context.update(load_hld_context(review.get("product"), review.get("release")))
    return context
