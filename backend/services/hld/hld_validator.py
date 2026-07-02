"""
Post-generation HLD accuracy validator.

Checks that the generated HLD stays grounded in code_graph.json and does not
introduce pipeline tooling or filler after 'not specified' disclaimers.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set


_BACKTICK_SYMBOL_RE = re.compile(r"`([^`]+)`")
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

_PIPELINE_TERMS = frozenset({
    "redis", "kafka", "rabbitmq", "memcached",
})

_FILLER_PHRASES = (
    r"\btypical(?:ly)?\b",
    r"\bgenerally\b",
    r"\bcould be\b",
    r"\bwe will assume\b",
    r"\bfor example\b",
    r"\bfor instance\b",
)


def _diagram_metrics(block: str) -> Dict[str, int]:
    first_line = block.strip().splitlines()[0].strip() if block.strip() else ""
    if first_line.startswith("sequenceDiagram"):
        return {
            "participants": len(set(re.findall(r"^\s*participant\s+(\S+)", block, re.MULTILINE))),
            "messages": len(re.findall(r"^\s*\S+\s*->>\s*\S+\s*:", block, re.MULTILINE)),
        }
    if first_line.startswith("classDiagram"):
        return {
            "classes": len(set(re.findall(r"^\s*class\s+([A-Za-z_]\w*)", block, re.MULTILINE))),
            "relationships": len(re.findall(r"^\s*[A-Za-z_]\w*\s+(?:--|\.\.|<\|)", block, re.MULTILINE)),
        }
    if first_line.startswith("flowchart") or first_line.startswith("graph"):
        nodes: Set[str] = set()
        edges = 0
        for line in block.splitlines()[1:]:
            edge = re.search(r"^\s*([A-Za-z0-9_]+).*?(?:-->|-\.->|---|==>)", line)
            if edge:
                nodes.add(edge.group(1))
                edges += 1
                target = re.search(r"(?:-->|-\.->|---|==>)(?:\|.*?\|)?\s*([A-Za-z0-9_]+)", line)
                if target:
                    nodes.add(target.group(1))
                continue
            node = re.match(r"^\s*([A-Za-z0-9_]+)\s*(?:\[|\(|\{)", line)
            if node:
                nodes.add(node.group(1))
        return {"nodes": len(nodes), "edges": edges}
    return {}


def _collect_allowed_symbols(code_graph: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    for res in code_graph.get("seed_resolutions", []):
        node = res.get("node") or {}
        label = (node.get("label") or "").strip()
        if label:
            allowed.add(label)
            allowed.add(label.lstrip("."))
        name = res.get("name") or ""
        for part in re.split(r"[\s/]+", name):
            if len(part) > 3 and part[0].isupper():
                allowed.add(part.split("(")[0].strip())

    mapping = code_graph.get("mapping", {})
    for mod in mapping.get("mapped_modules", []):
        for cb in mod.get("codebase_mappings", []):
            sym = cb.get("codebase_symbol", "")
            if sym:
                allowed.add(sym)
                allowed.add(sym.lstrip("."))
        for api in mod.get("interfaces_and_apis", []):
            for acb in api.get("codebase_mappings", []):
                sym = acb.get("codebase_symbol", "")
                if sym:
                    allowed.add(sym)
                    allowed.add(sym.lstrip("."))

    for proj in code_graph.get("target_projects", []):
        allowed.add(proj)

    return allowed


def _looks_like_code_symbol(token: str) -> bool:
    token = token.strip()
    if not token or len(token) < 3:
        return False
    if token.startswith("http"):
        return False
    if token.lower() in {
        "not mapped in code graph",
        "not specified in source documentation",
        "n/a",
        "external",
    }:
        return False
    # C#-ish: PascalCase, dotted method, or Controller/Workflow suffix
    if re.match(r"^[A-Z][A-Za-z0-9]*(\.[A-Z][A-Za-z0-9]*)?$", token):
        return True
    if token.startswith(".") and token.endswith("()"):
        return True
    if any(token.endswith(s) for s in ("Controller", "Workflow", "Service", "Repository", "DTO")):
        return True
    return False


def validate_hld(
    hld_markdown: str,
    code_graph: Dict[str, Any],
    *,
    requirements: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a validation report for a generated HLD document."""
    allowed = _collect_allowed_symbols(code_graph)
    issues: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    # --- Symbol grounding in §2 ---
    in_logical = False
    for line in hld_markdown.splitlines():
        if re.match(r"^##\s+2\b", line):
            in_logical = True
        elif re.match(r"^##\s+[3-9]\b", line):
            in_logical = False
        if not in_logical:
            continue
        for match in _BACKTICK_SYMBOL_RE.finditer(line):
            sym = match.group(1).strip()
            if _looks_like_code_symbol(sym) and sym not in allowed:
                # Allow partial matches (e.g. MealController when MealController is in allowed)
                if not any(sym in a or a in sym for a in allowed):
                    warnings.append({
                        "type": "ungrounded_symbol",
                        "message": f"Symbol `{sym}` in Logical View not found in code_graph",
                    })

    # --- Pipeline tooling leak in §3-5 ---
    tail = hld_markdown
    m = re.search(r"^##\s+3\b", hld_markdown, re.MULTILINE)
    if m:
        tail = hld_markdown[m.start():]
    tail_lower = tail.lower()
    for term in _PIPELINE_TERMS:
        if term in tail_lower:
            # Only flag if not in requirements or contract
            req_text = ""
            if requirements:
                req_text = str(requirements.get("hld_content", {})).lower()
            contract_text = " ".join(code_graph.get("constraints", [])).lower()
            if term not in req_text and term not in contract_text:
                issues.append({
                    "type": "pipeline_term_leak",
                    "message": f"Pipeline/infrastructure term '{term}' appears in §3-5 but not in sources",
                })

    # --- Filler after 'not specified' ---
    for section_num in ("3", "4", "5"):
        sec_match = re.search(
            rf"^##\s+{section_num}\b.*?(?=^##\s+\d|\Z)",
            hld_markdown,
            re.MULTILINE | re.DOTALL,
        )
        if not sec_match:
            continue
        section = sec_match.group(0)
        if "not specified in source documentation" not in section.lower():
            continue
        for pat in _FILLER_PHRASES:
            if re.search(pat, section, re.IGNORECASE):
                warnings.append({
                    "type": "filler_after_not_specified",
                    "message": f"Section {section_num} may contain filler examples after 'not specified'",
                })
                break

    # --- Content quality gates for generated HLD usefulness ---
    expected_headings = (
        "Evidence and Confidence Summary",
        "Requirements Traceability",
    )
    for heading in expected_headings:
        if heading not in hld_markdown:
            warnings.append({
                "type": "missing_quality_section",
                "message": f"HLD is missing quality section: {heading}",
            })
    if code_graph.get("resolved_at_checkpoint_b") and "| Decision | Source | Design Impact |" not in hld_markdown:
        warnings.append({
            "type": "weak_decision_section",
            "message": "Architecture decisions are present but not rendered with source/impact detail",
        })
    if code_graph.get("acceptance_criteria") and "Mapped Code Symbol" not in hld_markdown:
        warnings.append({
            "type": "weak_traceability",
            "message": "Acceptance criteria are present but code traceability is missing",
        })

    # --- Mermaid semantic checks ---
    for idx, block in enumerate(_MERMAID_BLOCK_RE.findall(hld_markdown), start=1):
        first_line = block.strip().splitlines()[0].strip() if block.strip() else ""
        if first_line.startswith("flowchart") or first_line.startswith("graph"):
            if re.search(r"^\s*participant\b", block, re.MULTILINE | re.IGNORECASE):
                issues.append({
                    "type": "mermaid_semantic",
                    "message": f"Diagram {idx}: 'participant' used inside flowchart",
                })
        if first_line.startswith("sequenceDiagram"):
            declared = set(re.findall(r"^\s*participant\s+(\S+)", block, re.MULTILINE))
            for ln in block.splitlines()[1:]:
                ln = ln.strip()
                if not ln or ln.startswith("participant"):
                    continue
                arrow = re.match(r"^(\S+)\s*->>", ln)
                if arrow and ("-" in arrow.group(1) or " " in arrow.group(1)):
                    issues.append({
                        "type": "mermaid_semantic",
                        "message": f"Diagram {idx}: sequence participant '{arrow.group(1)}' has spaces/hyphens",
                    })
                if arrow and arrow.group(1) not in declared:
                    warnings.append({
                        "type": "mermaid_grounding",
                        "message": f"Diagram {idx}: sequence source participant '{arrow.group(1)}' is not declared",
                    })
                target = re.match(r"^\S+\s*->>\s*(\S+)\s*:", ln)
                if target and target.group(1) not in declared:
                    warnings.append({
                        "type": "mermaid_grounding",
                        "message": f"Diagram {idx}: sequence target participant '{target.group(1)}' is not declared",
                    })
        metrics = _diagram_metrics(block)
        if first_line.startswith("sequenceDiagram") and (
            metrics.get("participants", 0) < 2 or metrics.get("messages", 0) < 1
        ):
            warnings.append({
                "type": "shallow_diagram",
                "message": f"Diagram {idx}: sequence diagram is too shallow for HLD review",
            })
        if (first_line.startswith("flowchart") or first_line.startswith("graph")) and (
            metrics.get("nodes", 0) < 3 or metrics.get("edges", 0) < 2
        ):
            warnings.append({
                "type": "shallow_diagram",
                "message": f"Diagram {idx}: flowchart is too shallow for HLD review",
            })

    return {
        "valid": len(issues) == 0,
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues,
        "warnings": warnings,
        "allowed_symbol_count": len(allowed),
    }
