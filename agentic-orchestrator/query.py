"""
query.py  —  the query-architecture adapter (B1 query side).

Loads the two small index files built by build_index.py and answers:
  "What does this symbol connect to?"  (its typed, confidence-scored neighbors)

Usage:
  python query.py AppleLoginController            # human-readable text
  python query.py AppleLoginController --json      # machine-readable JSON (for agents)

This is the ONE function the whole adapter is built around. Fancier queries
(find_callers, find_implementations, ...) are just filtered views of this walk.
The agent never reads graph.json itself — it calls this tool, which does.
"""

import json
import sys
from pathlib import Path

INDEX_DIR = Path(__file__).parent / "index"


def load_indexes():
    """Load the two small precomputed indexes (fast — not the full graph)."""
    with open(INDEX_DIR / "nodes_index.json", "r", encoding="utf-8") as f:
        nodes_index = json.load(f)
    with open(INDEX_DIR / "links_index.json", "r", encoding="utf-8") as f:
        links_index = json.load(f)
    return nodes_index, links_index


def canonical(name):
    """
    Reduce a symbol name to a comparable core, so a human's 'ExternalLogin'
    matches Graphify's decorated norm_label '.externallogin()'.
    Strips surrounding decoration, lowercases, and for a qualified query like
    'AppleLoginController.ExternalLogin' keeps only the final segment.
    """
    n = name.strip().lower()
    n = n.replace("()", "")     # drop call parens anywhere
    n = n.strip(".")            # drop leading/trailing dots
    if "." in n:                # qualified name -> keep final segment
        n = n.split(".")[-1]
    return n


def build_id_index(nodes_index):
    """Map node id -> node dict for direct graphId lookups from feature contracts."""
    id_index = {}
    for nodes in nodes_index.values():
        for n in nodes:
            nid = n.get("id")
            if nid:
                id_index[nid] = n
    return id_index


def resolve_node_by_id(node_id, nodes_index):
    """
    Resolve a Graphify node by its exact id (contract seedSymbols.graphId).
    Returns (chosen, alternatives): always at most one exact match.
    """
    if not node_id:
        return None, []
    id_index = build_id_index(nodes_index)
    node = id_index.get(node_id)
    if node:
        return node, []
    return None, []


def resolve_symbol(name, nodes_index):
    """
    Turn a human symbol name into node candidate(s).

    A name often maps to MANY nodes (e.g. several classes share a method name).
    We prefer real-code nodes (non-empty source_file) over external/framework
    stubs (empty source_file, like ASP.NET's 'ApiController').
    Returns (chosen, alternatives): the best candidate plus any others, so the
    caller can disambiguate if needed.

    Lookup strategy:
      1. Exact match on the raw norm_label key.
      2. Canonical match: compare canonical(query) against canonical(each key),
         so 'ExternalLogin' finds '.externallogin()'.
    """
    # 1. Exact key match (fast path — classes like 'applelogincontroller').
    candidates = nodes_index.get(name.lower(), [])

    # 2. Canonical fallback (methods like '.externallogin()').
    if not candidates:
        target = canonical(name)
        for key, nodes in nodes_index.items():
            if canonical(key) == target:
                candidates.extend(nodes)

    if not candidates:
        return None, []

    # Prefer nodes that are actually defined in our code.
    real = [c for c in candidates if c.get("source_file")]
    pool = real if real else candidates
    chosen = pool[0]
    alternatives = [c for c in candidates if c["id"] != chosen["id"]]
    return chosen, alternatives


def group_by_relation(entries, peer_key):
    """
    Group a list of link entries by their 'relation' type.
    peer_key is 'target' for outgoing links, 'source' for incoming.
    Returns: { relation_type: [ {peer_id, confidence}, ... ] }
    """
    grouped = {}
    for e in entries:
        rel = e.get("relation", "unknown")
        grouped.setdefault(rel, []).append({
            "peer": e.get(peer_key),
            "confidence": e.get("confidence"),
        })
    return grouped


def find_neighbors(name):
    """Core query: resolve the symbol, then return its grouped in/out neighbors."""
    nodes_index, links_index = load_indexes()
    chosen, alternatives = resolve_symbol(name, nodes_index)

    if chosen is None:
        return {"query": name, "found": False, "alternatives": []}

    relationships = links_index.get(chosen["id"], {"outgoing": [], "incoming": []})

    return {
        "query": name,
        "found": True,
        "node": chosen,                      # {id, label, source_file}
        "alternatives": alternatives,        # other nodes sharing this name
        "uses": group_by_relation(relationships["outgoing"], "target"),    # outgoing
        "used_by": group_by_relation(relationships["incoming"], "source"), # incoming
    }


def print_human(result):
    """Readable output for a human validating the adapter by hand."""
    if not result["found"]:
        print(f"No node found for '{result['query']}'.")
        return

    n = result["node"]
    print(f"\n=== {n['label']}  ({n['id']}) ===")
    print(f"defined in: {n['source_file'] or '(external / framework stub)'}")

    if result["alternatives"]:
        print(f"note: {len(result['alternatives'])} other node(s) share this name "
              f"(showing the best match; use the id to target a specific one).")

    print("\n-- USES (outgoing: what this symbol depends on) --")
    _print_groups(result["uses"])

    print("\n-- USED BY (incoming: what depends on this symbol) --")
    _print_groups(result["used_by"])
    print()


def _print_groups(groups):
    if not groups:
        print("   (none)")
        return
    # Show the most useful relation types first.
    order = ["implements", "inherits", "calls", "references", "contains", "imports", "method"]
    for rel in sorted(groups, key=lambda r: (order.index(r) if r in order else 99, r)):
        peers = groups[rel]
        print(f"   {rel} ({len(peers)}):")
        for p in peers[:15]:   # cap per group so output stays readable
            conf = p["confidence"]
            conf_str = f"  [conf {conf}]" if conf is not None else ""
            print(f"      -> {p['peer']}{conf_str}")
        if len(peers) > 15:
            print(f"      ... and {len(peers) - 15} more")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv[1:]

    if not args:
        print("Usage: python query.py <SymbolName> [--json]")
        sys.exit(1)

    result = find_neighbors(args[0])

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)


if __name__ == "__main__":
    main()