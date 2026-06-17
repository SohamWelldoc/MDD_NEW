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

# Use embedded Qdrant (no Docker required)
os.environ["QDRANT_URL"] = str(BACKEND_DIR.parent / "qdrant_data")
os.environ["ARTIFACT_DIR"] = str(BACKEND_DIR / "artifacts")

# Confluence base URL (page URL in .env is not valid for the API client)
os.environ["CONFLUENCE_URL"] = "https://welldoc.atlassian.net/wiki"

from models.schemas import IngestionRequest, IngestionStatus
from routes.ingestion import ingestion_jobs, run_ingestion
from services.codebase_analyzer import analyze_codebase
from services.hld_generator import generate_hld
from services.mdd_module_catalog import build_module_catalog
from services.mdd_generator import generate_mdd_for_modules
from services.requirements_generator import generate_requirements


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the HLD->optional MDD pipeline.")
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

    print("=" * 60)
    print("MDD_NEW HLD Pipeline")
    print(f"Artifacts -> {os.environ['ARTIFACT_DIR']}")
    print(f"Qdrant    -> {os.environ['QDRANT_URL']}")
    print("=" * 60)

    # --- Step 0: Ingest Confluence page into Qdrant ---
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
            username=os.environ["CONFLUENCE_USERNAME"],
            api_token=os.environ["CONFLUENCE_API_TOKEN"],
            page_id="5068259329",
            product="welldoc",
            clear_existing=True,
            clear_product_only=True,
        ),
    )
    status = ingestion_jobs[job_id]
    if status.status != "completed":
        raise RuntimeError(f"Ingestion failed: {status.error}")
    print(f"  OK — {status.chunks_created} chunks from {status.pages_processed} page(s)")

    # --- Step 1: Requirements ---
    print("\n[2/4] Generating requirements.json...")
    req = generate_requirements(product="welldoc", n_results=8)
    print(f"  OK — {req.artifact_path}")

    # --- Step 2: Codebase analysis (contract + monolith graph) ---
    contract_path = os.environ.get("CONTRACT_PATH", str(BACKEND_DIR.parent / "contract_AL-27103.json"))
    ticket = os.environ.get("TICKET", "AL-27103")
    print(f"\n[3/4] Analyzing contract ({contract_path}) against monolith graph...")
    code = analyze_codebase(contract_path=contract_path, ticket=ticket)
    print(f"  OK — {code.artifact_path}")

    # --- Step 3: HLD ---
    print("\n[4/4] Generating HLD.md...")
    hld = generate_hld()
    print(f"  OK — {hld.artifact_paths.get('hld')}")

    # --- Step 4: Optional MDD ---
    if args.mdd:
        print("\n[5/5] MDD module catalog...")
        catalog = build_module_catalog(artifact_dir=os.environ["ARTIFACT_DIR"])
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
                artifact_dir=os.environ["ARTIFACT_DIR"],
            )
            print(f"  OK — manifest at {gen.manifest_path}")

    print("\n" + "=" * 60)
    print("Pipeline complete. Artifacts:")
    artifact_dir = Path(os.environ["ARTIFACT_DIR"])
    for name in sorted(artifact_dir.iterdir()):
        print(f"  {name.name}  ({name.stat().st_size:,} bytes)")
    print("=" * 60)


if __name__ == "__main__":
    main()
