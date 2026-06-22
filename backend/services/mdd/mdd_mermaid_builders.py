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
    cls = resolve_class_name(sym, source_file)
    if not cls:
        return None
    cls = re.sub(r"\W", "_", cls)
    return cls if re.match(r"^[A-Za-z_]\w*$", cls) else None


def _safe_class_name(name: str) -> Optional[str]:
    if not name:
        return None
    cls = re.sub(r"\W", "_", name)
    return cls if re.match(r"^[A-Za-z_]\w*$", cls) else None


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


def _method_from_symbol(symbol: str) -> str:
    """Extract method name from `.Method()` or `Class.Method()` symbols."""
    symbol = (symbol or "").strip()
    if not symbol:
        return ""
    if symbol.startswith("."):
        return symbol.lstrip(".").replace("()", "")
    if "." in symbol and not symbol.startswith("."):
        tail = symbol.rsplit(".", 1)[-1].strip()
        if tail and re.match(r"^[A-Za-z_]\w*(?:\(\))?$", tail):
            return tail.replace("()", "")
    return ""


def _fallback_methods_for_mapping(cb: Dict[str, Any]) -> List[str]:
    """Generic classDiagram method fallback when code_graph methods[] is empty."""
    methods = cb.get("method_impacts") or cb.get("methods") or []
    if methods:
        labels: List[str] = []
        for method in methods[:5]:
            if isinstance(method, dict):
                raw = method.get("method_name") or method.get("display_name") or method.get("method") or ""
            else:
                raw = str(method)
            short = _short_method(raw)
            if short:
                labels.append(short)
        if labels:
            return labels

    symbol_method = cb.get("method_name") or _method_from_symbol(
        cb.get("codebase_symbol") or cb.get("raw_symbol") or cb.get("normalized_symbol", "")
    )
    if symbol_method:
        return [symbol_method[:40]]

    note = (cb.get("note") or "").lower()
    if "eligib" in note:
        return ["eligibility"]
    if cb.get("is_new_capability") or "new capability" in note:
        return ["mappedCapability"]

    return ["..."]


def build_architecture_flowchart(bundle: Dict[str, Any]) -> str:
    """§2.1 internal module structure — flowchart TD from mapped symbols."""
    mapping = bundle.get("mapping") or {}
    cbs = bundle.get("component_evidence") or mapping.get("codebase_mappings") or []
    if not cbs:
        return ""

    used: Set[str] = set()
    id_by_cls: Dict[str, str] = {}
    lines = ["flowchart LR"]

    for cb in cbs:
        sym = cb.get("codebase_symbol") or cb.get("raw_symbol") or cb.get("normalized_symbol", "")
        cls = _safe_class_name(cb.get("class_name", "")) or _class_symbol(sym, cb.get("source_file", ""))
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
    flows = bundle.get("use_cases") or bundle.get("filtered_flows") or []
    if not flows:
        return ""

    used: Set[str] = set()
    node_labels: Dict[str, str] = {}
    lines = ["flowchart LR"]

    def _node(comp: str) -> str:
        if comp not in node_labels:
            node_labels[comp] = _safe_id(comp.replace(" ", ""), used)
            lines.append(f"    {node_labels[comp]}[{_quote_label(comp)}]")
        return node_labels[comp]

    step_number = 1
    for flow in flows:
        prev_dst: Optional[str] = None
        for step in flow.get("steps") or flow.get("step_by_step_sequence") or []:
            src = step.get("source_component", "Source")
            dst = step.get("destination_component", "Destination")
            op_label = str(step.get("step_number") or step_number)

            src_id = _node(src)
            dst_id = _node(dst)
            from_id = prev_dst if prev_dst else src_id

            lines.append(f"    {from_id} -->|{_quote_label(op_label)}| {dst_id}")
            prev_dst = dst_id
            step_number += 1

    return "\n".join(lines) if len(lines) > 1 else ""


def build_use_case_flowcharts(bundle: Dict[str, Any]) -> List[Tuple[str, str]]:
    """One §3 flowchart per normalized use case."""
    diagrams: List[Tuple[str, str]] = []
    for index, flow in enumerate(bundle.get("use_cases") or [], start=1):
        steps = flow.get("steps") or []
        if not steps:
            continue
        used: Set[str] = set()
        node_labels: Dict[str, str] = {}
        lines = ["flowchart LR"]

        def _node(comp: str) -> str:
            label = comp or "Component"
            if label not in node_labels:
                node_labels[label] = _safe_id(label.replace(" ", ""), used)
                lines.append(f"    {node_labels[label]}[{_quote_label(label)}]")
            return node_labels[label]

        prev_dst: Optional[str] = None
        for step_number, step in enumerate(steps, start=1):
            src_id = _node(step.get("source_component", "Source"))
            dst_id = _node(step.get("destination_component", "Destination"))
            from_id = prev_dst if prev_dst else src_id
            label = str(step.get("step_number") or step_number)
            lines.append(f"    {from_id} -->|{_quote_label(label)}| {dst_id}")
            prev_dst = dst_id
        if len(lines) > 1:
            diagrams.append((flow.get("title") or f"Use Case {index}", "\n".join(lines)))
    return diagrams


def build_class_diagram(bundle: Dict[str, Any]) -> str:
    """§4 classDiagram from codebase_mappings (top classes, short methods)."""
    mapping = bundle.get("mapping") or {}
    cbs = bundle.get("component_evidence") or mapping.get("codebase_mappings") or []
    if not cbs:
        return ""

    lines = ["classDiagram"]
    declared: Set[str] = set()
    class_order: List[str] = []

    for cb in cbs[:6]:
        sym = cb.get("codebase_symbol") or cb.get("raw_symbol") or cb.get("normalized_symbol", "")
        src = cb.get("source_file", "")
        cls = _safe_class_name(cb.get("class_name", "")) or _class_symbol(sym, src)
        if not cls or cls in declared:
            continue
        declared.add(cls)
        class_order.append(cls)

        lines.append(f"    class {cls} {{")
        for method_name in _fallback_methods_for_mapping(cb):
            if method_name == "...":
                lines.append("        +...")
            else:
                lines.append(f"        +{method_name}()")
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

    # Add lightweight module-internal usage links so the class diagram is not isolated boxes.
    for i in range(len(class_order) - 1):
        lines.append(f"    {class_order[i]} ..> {class_order[i + 1]} : uses")

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
    flows = bundle.get("sequence_flows") or bundle.get("use_cases") or bundle.get("filtered_flows") or []
    if not flows:
        return ""

    used: Set[str] = set()
    participants_order: List[Tuple[str, str]] = []
    participants: Dict[str, str] = {}
    msg_lines: List[str] = []

    for flow in flows:
        for step in flow.get("steps") or flow.get("step_by_step_sequence") or []:
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


def build_sequence_diagrams(bundle: Dict[str, Any]) -> List[Tuple[str, str]]:
    """One §5 sequenceDiagram per normalized sequence flow."""
    diagrams: List[Tuple[str, str]] = []
    for index, flow in enumerate(bundle.get("sequence_flows") or bundle.get("use_cases") or [], start=1):
        steps = flow.get("steps") or []
        if not steps:
            continue
        used: Set[str] = set()
        participants_order: List[Tuple[str, str]] = []
        participants: Dict[str, str] = {}
        messages: List[str] = []
        for step in steps:
            src = step.get("source_component", "")
            dst = step.get("destination_component", "")
            op = (step.get("operation_signature") or "call")[:70].replace("\n", " ")
            for comp in (src, dst):
                if comp and comp not in participants:
                    pid, alias = _participant_id(comp, used)
                    participants[comp] = pid
                    participants_order.append((pid, alias.replace('"', "'")))
            if src in participants and dst in participants:
                messages.append(f"    {participants[src]}->>{participants[dst]}: {op}")
        if messages:
            lines = ["sequenceDiagram"]
            for pid, alias in participants_order:
                lines.append(f'    participant {pid} as "{alias}"')
            lines.extend(messages)
            diagrams.append((flow.get("title") or f"Sequence Flow {index}", "\n".join(lines)))
    return diagrams


def build_data_model_diagram(bundle: Dict[str, Any]) -> str:
    """§7 data model relationships from DTO/entity evidence."""
    models = bundle.get("data_models") or []
    if not models:
        return ""
    lines = ["classDiagram"]
    module_id = _safe_class_name(bundle.get("slug", "") or bundle.get("logical_name", "") or "Module") or "Module"
    lines.append(f"    class {module_id}")
    declared = {module_id}
    for model in models[:10]:
        name = _safe_class_name(model.get("name", ""))
        if not name or name in declared:
            continue
        declared.add(name)
        lines.append(f"    class {name}")
        lines.append(f"    {module_id} ..> {name} : uses")
    return "\n".join(lines) if len(lines) > 2 else ""


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

    if include.get("data_model_design"):
        d = build_data_model_diagram(bundle)
        if d:
            diagrams["data_model"] = d

    return diagrams
