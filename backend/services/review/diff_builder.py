"""Diff helpers for review draft previews."""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple


_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def extract_section(markdown: str, heading: Optional[str]) -> Tuple[str, str]:
    if not heading:
        return "document", markdown
    escaped = re.escape(heading.strip())
    pattern = re.compile(
        rf"^(?P<hashes>#+)\s+.*{escaped}.*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(markdown)
    if not match:
        simple = re.compile(rf"^#+\s+{escaped}\s*$", re.IGNORECASE | re.MULTILINE)
        match = simple.search(markdown)
    if not match:
        return "document", markdown
    level = len(match.group("hashes"))
    next_heading = re.compile(rf"^#{{1,{level}}}\s+", re.MULTILINE)
    next_match = next_heading.search(markdown, match.end())
    end = next_match.start() if next_match else len(markdown)
    return markdown[match.start():end], markdown[match.start():end]


def _headings(markdown: str) -> List[str]:
    return re.findall(r"^#{1,6}\s+(.+)$", markdown or "", flags=re.MULTILINE)


def _changed_sections(old_markdown: str, new_markdown: str, target_section: Optional[str]) -> List[str]:
    if target_section:
        return [target_section]
    old_heads = set(_headings(old_markdown))
    new_heads = set(_headings(new_markdown))
    changed = sorted(old_heads ^ new_heads)
    return changed[:20] or ["document"]


def _mermaid_changes(old_focus: str, new_focus: str) -> Dict[str, Any]:
    before = _MERMAID_BLOCK_RE.findall(old_focus or "")
    after = _MERMAID_BLOCK_RE.findall(new_focus or "")
    changed = before != after
    details = _mermaid_change_details(before, after)
    return {
        "changed": changed,
        "before": before,
        "after": after,
        "before_count": len(before),
        "after_count": len(after),
        "details": details,
    }


def _mermaid_change_details(before: List[str], after: List[str]) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    max_count = max(len(before), len(after))
    for index in range(max_count):
        old = before[index] if index < len(before) else ""
        new = after[index] if index < len(after) else ""
        if old == new:
            continue
        old_lines = _meaningful_mermaid_lines(old)
        new_lines = _meaningful_mermaid_lines(new)
        added = sorted(new_lines - old_lines)
        removed = sorted(old_lines - new_lines)
        details.append({
            "index": index + 1,
            "status": "added" if not old else "removed" if not new else "modified",
            "before_line_count": len(old_lines),
            "after_line_count": len(new_lines),
            "added_lines": added[:20],
            "removed_lines": removed[:20],
            "added_line_count": len(added),
            "removed_line_count": len(removed),
        })
    return details


def _meaningful_mermaid_lines(block: str) -> set[str]:
    return {
        re.sub(r"\s+", " ", line.strip())
        for line in (block or "").splitlines()
        if line.strip() and not line.strip().startswith("%%")
    }


def build_diff(
    old_markdown: str,
    new_markdown: str,
    *,
    target_section: Optional[str] = None,
) -> Dict[str, Any]:
    _label, old_focus = extract_section(old_markdown, target_section)
    _label, new_focus = extract_section(new_markdown, target_section)
    old_lines = old_focus.splitlines()
    new_lines = new_focus.splitlines()
    unified = "\n".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="current",
            tofile="draft",
            lineterm="",
        )
    )
    mermaid = _mermaid_changes(old_focus, new_focus)
    changed_sections = _changed_sections(old_markdown, new_markdown, target_section)
    summary = []
    if old_markdown != new_markdown:
        summary.append(f"Changed scope: {', '.join(changed_sections[:5])}")
    if mermaid["changed"]:
        if mermaid["before_count"] != mermaid["after_count"]:
            summary.append(f"Mermaid diagram count changed: {mermaid['before_count']} -> {mermaid['after_count']}")
        for detail in mermaid["details"]:
            if detail["status"] == "modified":
                summary.append(
                    "Mermaid diagram "
                    f"{detail['index']} modified: +{detail['added_line_count']} line(s), "
                    f"-{detail['removed_line_count']} line(s)"
                )
            elif detail["status"] == "added":
                summary.append(f"Mermaid diagram {detail['index']} added with {detail['after_line_count']} line(s)")
            elif detail["status"] == "removed":
                summary.append(f"Mermaid diagram {detail['index']} removed")
    if not summary:
        summary.append("No text changes detected.")
    return {
        "target_section": target_section,
        "changed_sections": changed_sections,
        "change_summary": summary,
        "old_snippet": old_focus[:12000],
        "new_snippet": new_focus[:12000],
        "mermaid_before": mermaid["before"],
        "mermaid_after": mermaid["after"],
        "mermaid_change_details": mermaid["details"],
        "mermaid_changed": mermaid["changed"],
        "unified_diff": unified,
        "changed": old_markdown != new_markdown,
    }
