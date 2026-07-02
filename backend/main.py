"""
FastAPI Main Application
"""

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from routes import ingestion, requirements, codebase, hld, mdd, demo, reviews

app = FastAPI(
    title="HLD Generation Pipeline API",
    description=(
        "Generates a High-Level Design document by combining (1) requirements "
        "extracted from a Confluence corpus and (2) a code analysis of the "
        "target codebase. Built on top of the proven Confluence RAG stack."
    ),
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize connections on startup"""
    print("[Starting] Starting HLD Generation Pipeline API...")
    # Connections are lazy-loaded, nothing to do here
    print("[Ready] API ready")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown"""
    print("[Stopping] Shutting down HLD Generation Pipeline API...")
    
    # Close vector-store handles
    from services.artifact_store.db import close_vector_store
    try:
        close_vector_store()
    except Exception as e:
        print(f"Warning: Error during shutdown cleanup: {e}")
    
    print("[Done] Shutdown complete")


# Include routers
health_router = APIRouter()

@health_router.get("/")
async def health_check():
    return {"status": "healthy"}

app.include_router(health_router, prefix="/api/health", tags=["health"])
app.include_router(ingestion.router, prefix="/api/ingestion", tags=["ingestion"])
app.include_router(requirements.router, prefix="/api/requirements", tags=["requirements"])
app.include_router(codebase.router, prefix="/api/codebase", tags=["codebase"])
app.include_router(hld.router, prefix="/api/hld", tags=["hld"])
app.include_router(mdd.router, prefix="/api/mdd", tags=["mdd"])
app.include_router(demo.router, prefix="/api/demo", tags=["demo"])
app.include_router(reviews.router, prefix="/api/reviews", tags=["reviews"])

# Serve frontend (optional - kept for future UI)
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path) and os.path.exists(os.path.join(frontend_path, "index.html")):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/")
    async def root():
        return FileResponse(os.path.join(frontend_path, "index.html"))
else:
    @app.get("/")
    async def root():
        return {
            "service": "HLD Generation Pipeline API",
            "docs": "/docs",
            "pipeline": [
                "POST /api/ingestion/start    (Confluence -> product/release JSONL artifacts)",
                "POST /api/requirements/generate  (Confluence RAG -> hld/requirements_<timestamp>.json)",
                "POST /api/codebase/analyze   (contract + monolith graph -> codebase/code_graph_<timestamp>.json)",
                "POST /api/hld/generate       (requirements + code_graph -> hld/HLD_<timestamp>.md/.docx)",
                "POST /api/hld/run            (end-to-end orchestrator)",
                "GET  /api/hld/latest         (raw HLD markdown)",
                "GET  /api/mdd/modules       (module catalog for multi-select)",
                "POST /api/mdd/generate     (generate one MDD per selected module)",
                "GET  /api/mdd/manifest      (last MDD generation metadata)",
                "POST /api/reviews/create    (start HLD/MDD human review session)",
            ],
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        log_level="info",
        timeout_keep_alive=75
    )
