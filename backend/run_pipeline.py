"""One-shot local pipeline runner: ingest -> requirements -> codebase -> HLD."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Ensure backend is on path and cwd is backend/
BACKEND_DIR = Path(__file__).resolve().parent
os.chdir(BACKEND_DIR)
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR.parent / ".env")

# File-backed RAG artifacts live under artifacts/<project>/<release>/<timestamp>.
os.environ.setdefault("ARTIFACT_BASE_DIR", str(BACKEND_DIR / "artifacts"))
os.environ.setdefault("PROJECT", "welldoc")
os.environ.setdefault("TICKET", "AL-27103")
os.environ.setdefault("RELEASE", os.environ["TICKET"])
os.environ.setdefault(
    "ARTIFACT_TIMESTAMP",
    datetime.now().replace(microsecond=0).isoformat().replace(":", "-"),
)

# Confluence base URL (page URL in .env is not valid for the API client)
os.environ["CONFLUENCE_URL"] = "https://welldoc.atlassian.net/wiki"

from services.artifact_store.artifact_paths import artifact_context, latest_matching


def _latest_required(stage_dir: Path, pattern: str, label: str) -> Path:
    path = latest_matching(stage_dir, pattern)
    if not path:
        raise RuntimeError(f"No reusable {label} found in {stage_dir} matching {pattern}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the HLD->optional MDD pipeline.")
    parser.add_argument("--product", default=os.environ.get("PROJECT", "welldoc"))
    parser.add_argument("--release", default=os.environ.get("RELEASE") or os.environ.get("TICKET", "AL-27103"))
    parser.add_argument("--page-id", default=os.environ.get("CONFLUENCE_PAGE_ID", "5068259329"))
    parser.add_argument("--confluence-page-url", default=os.environ.get("CONFLUENCE_PAGE_URL"))
    parser.add_argument("--contract-path", default=os.environ.get("CONTRACT_PATH"))
    parser.add_argument("--graph-path", default=os.environ.get("GRAPH_PATH"))
    parser.add_argument(
        "--reuse-ingestion",
        action="store_true",
        help="Reuse latest Confluence chunks/embeddings instead of fetching and embedding again.",
    )
    parser.add_argument(
        "--reuse-requirements",
        action="store_true",
        help="Reuse latest requirements_<timestamp>.json instead of generating requirements again.",
    )
    parser.add_argument(
        "--reuse-codebase",
        action="store_true",
        help="Reuse latest code_graph_<timestamp>.json instead of analyzing graph.json again.",
    )
    parser.add_argument(
        "--hld-only",
        action="store_true",
        help="Shortcut for --reuse-ingestion --reuse-requirements --reuse-codebase.",
    )
    parser.add_argument(
        "--requirements-only",
        action="store_true",
        help="Run only requirements generation, reusing latest ingestion.",
    )
    parser.add_argument(
        "--codebase-only",
        action="store_true",
        help="Run only codebase analysis, reusing latest requirements.",
    )
    parser.add_argument(
        "--mdd",
        action="store_true",
        help="Build MDD module catalog after HLD generation. If --mdd-modules is provided, also generate MDDs.",
    )
    parser.add_argument(
        "--mdd-modules",
        default=None,
        help="Comma-separated logical module names (from /api/mdd/modules) to generate. Requires --mdd.",
    )
    args = parser.parse_args()
    if args.hld_only:
        args.reuse_ingestion = True
        args.reuse_requirements = True
        args.reuse_codebase = True
    if args.requirements_only:
        args.reuse_ingestion = True
    if args.codebase_only:
        args.reuse_ingestion = True
        args.reuse_requirements = True

    context = artifact_context(product=args.product, release=args.release, create=True)
    os.environ["PROJECT"] = context.product
    os.environ["PRODUCT"] = context.product
    os.environ["RELEASE"] = context.release
    os.environ["ARTIFACT_TIMESTAMP"] = context.timestamp
    os.environ["ARTIFACT_DIR"] = str(context.root_dir)

    print("=" * 60)
    print("MDD_NEW HLD Pipeline")
    print(f"Artifact base -> {os.environ['ARTIFACT_BASE_DIR']}")
    print(f"Project       -> {context.product}")
    print(f"Release       -> {context.release}")
    print(f"Run timestamp -> {os.environ['ARTIFACT_TIMESTAMP']}")
    print("=" * 60)

    confluence_dir = context.stage_dir("confluence")
    hld_dir = context.stage_dir("hld")
    codebase_dir = context.stage_dir("codebase")

    # --- Step 0: Ingest Confluence page into JSONL vector store ---
    if args.reuse_ingestion:
        chunks_path = _latest_required(confluence_dir, "chunks_*.jsonl", "Confluence chunks")
        embeddings_path = _latest_required(confluence_dir, "embeddings_*.jsonl", "Confluence embeddings")
        print("\n[1/4] Reusing Confluence ingestion artifacts...")
        print(f"  OK — chunks: {chunks_path}")
        print(f"  OK — embeddings: {embeddings_path}")
    else:
        from models.schemas import IngestionRequest, IngestionStatus
        from routes.ingestion import ingestion_jobs, run_ingestion

        print("\n[1/4] Ingesting Confluence page...")
        job_id = str(uuid.uuid4())
        ingestion_jobs[job_id] = IngestionStatus(
            job_id=job_id,
            status="pending",
            progress=0,
            pages_processed=0,
            chunks_created=0,
            started_at=datetime.now().isoformat(),
        )
        run_ingestion(
            job_id,
            IngestionRequest(
                confluence_url=os.environ["CONFLUENCE_URL"],
                confluence_page_url=args.confluence_page_url,
                username=os.environ["CONFLUENCE_USERNAME"],
                api_token=os.environ["CONFLUENCE_API_TOKEN"],
                page_id=args.page_id,
                product=context.product,
                release=context.release,
                clear_existing=True,
                clear_product_only=True,
            ),
        )
        status = ingestion_jobs[job_id]
        if status.status != "completed":
            raise RuntimeError(f"Ingestion failed: {status.error}")
        print(f"  OK — {status.chunks_created} chunks from {status.pages_processed} page(s)")

    # --- Step 1: Requirements ---
    if args.reuse_requirements:
        req_path = _latest_required(hld_dir, "requirements_*.json", "requirements")
        print("\n[2/4] Reusing requirements artifact...")
        print(f"  OK — {req_path}")
    else:
        from services.requirements.requirements_generator import generate_requirements

        print("\n[2/4] Generating timestamped requirements artifacts...")
        req = generate_requirements(product=context.product, release=context.release, n_results=8)
        req_path = Path(req.artifact_path)
        print(f"  OK — {req_path}")

    if args.requirements_only:
        print("\nRequirements-only run complete.")
        return

    # --- Step 2: Codebase analysis (contract + monolith graph) ---
    contract_path = args.contract_path or str(BACKEND_DIR.parent / "contract_AL-27103.json")
    ticket = os.environ.get("TICKET", "AL-27103")
    if args.reuse_codebase:
        code_path = _latest_required(codebase_dir, "code_graph_*.json", "code graph")
        contract_snapshot_path = _latest_required(codebase_dir, "contract_*.json", "contract snapshot")
        print("\n[3/4] Reusing codebase analysis artifact...")
        print(f"  OK — code graph: {code_path}")
        print(f"  OK — contract: {contract_snapshot_path}")
    else:
        from services.codebase.codebase_analyzer import analyze_codebase

        print(f"\n[3/4] Analyzing contract ({contract_path}) against monolith graph...")
        code = analyze_codebase(
            product=context.product,
            release=context.release,
            contract_path=contract_path,
            ticket=ticket,
            graph_path=args.graph_path,
        )
        code_path = Path(code.artifact_path)
        print(f"  OK — {code_path}")

    if args.codebase_only:
        print("\nCodebase-only run complete.")
        return

    # --- Step 3: HLD ---
    from services.hld.hld_generator import generate_hld

    print("\n[4/4] Generating HLD DOCX/JSON artifacts...")
    hld = generate_hld(product=context.product, release=context.release)
    print(f"  OK — DOCX: {hld.artifact_paths.get('docx')}")
    print(f"  OK — JSON: {hld.artifact_paths.get('hld_json')}")

    # --- Step 4: Optional MDD ---
    if args.mdd:
        from services.mdd.mdd_module_catalog import build_module_catalog
        from services.mdd.mdd_generator import generate_mdd_for_modules

        print("\n[5/5] MDD module catalog...")
        catalog = build_module_catalog(product=context.product, release=context.release)
        print(f"  OK — {catalog.artifact_path}")

        if args.mdd_modules:
            ticket = os.environ.get("TICKET", "AL-27103")
            selected = [m.strip() for m in args.mdd_modules.split(",") if m.strip()]
            if not selected:
                raise RuntimeError("--mdd-modules was provided but resolved to an empty list.")

            print("\n[6/6] Generating MDDs...")
            gen = generate_mdd_for_modules(
                selected_modules=selected,
                ticket=ticket,
                product=context.product,
                release=context.release,
            )
            print(f"  OK — manifest at {gen.manifest_path}")

    print("\n" + "=" * 60)
    print("Pipeline complete. Artifacts:")
    artifact_dir = context.root_dir
    for name in sorted(artifact_dir.rglob("*")):
        if name.is_file():
            print(f"  {name.relative_to(artifact_dir)}  ({name.stat().st_size:,} bytes)")
    print("=" * 60)


if __name__ == "__main__":
    main()
