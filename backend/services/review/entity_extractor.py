"""Extract key entities from review target sections."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


_GENERIC_ENTITIES = {
    "High Level Design",
    "Module Detail Design",
    "The Food",
    "This Flow",
    "The Module",
}


def extract_section_entities(
    section_text: str,
    *,
    code_graph: Optional[Dict[str, Any]] = None,
    limit: int = 12,
) -> List[str]:
    """Extract important actors/services/modules from selected section text."""
    entities: List[str] = []
    for entity in _capitalized_entities(section_text):
        _append_unique(entities, entity)
    for entity in _known_code_entities(code_graph or {}):
        if entity.lower() in (section_text or "").lower():
            _append_unique(entities, entity)
    return [entity for entity in entities if entity not in _GENERIC_ENTITIES][:limit]


def _capitalized_entities(text: str) -> Iterable[str]:
    patterns = [
        r"\b(?:[A-Z][A-Za-z0-9]*(?:\s+|[-/])){1,5}(?:Module|Service|Device|Flow|API|CGM|User|Client|Server|Database|Gateway|Component)\b",
        r"\b(?:[A-Z]{2,}|[A-Z][a-z]+)(?:\s+(?:[A-Z]{2,}|[A-Z][a-z]+)){1,5}\b",
    ]
    seen = set()
    for pattern in patterns:
        for match in re.findall(pattern, text or ""):
            entity = re.sub(r"\s+", " ", match).strip(" .,:;")
            if len(entity) < 3 or entity.lower() in seen:
                continue
            seen.add(entity.lower())
            yield entity
    for simple in ("User", "Food Module", "CGM Connection Service", "Libre CGM"):
        if simple.lower() in (text or "").lower() and simple.lower() not in seen:
            yield simple


def _known_code_entities(code_graph: Dict[str, Any]) -> Iterable[str]:
    for key in ("classes", "functions", "methods", "apis", "endpoints", "target_projects"):
        value = code_graph.get(key) or []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("path") or item.get("endpoint")
            else:
                name = item
            if name:
                yield str(name)


def _append_unique(items: List[str], value: str) -> None:
    if value and value.lower() not in {item.lower() for item in items}:
        items.append(value)
