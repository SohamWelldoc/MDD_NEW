"""HLD-to-MDD cascade detection helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from services.mdd.mdd_module_catalog import load_module_catalog
from services.mdd.mdd_template import slugify_module_name
from services.review.review_store import list_reviews, read_version_markdown, save_review, utc_now


_MODULE_HEADING_RE = re.compile(
    r"^###\s+2\.(?P<number>\d+)\s+(?P<name>.+?)\s+Logical View\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_MERMAID_LINE_RE = re.compile(r"```mermaid\s*\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
_STOP_WORDS = {
    "",
    "api",
    "app",
    "component",
    "connection",
    "detail",
    "design",
    "flow",
    "module",
    "service",
    "system",
}


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value or "", flags=re.IGNORECASE).lower().strip()


def _tokens(value: str) -> Set[str]:
    return {token for token in _normalize_text(value).split() if token not in _STOP_WORDS and len(token) > 1}


def _module_aliases(module: Dict[str, Any]) -> Set[str]:
    names = {
        module.get("logical_name") or "",
        module.get("module") or "",
        module.get("slug") or "",
        module.get("id") or "",
    }
    aliases = {_normalize_text(name) for name in names if name}
    for name in names:
        aliases.update(_tokens(name))
    for symbol in module.get("primary_symbols", []) or []:
        tail = str(symbol).split(".")[-1].split("/")[-1]
        aliases.add(_normalize_text(tail))
        aliases.update(_tokens(tail))
    return {alias for alias in aliases if alias and alias not in _STOP_WORDS}


def _module_mentioned(text: str, module: Dict[str, Any]) -> bool:
    normalized = f" {_normalize_text(text)} "
    if not normalized.strip():
        return False
    for alias in _module_aliases(module):
        if len(alias) <= 2 and alias not in normalized.split():
            continue
        if f" {alias} " in normalized:
            return True
    return False


def _parse_hld_module_sections(markdown: str) -> Dict[str, Dict[str, str]]:
    matches = list(_MODULE_HEADING_RE.finditer(markdown or ""))
    sections: Dict[str, Dict[str, str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown or "")
        name = match.group("name").strip()
        slug = slugify_module_name(name)
        sections[slug] = {
            "name": name,
            "section": f"2.{match.group('number')}",
            "text": (markdown or "")[match.start():end],
        }
    return sections


def _changed_mermaid_text(diff: Dict[str, Any]) -> str:
    lines: List[str] = []
    for detail in diff.get("mermaid_change_details", []) or []:
        lines.extend(detail.get("added_lines", []) or [])
        lines.extend(detail.get("removed_lines", []) or [])
    if lines:
        return "\n".join(str(line) for line in lines)
    blocks: List[str] = []
    blocks.extend(diff.get("mermaid_after", []) or [])
    blocks.extend(diff.get("mermaid_before", []) or [])
    return "\n".join(blocks)


def _latest_draft_entry_for_version(hld_review: Dict[str, Any], hld_version: str) -> Optional[Dict[str, Any]]:
    accepted = next((v for v in hld_review.get("versions", []) if v.get("version") == hld_version), None)
    approved_from = (accepted or {}).get("approved_from") or f"{hld_version}_draft"
    return next((v for v in hld_review.get("versions", []) if v.get("version") == approved_from), None)


def _catalog_modules(product: str, release: str) -> List[Dict[str, Any]]:
    try:
        catalog = load_module_catalog(product=product, release=release)
        return catalog.get("modules", []) or []
    except Exception:  # noqa: BLE001
        return []


def _candidate_modules(product: str, release: str) -> List[Dict[str, Any]]:
    by_slug: Dict[str, Dict[str, Any]] = {}
    for module in _catalog_modules(product, release):
        slug = module.get("slug") or slugify_module_name(module.get("logical_name") or "")
        if slug:
            by_slug[slug] = {**module, "slug": slug}
    for review in list_reviews(product, release, document_type="mdd"):
        slug = review.get("module_slug")
        if slug and slug not in by_slug:
            by_slug[slug] = {
                "id": slug,
                "slug": slug,
                "logical_name": slug.replace("_", " "),
                "primary_symbols": [],
            }
    return list(by_slug.values())


def _impact_record(module: Dict[str, Any], confidence: str, reasons: List[str], hld_version: str) -> Dict[str, Any]:
    return {
        "module": module.get("logical_name") or module.get("module") or module.get("slug"),
        "slug": module.get("slug") or slugify_module_name(module.get("logical_name") or ""),
        "confidence": confidence,
        "reasons": sorted(set(reason for reason in reasons if reason)),
        "hld_version": hld_version,
    }


def predict_affected_mdd_modules(
    *,
    product: str,
    release: str,
    hld_review: Dict[str, Any],
    hld_version: str,
) -> List[Dict[str, Any]]:
    """Predict MDD module impact from the actual approved HLD draft diff."""
    modules = _candidate_modules(product, release)
    if not modules:
        return []

    draft_entry = _latest_draft_entry_for_version(hld_review, hld_version)
    diff = (draft_entry or {}).get("diff") or {}
    change_plan = (draft_entry or {}).get("change_plan") or {}
    feedback = next(
        (
            item
            for item in hld_review.get("feedback_items", [])
            if item.get("applied_in_version") == hld_version
            or item.get("draft_version") == (draft_entry or {}).get("version")
        ),
        {},
    )

    try:
        old_markdown = read_version_markdown(hld_review, (draft_entry or {}).get("base_version") or "v1")
    except Exception:  # noqa: BLE001
        old_markdown = ""
    try:
        new_markdown = read_version_markdown(hld_review, (draft_entry or {}).get("version") or hld_version)
    except Exception:  # noqa: BLE001
        try:
            new_markdown = read_version_markdown(hld_review, hld_version)
        except Exception:  # noqa: BLE001
            new_markdown = ""

    old_sections = _parse_hld_module_sections(old_markdown)
    new_sections = _parse_hld_module_sections(new_markdown)
    mermaid_delta = _changed_mermaid_text(diff)
    signal = "\n".join(
        str(part)
        for part in [
            feedback.get("feedback"),
            feedback.get("target_section"),
            change_plan.get("target_section"),
            change_plan.get("target_scope"),
            " ".join(change_plan.get("extracted_entities", []) or []),
            " ".join(diff.get("changed_sections", []) or []),
            mermaid_delta,
        ]
        if part
    )
    document_wide = (
        change_plan.get("target_scope") == "document"
        or feedback.get("target_kind") == "full_document"
        or diff.get("target_section") in {None, "", "document"}
    )

    impacts: List[Dict[str, Any]] = []
    for module in modules:
        slug = module.get("slug") or slugify_module_name(module.get("logical_name") or "")
        reasons: List[str] = []
        confidence = ""

        old_section = old_sections.get(slug, {}).get("text", "")
        new_section = new_sections.get(slug, {}).get("text", "")
        if old_section != new_section and (old_section or new_section):
            reasons.append(f"HLD logical section changed for {module.get('logical_name') or slug}.")
            confidence = "high"

        if mermaid_delta and _module_mentioned(mermaid_delta, module):
            reasons.append("Changed Mermaid diagram lines reference this module.")
            confidence = "high"

        if signal and _module_mentioned(signal, module):
            reasons.append("Reviewer feedback/change plan references this module.")
            confidence = confidence or "medium"

        if reasons:
            impacts.append(_impact_record(module, confidence or "medium", reasons, hld_version))

    if not impacts and document_wide:
        return [
            _impact_record(
                module,
                "low",
                ["HLD change is document-wide; module impact should be rechecked."],
                hld_version,
            )
            for module in modules
        ]

    return impacts


def affected_mdd_reviews(
    *,
    product: str,
    release: str,
    hld_review: Dict[str, Any],
    hld_version: str,
) -> List[Dict[str, Any]]:
    impacts = predict_affected_mdd_modules(
        product=product,
        release=release,
        hld_review=hld_review,
        hld_version=hld_version,
    )
    impact_by_slug = {impact.get("slug"): impact for impact in impacts}
    all_mdd = list_reviews(product, release, document_type="mdd")
    affected = []
    for review in all_mdd:
        impact = impact_by_slug.get(review.get("module_slug"))
        if impact:
            affected.append({
                **review,
                "cascade_reason": "; ".join(impact.get("reasons", [])),
                "cascade_confidence": impact.get("confidence"),
                "cascade_impact": impact,
            })
    return affected


def mark_affected_mdd_stale(product: str, release: str, hld_review: Dict[str, Any], hld_version: str, actor: str) -> Dict[str, Any]:
    marked = []
    affected_modules = predict_affected_mdd_modules(
        product=product,
        release=release,
        hld_review=hld_review,
        hld_version=hld_version,
    )
    impact_by_slug = {impact.get("slug"): impact for impact in affected_modules}
    for review in affected_mdd_reviews(product=product, release=release, hld_review=hld_review, hld_version=hld_version):
        review["stale"] = True
        review["status"] = "stale_due_to_hld_change" if review.get("status") in {"approved", "finalized"} else review.get("status", "in_review")
        review["stale_reason"] = review.get("cascade_reason") or f"HLD changed to {hld_version}"
        review["affected_by"] = [hld_version]
        review["cascade_confidence"] = review.get("cascade_confidence")
        review["cascade_impact"] = review.get("cascade_impact")
        review.setdefault("audit", []).append({
            "event": "marked_stale_due_to_hld_change",
            "hld_version": hld_version,
            "actor": actor,
            "reason": review["stale_reason"],
            "confidence": review.get("cascade_confidence"),
            "at": utc_now(),
        })
        save_review(review)
        marked.append(review["review_id"])
        impact = impact_by_slug.get(review.get("module_slug"))
        if impact is not None:
            impact.setdefault("review_ids", []).append(review["review_id"])
    return {
        "review_ids": marked,
        "modules": affected_modules,
    }


def latest_hld_module_impacts(product: str, release: str) -> Dict[str, Dict[str, Any]]:
    """Return the latest stored HLD approval impact by MDD module slug."""
    for review in list_reviews(product, release, document_type="hld"):
        for event in reversed(review.get("audit", []) or []):
            impacts = event.get("affected_modules") or []
            if event.get("event") == "draft_approved" and impacts:
                return {impact.get("slug"): impact for impact in impacts if impact.get("slug")}
    return {}
