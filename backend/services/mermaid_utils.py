"""
Mermaid Utilities
=================

Post-processing for LLM-generated Mermaid diagrams. Ported (and trimmed)
from the proven sanitization pipeline used in the `MDD Generation_`
project. The HLD generator runs every emitted document through
`postprocess_mermaid()` before persisting it to disk.

Two surfaces are exposed:

  * sanitize_mermaid_diagrams(doc)  -> fixes common Mermaid v11 issues
  * validate_diagrams(doc)          -> returns a small report dict
"""

from __future__ import annotations

import re
from typing import Dict, List


_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
_VALID_DIAGRAM_TYPES = {
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "erDiagram", "stateDiagram", "gantt", "pie", "gitgraph",
    "journey", "mindmap", "timeline",
}


# ----------------------------------------------------------------------
# Sanitization
# ----------------------------------------------------------------------
def _close_unclosed_fences(doc: str) -> str:
    """If the LLM was truncated mid-diagram, close the dangling fence."""
    opens = len(re.findall(r"```mermaid", doc))
    closes = len(re.findall(r"```", doc)) - opens  # total fences minus opens = closes-ish
    if opens > 0 and (len(re.findall(r"```", doc)) % 2 == 1):
        doc = doc.rstrip() + "\n```\n"
    return doc


def _fix_block(block: str) -> str:
    lines = block.splitlines()

    # Fix 1: Remove rx:/ry: from classDef (invalid CSS in Mermaid v11)
    lines = [re.sub(r",?\s*r[xy]:\s*\d+", "", ln) for ln in lines]

    # Fix 2: Remove trailing `>` after arrow labels  -->|text|>  ->  -->|text|
    lines = [re.sub(r"((?:-->|-\.->|==>)\|[^|]*\|)>", r"\1", ln) for ln in lines]

    # Fix 3: flowchart dashed arrows sometimes come back as -.->>; normalize to -.->.
    lines = [re.sub(r"-\.->>", r"-.->", ln) for ln in lines]

    # Fix 6: graph TD -> flowchart TD (only on first non-blank line)
    for i, ln in enumerate(lines):
        if ln.strip():
            lines[i] = re.sub(r"^\s*graph\s+", "flowchart ", ln)
            break

    # Fix 7: strip trailing text after `end`
    lines = [re.sub(r"^(\s*end)\b.*$", r"\1", ln) for ln in lines]

    # Fix 8: quote unquoted subgraph titles containing spaces
    fixed: List[str] = []
    for ln in lines:
        m = re.match(r"^(\s*subgraph\s+)([^\"\n]+)$", ln)
        if m and " " in m.group(2).strip() and not m.group(2).strip().startswith('"'):
            fixed.append(f'{m.group(1)}"{m.group(2).strip()}"')
        else:
            fixed.append(ln)
    lines = fixed

    # Fix 9: capitalize reserved `end` inside node labels [end] / (end) / {end}
    lines = [re.sub(r"([\[\(\{])\s*end\s*([\]\)\}])", r"\1End\2", ln) for ln in lines]

    # Fix 10: drop flowchart edges targeting invalid bare `.Method()` node ids
    lines = [
        ln for ln in lines
        if not re.search(r"(-->|-\.->|==>|---)\s*\.[\w()]+\(\)", ln)
    ]

    # Fix 11: remove invalid classDiagram declarations (class names starting with `.`)
    lines = [ln for ln in lines if not re.match(r"\s*class\s+\.", ln)]

    # Fix 12: quote flowchart edge labels that contain parentheses but are unquoted
    fixed_edges: List[str] = []
    for ln in lines:
        m = re.match(r"^(\s*\S+\s*-->\|)([^\"|][^|]*\([^)]*\)[^|]*)(\|\s*\S+.*)$", ln)
        if m:
            label = m.group(2).strip().replace('"', "'")
            fixed_edges.append(f'{m.group(1)}"{label}"{m.group(3)}')
        else:
            fixed_edges.append(ln)
    lines = fixed_edges

    # Fix 13: sequenceDiagram messages do not use flowchart-style |"label"| syntax.
    fixed_sequence: List[str] = []
    in_sequence = any(ln.strip().startswith("sequenceDiagram") for ln in lines[:2])
    for ln in lines:
        if in_sequence and "->>" in ln and ":" in ln:
            prefix, msg = ln.split(":", 1)
            msg = re.sub(r'\|\s*["\']?([^|"\']+)["\']?\s*\|', r"\1", msg)
            msg = re.sub(r"\s+(?:-->|-\.->|==>|---).*$", "", msg).strip()
            msg = msg.strip('"').strip("'")
            fixed_sequence.append(f"{prefix}: {msg.strip()}")
        else:
            fixed_sequence.append(ln)
    lines = fixed_sequence

    return "\n".join(lines)


def sanitize_mermaid_block(block: str) -> str:
    """Sanitize a single Mermaid diagram body (no fences)."""
    if not block or not block.strip():
        return ""
    return _fix_block(block.strip())


def validate_mermaid_block(block: str) -> Dict[str, object]:
    """Structural validation for a single Mermaid diagram body (no fences)."""
    return _validate_block(block.strip() if block else "")


def is_valid_mermaid_block(block: str) -> bool:
    """Return True when a diagram body passes structural validation."""
    if not block or not block.strip():
        return False
    return bool(validate_mermaid_block(block).get("valid"))


def sanitize_mermaid_diagrams(doc: str) -> str:
    """Apply syntactic fixes to every mermaid fenced block in `doc`."""
    doc = _close_unclosed_fences(doc)

    def _replace(match: re.Match) -> str:
        inner = match.group(1)
        return f"```mermaid\n{_fix_block(inner).rstrip()}\n```"

    return _MERMAID_BLOCK_RE.sub(_replace, doc)


# ----------------------------------------------------------------------
# Validation (cheap structural checks — does NOT execute mermaid)
# ----------------------------------------------------------------------
def _strip_quoted(text: str) -> str:
    """Remove "..." quoted spans so paren/brace balance checks don't trip on labels."""
    return re.sub(r'"[^"\n]*"', "", text)


def _semantic_issues(block: str) -> List[str]:
    """Semantic checks beyond bracket balance (Mermaid v11 rules)."""
    issues: List[str] = []
    body = block.strip()
    if not body:
        return issues

    first_line = body.splitlines()[0].strip()
    is_flowchart = first_line.startswith("flowchart") or first_line.startswith("graph")
    is_sequence = first_line.startswith("sequenceDiagram")

    if is_flowchart and re.search(r"^\s*participant\b", body, re.MULTILINE | re.IGNORECASE):
        issues.append("flowchart contains sequenceDiagram keyword 'participant'")
    if is_flowchart and "->>" in body:
        issues.append("flowchart contains sequenceDiagram arrow '->>'")

    if is_sequence:
        for ln in body.splitlines()[1:]:
            ln = ln.strip()
            if not ln or ln.lower().startswith("participant"):
                continue
            m = re.match(r"^(\S+)\s*->>", ln)
            if m:
                pid = m.group(1)
                if " " in pid or "-" in pid:
                    issues.append(f"sequenceDiagram arrow uses invalid participant id '{pid}'")
            if "-->" in ln or "-.->" in ln or "==>" in ln:
                issues.append("sequenceDiagram message contains flowchart arrow syntax")

    return issues


def _validate_block(block: str) -> Dict[str, object]:
    issues: List[str] = []
    body = block.strip()

    if not body:
        return {"valid": False, "issues": ["empty block"]}

    first_token = body.split(None, 1)[0]
    if first_token not in _VALID_DIAGRAM_TYPES:
        issues.append(f"unknown diagram type '{first_token}'")

    stripped = _strip_quoted(body)
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        if stripped.count(opener) != stripped.count(closer):
            issues.append(f"unbalanced '{opener}{closer}'")

    issues.extend(_semantic_issues(body))

    return {"valid": not issues, "issues": issues}


def validate_diagrams(doc: str) -> Dict[str, object]:
    """Return a small structural validation report for all mermaid blocks."""
    blocks = _MERMAID_BLOCK_RE.findall(doc)
    reports = [_validate_block(b) for b in blocks]
    valid = sum(1 for r in reports if r["valid"])
    return {
        "total": len(reports),
        "valid": valid,
        "invalid": len(reports) - valid,
        "details": reports,
    }


def postprocess_mermaid(doc: str) -> str:
    """Convenience: sanitize and return the cleaned document."""
    return sanitize_mermaid_diagrams(doc)
