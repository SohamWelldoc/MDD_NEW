"""
Deterministic Mermaid diagram builders for MDD sections.

LLM-generated Mermaid often uses invalid node IDs (e.g. `.Method()` as bare nodes).
These builders produce valid syntax from the module context bundle.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple


def _safe_id(name: str, used: Set[str]) -> str:
    """Mermaid-safe identifier (letters, digits, underscore only)."""
    base = re.sub(r"[^\w]", "", (name or "Node").replace(".", ""))
    if not base or base[0].isdigit():
        base = f"N_{base}" if base else "Node"
    candidate = base[:40]
    n = 2
    while candidate in used:
        candidate = f"{base[:36]}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _quote_label(text: str) -> str:
    """Quote label for flowchart edge or node display."""
    t = (text or "").replace('"', "'").strip()
    return f'"{t}"'


def resolve_class_name(sym: str, source_file: str = "") -> Optional[str]:
    """Map a codebase_symbol (possibly method-only or composite) to a class name."""
    if not sym:
        return None
    if sym.startswith("."):
        cls = _class_from_file(source_file)
        return cls if cls else None
    if " " in sym or "+" in sym:
        first = sym.split("+")[0].strip().split()[0]
        if first and re.match(r"^\w+$", first):
            return first
        cls = _class_from_file(source_file)
        return cls if cls else None
    return sym.split(".")[0].strip() or None


def _class_symbol(sym: str, source_file: str) -> Optional[str]:
    """Return a valid classDiagram class name, or None to skip."""
    return resolve_class_name(sym, source_file)


def _class_from_file(source_file: str) -> str:
    if not source_file:
        return ""
    name = source_file.replace("\\", "/").split("/")[-1]
    return name[:-3] if name.endswith(".cs") else name


def _short_method(method: str) -> str:
    if not method:
        return ""
    short = method.split("_")[-1] if "_" in method else method
    return short.replace("()", "")[:40]


def build_architecture_flowchart(bundle: Dict[str, Any]) -> str:
    """§2.1 internal module structure — flowchart TD from mapped symbols."""
    mapping = bundle.get("mapping") or {}
    cbs = mapping.get("codebase_mappings") or []
    if not cbs:
        return ""

    used: Set[str] = set()
    id_by_cls: Dict[str, str] = {}
    lines = ["flowchart TD"]

    for cb in cbs:
        sym = cb.get("codebase_symbol", "")
        cls = _class_symbol(sym, cb.get("source_file", ""))
        if not cls or cls in id_by_cls:
            continue
        nid = _safe_id(cls, used)
        id_by_cls[cls] = nid
        lines.append(f"    {nid}[{_quote_label(cls)}]")

    cls_list = list(id_by_cls.keys())
    for i in range(len(cls_list) - 1):
        lines.append(f"    {id_by_cls[cls_list[i]]} --> {id_by_cls[cls_list[i + 1]]}")

    for dep in bundle.get("dependencies_in") or []:
        dep_name = (dep.get("dependency") or "").strip()
        if not dep_name:
            continue
        dep_id = _safe_id(dep_name.replace(" ", ""), used)
        lines.append(f"    {dep_id}[{_quote_label(dep_name)}]")
        if cls_list:
            lines.append(f"    {id_by_cls[cls_list[0]]} -.-> {dep_id}")

    return "\n".join(lines) if len(lines) > 1 else ""


def build_use_case_flowchart(bundle: Dict[str, Any]) -> str:
    """§3 use-case flow — flowchart TD from filtered flow steps."""
    flows = bundle.get("filtered_flows") or []
    if not flows:
        return ""

    used: Set[str] = set()
    node_labels: Dict[str, str] = {}
    lines = ["flowchart TD"]

    def _node(comp: str) -> str:
        if comp not in node_labels:
            node_labels[comp] = _safe_id(comp.replace(" ", ""), used)
            lines.append(f"    {node_labels[comp]}[{_quote_label(comp)}]")
        return node_labels[comp]

    for flow in flows:
        prev_dst: Optional[str] = None
        for step in flow.get("step_by_step_sequence") or []:
            src = step.get("source_component", "Source")
            dst = step.get("destination_component", "Destination")
            op = (step.get("operation_signature") or "")[:50]

            src_id = _node(src)
            dst_id = _node(dst)
            from_id = prev_dst if prev_dst else src_id

            if op:
                lines.append(f"    {from_id} -->|{_quote_label(op)}| {dst_id}")
            else:
                lines.append(f"    {from_id} --> {dst_id}")
            prev_dst = dst_id

    return "\n".join(lines) if len(lines) > 1 else ""


def build_class_diagram(bundle: Dict[str, Any]) -> str:
    """§4 classDiagram from codebase_mappings (top classes, short methods)."""
    mapping = bundle.get("mapping") or {}
    cbs = mapping.get("codebase_mappings") or []
    if not cbs:
        return ""

    lines = ["classDiagram"]
    declared: Set[str] = set()

    for cb in cbs[:6]:
        sym = cb.get("codebase_symbol", "")
        src = cb.get("source_file", "")
        cls = _class_symbol(sym, src)
        if not cls or cls in declared:
            continue
        declared.add(cls)

        lines.append(f"    class {cls} {{")
        methods = cb.get("methods") or []
        for m in methods[:5]:
            short = _short_method(m)
            if short:
                lines.append(f"        +{short}()")
        if not methods:
            lines.append("        +...")
        lines.append("    }")

        bases = cb.get("base_classes") or []
        for base in bases[:2]:
            bname = base.replace("base", "Base").title().replace("Base", "Base")
            if bname.lower() == "controllerbase":
                bname = "ControllerBase"
            elif bname.lower() == "workflowbase":
                bname = "WorkflowBase"
            if bname and re.match(r"^\w+$", bname):
                if bname not in declared:
                    lines.append(f"    class {bname}")
                    declared.add(bname)
                lines.append(f"    {cls} --|> {bname}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _participant_id(name: str, used: Set[str]) -> Tuple[str, str]:
    """Map component name to sequenceDiagram participant id + alias."""
    # Prefer PascalCase single token
    clean = name.strip()
    if "Controller" in clean or "Workflow" in clean or "Service" in clean:
        parts = clean.split()
        for p in parts:
            if re.match(r"^[A-Z]\w+$", p):
                return _safe_id(p, used), clean
    # Logical module name -> shortened
    short = re.sub(r"[^\w]", "", clean.replace(" ", ""))[:30]
    pid = _safe_id(short or "Module", used)
    return pid, clean


def build_sequence_diagram(bundle: Dict[str, Any]) -> str:
    """§5 sequenceDiagram from filtered flow steps."""
    flows = bundle.get("filtered_flows") or []
    if not flows:
        return ""

    used: Set[str] = set()
    participants_order: List[Tuple[str, str]] = []
    participants: Dict[str, str] = {}
    msg_lines: List[str] = []

    for flow in flows:
        for step in flow.get("step_by_step_sequence") or []:
            src = step.get("source_component", "")
            dst = step.get("destination_component", "")
            op = (step.get("operation_signature") or "call")[:60].replace("\n", " ")

            for comp in (src, dst):
                if comp and comp not in participants:
                    pid, alias = _participant_id(comp, used)
                    participants[comp] = pid
                    participants_order.append((pid, alias.replace('"', "'")))

            if src in participants and dst in participants:
                msg_lines.append(
                    f"    {participants[src]}->>{participants[dst]}: {op}"
                )

    if not msg_lines:
        return ""

    lines = ["sequenceDiagram"]
    for pid, alias in participants_order:
        lines.append(f'    participant {pid} as "{alias}"')
    lines.extend(msg_lines)
    return "\n".join(lines)


def build_mdd_diagrams(bundle: Dict[str, Any], include: Dict[str, Any]) -> Dict[str, str]:
    """Build all applicable diagrams deterministically."""
    diagrams: Dict[str, str] = {}

    if include.get("module_architecture"):
        d = build_architecture_flowchart(bundle)
        if d:
            diagrams["architecture"] = d

    if include.get("use_case_flow"):
        d = build_use_case_flowchart(bundle)
        if d:
            diagrams["use_case"] = d

    if include.get("component_design"):
        d = build_class_diagram(bundle)
        if d:
            diagrams["class"] = d

    if include.get("sequence_flow"):
        d = build_sequence_diagram(bundle)
        if d:
            diagrams["sequence"] = d
        else:
            hld_diagrams = bundle.get("sequence_diagrams") or []
            if hld_diagrams:
                diagrams["sequence"] = hld_diagrams[0].strip()

    return diagrams
