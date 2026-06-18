"""
build_index.py  —  one-time preprocessing step (B1).

Reads the large graph.json (NetworkX node-link format) once and writes two small,
fast-to-load index files that query.py uses on every run:

  nodes_index.json : norm_label -> list of {id, label, source_file}
                     ("what node(s) does this symbol name refer to?")

  links_index.json : node_id -> {outgoing: [...], incoming: [...]}
                     ("what connects to this node?", split by direction)

Run this ONCE after generating graph.json, and again only when graph.json changes.
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

# --- Configuration -----------------------------------------------------------
# Path to the graph produced by Graphify. Adjust if your layout differs.
# We point OUT of the orchestrator folder, into the monolith clone next door.
GRAPH_PATH = Path(__file__).resolve().parent.parent / "graph.json"

# Where to write the index files (here, in the orchestrator folder).
OUT_DIR = Path(__file__).parent / "index"


def _node_entry(node: dict) -> dict:
    """Preserve useful Graphify node metadata for downstream design docs."""
    passthrough_keys = [
        "id",
        "label",
        "norm_label",
        "source_file",
        "source_location",
        "file_type",
        "kind",
        "type",
        "signature",
        "parameters",
        "return_type",
        "line_start",
        "line_end",
        "parent",
        "container",
        "namespace",
        "note",
        "community",
    ]
    return {key: node.get(key, "") for key in passthrough_keys if key in node}


def build_index(graph_path: Path, out_dir: Path) -> bool:
    # 1. Sanity-check the input exists before we do anything expensive.
    if not graph_path.exists():
        print(f"ERROR: graph not found at {graph_path}")
        return False

    print(f"Loading graph from {graph_path} ...")
    # 2. The expensive step: parse the whole graph once.
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    # Support feature-spec graphs (seedSymbols) as well as full Graphify output.
    if graph.get("seedSymbols") and not graph.get("nodes"):
        from graph_normalizer import normalize_graph
        graph = normalize_graph(graph)
        print(f"  Normalized seedSymbols graph -> {len(graph.get('nodes', [])):,} synthetic nodes.")

    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    print(f"  {len(nodes):,} nodes, {len(links):,} links loaded.")

    # 3. Build the nodes index: norm_label -> [ {id, label, source_file}, ... ]
    #    A single norm_label can map to several nodes (e.g. many classes share a
    #    common method name), so each key holds a LIST.
    nodes_index = defaultdict(list)
    for n in nodes:
        norm = n.get("norm_label") or n.get("label", "").lower()
        nodes_index[norm].append(_node_entry(n))

    # 4. Build the links index: node_id -> {outgoing: [...], incoming: [...]}
    #    outgoing = this node is the SOURCE (things it uses / contains / calls)
    #    incoming = this node is the TARGET (things that use / contain / call it)
    links_index = defaultdict(lambda: {"outgoing": [], "incoming": []})
    for l in links:
        src = l.get("source")
        tgt = l.get("target")
        relation = l.get("relation")
        confidence = l.get("confidence_score", l.get("confidence"))

        # Record the edge on the source node's "outgoing" list...
        links_index[src]["outgoing"].append({
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": l.get("source_file", ""),
            "source_location": l.get("source_location", ""),
            "context": l.get("context", ""),
        })
        # ...and on the target node's "incoming" list.
        links_index[tgt]["incoming"].append({
            "source": src,
            "relation": relation,
            "confidence": confidence,
            "source_file": l.get("source_file", ""),
            "source_location": l.get("source_location", ""),
            "context": l.get("context", ""),
        })

    # 5. Write the two index files.
    out_dir.mkdir(exist_ok=True, parents=True)
    nodes_out = out_dir / "nodes_index.json"
    links_out = out_dir / "links_index.json"

    with open(nodes_out, "w", encoding="utf-8") as f:
        json.dump(nodes_index, f)
    with open(links_out, "w", encoding="utf-8") as f:
        json.dump(links_index, f)

    # 6. Report what we built.
    print(f"Wrote {nodes_out}  ({len(nodes_index):,} distinct symbol names)")
    print(f"Wrote {links_out}  ({len(links_index):,} nodes with relationships)")
    print("Done. query.py can now load these instead of the full graph.")
    return True


def main():
    build_index(GRAPH_PATH, OUT_DIR)


if __name__ == "__main__":
    main()