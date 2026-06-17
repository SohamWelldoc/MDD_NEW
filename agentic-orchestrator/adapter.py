"""
adapter.py  —  the query-architecture adapter interface.

This is THE engine-agnostic question layer. The decomposer calls these named
functions; they never touch graph.json directly.
Today the functions are answered by query.py's graph walk; in v2 the same
function signatures can be re-implemented against Roslyn with nothing else
changing.

Each function is a thin, filtered view over find_neighbors:
  find_base_class            -> outgoing 'inherits'
  find_implemented_interfaces-> outgoing 'implements'
  find_implementations       -> incoming 'implements'   (who implements this interface)
  find_callers               -> incoming 'calls'        (who calls this method)
  find_callees               -> outgoing 'calls'        (what this method calls)
  find_methods               -> outgoing 'method'       (a class's methods)

Every answer carries the confidence score (from the graph) and an
'ambiguous' flag when the name matched more than one node — so the caller
always knows how trustworthy and how specific the result is.

CLI (for humans testing by hand; mirrors the functions exactly):
  python adapter.py callers   ExternalLogin
  python adapter.py callees    ExternalLogin
  python adapter.py base       AppleLoginController
  python adapter.py implements IExternalLoginService
  python adapter.py interfaces AppleLoginController
  python adapter.py methods    AppleLoginController
  add --json for machine-readable output.
"""

import json
import sys

# Reuse the proven resolve + walk from query.py (single source of truth).
from query import find_neighbors


def _answer(name, direction, relation):
    """
    Shared shape for every named question.
    direction: 'uses' (outgoing) or 'used_by' (incoming).
    relation:  the relation key to filter on (e.g. 'calls', 'inherits').
    Returns a uniform result dict the decomposer can rely on.
    """
    result = find_neighbors(name)
    if not result["found"]:
        return {"query": name, "found": False, "question": f"{direction}:{relation}",
                "results": [], "ambiguous": False}

    bucket = result[direction].get(relation, [])
    return {
        "query": name,
        "found": True,
        "question": f"{direction}:{relation}",
        "node": result["node"],
        "ambiguous": len(result["alternatives"]) > 0,   # name matched >1 node
        "alternatives": result["alternatives"],
        "results": bucket,            # [ {peer, confidence}, ... ]
    }


# --- The named questions (the adapter interface) -----------------------------

def find_base_class(name):
    """What class does this inherit from? (outgoing 'inherits')"""
    return _answer(name, "uses", "inherits")

def find_implemented_interfaces(name):
    """What interfaces does this class implement? (outgoing 'implements')"""
    return _answer(name, "uses", "implements")

def find_implementations(name):
    """What classes implement this interface? (incoming 'implements')"""
    return _answer(name, "used_by", "implements")

def find_callers(name):
    """Who calls this method? (incoming 'calls') — query a METHOD name."""
    return _answer(name, "used_by", "calls")

def find_callees(name):
    """What does this method call? (outgoing 'calls') — query a METHOD name."""
    return _answer(name, "uses", "calls")

def find_methods(name):
    """What methods does this class contain? (outgoing 'method')"""
    return _answer(name, "uses", "method")


# --- Thin CLI wrapper (parsing + printing only; no logic) --------------------

_COMMANDS = {
    "base": find_base_class,
    "interfaces": find_implemented_interfaces,
    "implements": find_implementations,
    "callers": find_callers,
    "callees": find_callees,
    "methods": find_methods,
}


def _print_human(r):
    if not r["found"]:
        print(f"No node found for '{r['query']}'.")
        return
    n = r["node"]
    print(f"\n=== {n['label']} ===  ({r['question']})")
    print(f"defined in: {n['source_file'] or '(external / framework stub)'}")
    if r["ambiguous"]:
        print(f"! ambiguous: {len(r['alternatives'])} other node(s) share this name "
              f"— result may not be the one you meant.")
    if not r["results"]:
        print("   (no matches for this question)")
    else:
        for item in r["results"]:
            conf = item.get("confidence")
            conf_str = f"  [conf {conf}]" if conf is not None else ""
            print(f"   -> {item['peer']}{conf_str}")
    print()


def main():
    parts = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv[1:]

    if len(parts) < 2 or parts[0] not in _COMMANDS:
        print("Usage: python adapter.py <command> <SymbolName> [--json]")
        print("commands:", ", ".join(_COMMANDS))
        sys.exit(1)

    command, name = parts[0], parts[1]
    result = _COMMANDS[command](name)

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)


if __name__ == "__main__":
    main()