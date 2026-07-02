"""Map reviewer feedback to the most relevant markdown section."""

from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional

from services.shared.llm_client import get_llm_client


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "that", "the", "this", "to", "with", "add", "change",
    "update", "improve", "make", "more", "better", "details", "section",
}


def extract_sections(markdown: str) -> List[Dict[str, Any]]:
    """Return markdown heading sections with their text bounds."""
    matches = list(re.finditer(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$", markdown or "", flags=re.MULTILINE))
    sections: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        level = len(match.group("hashes"))
        end = len(markdown)
        for next_match in matches[index + 1:]:
            if len(next_match.group("hashes")) <= level:
                end = next_match.start()
                break
        sections.append({
            "heading": match.group("title").strip(),
            "level": level,
            "start": match.start(),
            "end": end,
            "text": markdown[match.start():end].strip(),
        })
    return sections


def detect_feedback_section(
    *,
    markdown: str,
    feedback: str,
    change_type: Optional[str] = None,
    target_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Suggest the best target section for feedback using deterministic scoring with LLM fallback."""
    if target_kind == "full_document":
        return {"target_section": None, "confidence": 0.0, "reason": "Full document target requested.", "router_source": "deterministic"}

    sections = extract_sections(markdown)
    if not sections:
        return {"target_section": None, "confidence": 0.0, "reason": "No markdown headings found.", "router_source": "deterministic"}

    best = _deterministic_section_match(
        sections=sections,
        feedback=feedback,
        change_type=change_type,
        target_kind=target_kind,
    )
    if best and best["confidence"] >= 0.55:
        return best
    llm_result = _llm_section_match(sections=sections, feedback=feedback, change_type=change_type, target_kind=target_kind)
    if llm_result.get("target_section"):
        return llm_result
    if best:
        return best
    return {"target_section": None, "confidence": 0.0, "reason": "No confident section match found.", "router_source": "deterministic"}


def _deterministic_section_match(
    *,
    sections: List[Dict[str, Any]],
    feedback: str,
    change_type: Optional[str],
    target_kind: Optional[str],
) -> Optional[Dict[str, Any]]:
    feedback_tokens = _tokens(feedback)
    feedback_lower = (feedback or "").lower()
    best: Optional[Dict[str, Any]] = None
    for section in sections:
        heading_lower = section["heading"].lower()
        section_lower = section["text"].lower()
        heading_tokens = _tokens(section["heading"])
        body_tokens = _tokens(section["text"][:3000])

        score = 0
        score += len(feedback_tokens & heading_tokens) * 6
        score += len(feedback_tokens & body_tokens) * 2

        score += _semantic_bonus(feedback_lower, heading_lower, section_lower, change_type, target_kind)
        if heading_lower in feedback_lower:
            score += 10
        if section["level"] == 1 and len(sections) > 1 and heading_lower not in feedback_lower:
            score -= 12

        candidate = {
            "target_section": section["heading"],
            "confidence": min(1.0, score / 18),
            "score": score,
            "reason": "Matched reviewer feedback keywords and section content.",
            "router_source": "deterministic",
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    if not best or best["score"] <= 0:
        return None
    return best


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_/-]{2,}", text or "")
    return {word.lower() for word in words if word.lower() not in _STOP_WORDS}


def _semantic_bonus(feedback: str, heading: str, section: str, change_type: Optional[str], target_kind: Optional[str]) -> int:
    bonus = 0
    semantic_groups = [
        (("diagram", "mermaid", "flow", "visual", "lifecycle", "steps", "interaction", "interactions"), ("diagram", "flow", "flows", "interaction", "interactions", "lifecycle", "architecture", "sequence", "steps")),
        (("security", "auth", "authentication", "authorization", "token", "encrypt"), ("security", "auth", "authentication", "authorization")),
        (("performance", "scalability", "latency", "throughput", "load"), ("performance", "scalability", "non-functional", "nfr")),
        (("api", "endpoint", "interface", "contract"), ("api", "interface", "endpoint", "integration")),
        (("database", "schema", "table", "storage"), ("data", "database", "storage", "persistence")),
        (("requirement", "traceability", "acceptance"), ("traceability", "requirement", "acceptance")),
        (("deployment", "infra", "server", "cluster"), ("deployment", "infrastructure", "runtime")),
    ]
    haystack = f"{heading} {section[:1000]}"
    for feedback_terms, section_terms in semantic_groups:
        if any(term in feedback for term in feedback_terms) and any(term in haystack for term in section_terms):
            bonus += 8
    if change_type == "diagram" and "```mermaid" in section:
        bonus += 8
    if (change_type == "diagram" or target_kind == "diagram" or "diagram" in feedback) and _looks_like_flow_section(heading, section):
        bonus += 12
    if any(word in feedback for word in ("these steps", "steps", "interaction", "interactions")) and _looks_like_flow_section(heading, section):
        bonus += 12
    return bonus


def _looks_like_flow_section(heading: str, section: str) -> bool:
    text = f"{heading} {section}".lower()
    flow_terms = ("interaction", "interactions", "flow", "flows", "sequence", "step-by-step", "triggered", "sends", "retrieves", "displays")
    numbered_steps = len(re.findall(r"(?:^|\s)\d+\.\s+", section or "")) >= 2
    return numbered_steps or any(term in text for term in flow_terms)


def _llm_section_match(
    *,
    sections: List[Dict[str, Any]],
    feedback: str,
    change_type: Optional[str],
    target_kind: Optional[str],
) -> Dict[str, Any]:
    try:
        choices = [
            {
                "heading": section["heading"],
                "level": section["level"],
                "snippet": re.sub(r"\s+", " ", section["text"])[:700],
            }
            for section in sections[:40]
        ]
        prompt = {
            "task": "Choose the single best markdown section for reviewer feedback.",
            "feedback": feedback,
            "change_type": change_type,
            "target_kind": target_kind,
            "sections": choices,
            "return_json_schema": {
                "target_section": "exact heading text or null",
                "confidence": "0.0 to 1.0",
                "reason": "short reason",
            },
        }
        raw = get_llm_client().chat(
            "You route review feedback to the best document section. Return JSON only.",
            json.dumps(prompt, ensure_ascii=False)[:18000],
            temperature=0.0,
            max_tokens=500,
        )
        parsed = json.loads(_strip_json(raw))
        target = parsed.get("target_section")
        valid_headings = {section["heading"] for section in sections}
        if target not in valid_headings:
            return {"target_section": None, "confidence": 0.0, "reason": "LLM router did not return a valid heading.", "router_source": "llm"}
        return {
            "target_section": target,
            "confidence": float(parsed.get("confidence") or 0.0),
            "reason": parsed.get("reason") or "Selected by LLM section router.",
            "router_source": "llm",
        }
    except Exception:
        return {"target_section": None, "confidence": 0.0, "reason": "LLM section router unavailable.", "router_source": "llm"}


def _strip_json(raw: str) -> str:
    text = (raw or "").strip()
    fenced = re.match(r"^```(?:json)?\s*\n(.*?)```$", text, re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text
