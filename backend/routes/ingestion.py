"""Ingestion endpoints for Confluence data using JSONL vector storage."""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from datetime import datetime
import uuid
import os
import re
from typing import Dict
from urllib.parse import urlparse
from dotenv import load_dotenv

from models.schemas import IngestionRequest, IngestionStatus, IngestionResponse
from services.confluence.confluence import create_confluence_client, fetch_all_pages
from services.confluence.preprocessor import ConfluencePreprocessor
from services.artifact_store.db import get_embedding_model, get_vector_store
from services.confluence.chunker import SmartChunker
from services.artifact_store.artifact_paths import artifact_context, safe_segment, write_json

load_dotenv()

router = APIRouter()

# In-memory dict for ingestion job status tracking
ingestion_jobs: Dict[str, IngestionStatus] = {}

SPACE_TO_PRODUCT_MAPPING = {
    "als": "als",
    "rav": "cvs",
    "cvs": "cvs",
    "lillyhealth": "lillyhealth",
    "lh": "lillyhealth",
    "lilly": "lillyhealth",
    "welldoc": "welldoc",
    "bluestar": "welldoc",
    "blues": "welldoc",
    "bscc": "welldoc"
}


def _extract_page_id_from_url(page_url: str = None) -> str:
    if not page_url:
        return ""
    parsed = urlparse(page_url)
    query_match = re.search(r"(?:pageId|page_id)=([0-9]+)", parsed.query)
    if query_match:
        return query_match.group(1)
    path_match = re.search(r"/pages/([0-9]+)", parsed.path)
    return path_match.group(1) if path_match else ""


def _infer_product(request: IngestionRequest, pages: list) -> str:
    if request.product:
        return safe_segment(request.product.lower(), "default")
    if request.space_key:
        mapped = SPACE_TO_PRODUCT_MAPPING.get(request.space_key.lower().strip())
        if mapped:
            return mapped
    for page in pages:
        space_key = (page.get("space") or {}).get("key", "")
        mapped = SPACE_TO_PRODUCT_MAPPING.get(space_key.lower().strip())
        if mapped:
            return mapped
    return "default"


def _infer_release(request: IngestionRequest, pages: list, page_id: str = None) -> str:
    if request.release:
        return safe_segment(request.release, "default")
    for source in (os.getenv("RELEASE"), os.getenv("TICKET")):
        if source:
            return safe_segment(source, "default")

    text_parts = []
    for page in pages:
        text_parts.append(page.get("title", ""))
        text_parts.extend([ancestor.get("title", "") for ancestor in page.get("ancestors", [])])
        labels = page.get("metadata", {}).get("labels", {}).get("results", [])
        text_parts.extend([label.get("name", "") for label in labels])
    joined = " ".join(part for part in text_parts if part)
    release_match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", joined)
    if release_match:
        return safe_segment(f"{release_match.group(1)}_release", "default")
    return safe_segment(page_id or request.page_id or "default", "default")


def run_ingestion(job_id: str, request: IngestionRequest):
    """Background task to run ingestion"""
    global ingestion_jobs
    
    try:
        # Get job status
        job_status = ingestion_jobs.get(job_id)
        if not job_status:
            return
        
        # Update status
        job_status.status = "processing"
        job_status.message = "Validating inputs..."
        ingestion_jobs[job_id] = job_status
        
        # Additional runtime validation
        if not request.confluence_url.startswith(('http://', 'https://')):
            raise ValueError("Invalid Confluence URL protocol")
        
        if len(request.api_token) < 10:
            raise ValueError("API token appears to be invalid")
        
        job_status.message = "Connecting to Confluence..."
        ingestion_jobs[job_id] = job_status
        
        # Create Confluence client
        confluence = create_confluence_client(
            request.confluence_url,
            request.username,
            request.api_token
        )
        
        job_status.message = "Fetching pages..."
        ingestion_jobs[job_id] = job_status
        
        page_id = request.page_id or _extract_page_id_from_url(request.confluence_page_url)

        # Fetch pages
        pages = fetch_all_pages(
            confluence,
            space_key=request.space_key,
            page_id=page_id
        )
        
        if not pages:
            job_status.status = "failed"
            job_status.error = "No pages found"
            ingestion_jobs[job_id] = job_status
            return

        product = _infer_product(request, pages)
        release = _infer_release(request, pages, page_id)
        context = artifact_context(product=product, release=release, create=True)
        confluence_dir = context.stage_dir("confluence")
        os.environ["ARTIFACT_DIR"] = str(context.root_dir)
        os.environ["PROJECT"] = product
        os.environ["PRODUCT"] = product
        os.environ["RELEASE"] = release
        os.environ["ARTIFACT_TIMESTAMP"] = context.timestamp

        job_status.product = product
        job_status.release = release
        job_status.artifact_root = str(context.root_dir)
        ingestion_jobs[job_id] = job_status
        
        # Progress: 10% - Pages fetched
        job_status.progress = 10
        job_status.message = f"Processing {len(pages)} pages..."
        ingestion_jobs[job_id] = job_status
        
        # Process pages
        preprocessor = ConfluencePreprocessor(
            confluence_base_url=request.confluence_url,
            preserve_links=True,
            preserve_images=True,
            extract_tables=True,
            extract_code_blocks=True,
            confluence_client=confluence  # Pass client to fetch user display names
        )
        
        processed_pages = preprocessor.process_pages_batch(pages)

        confluence_metadata = {
            "product": product,
            "release": release,
            "timestamp": context.timestamp,
            "confluence_url": request.confluence_url,
            "confluence_page_url": request.confluence_page_url,
            "page_id": page_id,
            "space_key": request.space_key,
            "pages_processed": len(processed_pages),
            "pages": [
                {
                    "page_id": page.get("page_id"),
                    "title": page.get("title"),
                    "space_key": page.get("space_key"),
                    "space_name": page.get("space_name"),
                    "url": page.get("url"),
                    "version": page.get("version"),
                    "metadata": page.get("metadata", {}),
                }
                for page in processed_pages
            ],
            "created_at": datetime.now().isoformat(),
        }
        confluence_json_path = confluence_dir / f"confluence_{context.timestamp}.json"
        write_json(confluence_json_path, confluence_metadata)
        
        # Progress: 20% - Pages processed
        job_status.progress = 20
        job_status.pages_processed = len(processed_pages)
        job_status.message = f"Processed {len(processed_pages)} pages, chunking..."
        ingestion_jobs[job_id] = job_status
        
        # Chunk documents using SmartChunker
        chunk_size = request.chunk_size or int(os.getenv("CHUNK_SIZE", "1500"))
        chunk_overlap = request.chunk_overlap or int(os.getenv("CHUNK_OVERLAP", "200"))
        chunker = SmartChunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        
        chunks, stats = chunker.chunk_documents(processed_pages)
        
        # Progress: 30% - Chunking done
        job_status.progress = 30
        job_status.chunks_created = len(chunks)
        job_status.message = f"Created {len(chunks)} chunks, preparing for storage..."
        ingestion_jobs[job_id] = job_status
        
        # Store chunks and embeddings in JSONL under project/release/confluence.
        vector_store = get_vector_store(project=product, release=release, timestamp=context.timestamp)
        embedding_model = get_embedding_model()

        if request.clear_existing:
            vector_store.clear()
            job_status.message = f"Cleared existing JSON vector files for release '{release}', storing new chunks..."
            print(f"[Ingestion] Cleared JSON vector files at {confluence_dir}")

        # Prepare data for JSON vector storage.
        documents = [chunk['text'] for chunk in chunks]
        
        # Progress: 40% - Start embedding generation with batch progress
        total_chunks = len(documents)
        encode_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
        log_interval = int(os.getenv("EMBEDDING_LOG_INTERVAL", "100"))
        all_embeddings = []
        
        print(f"Generating embeddings for {total_chunks} chunks (batch_size={encode_batch_size})...")
        
        for batch_start in range(0, total_chunks, log_interval):
            batch_end = min(batch_start + log_interval, total_chunks)
            batch_docs = documents[batch_start:batch_end]
            
            # Calculate progress: 40% to 70% range for embedding phase
            batch_progress = 40 + int((batch_end / total_chunks) * 30)
            percent_done = int((batch_end / total_chunks) * 100)
            
            job_status.progress = batch_progress
            job_status.message = f"Embedding {batch_end}/{total_chunks} ({percent_done}%)..."
            print(f"[EMBEDDING] Processing {batch_end}/{total_chunks} ({percent_done}%) - Progress: {batch_progress}%")
            ingestion_jobs[job_id] = job_status
            
            # Encode this batch
            batch_embeddings = embedding_model.encode(
                batch_docs,
                show_progress_bar=False,
                batch_size=encode_batch_size,
                normalize_embeddings=True
            )
            all_embeddings.extend(batch_embeddings)
        
        embeddings = all_embeddings
        print(f"[EMBEDDING] Complete! Generated {len(embeddings)} embeddings")
        
        # Progress: 70% - Embeddings done, preparing JSON write
        job_status.progress = 70
        job_status.message = f"Preparing {len(chunks)} vectors for JSON storage..."
        ingestion_jobs[job_id] = job_status

        print(f"Writing {len(chunks)} chunks and embeddings to JSONL at {confluence_dir}...")

        # Progress: 80% - Writing JSON files
        job_status.progress = 80
        job_status.message = f"Writing {len(chunks)} vectors to JSONL..."
        ingestion_jobs[job_id] = job_status

        manifest = vector_store.write(
            chunks=chunks,
            embeddings=embeddings,
            product=product,
            release=release,
            pages_processed=len(processed_pages),
            model_name=os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5"),
        )
        manifest["confluence_metadata_path"] = str(confluence_json_path)

        # Complete
        job_status.status = "completed"
        job_status.progress = 100
        job_status.message = (
            f"Successfully ingested {len(chunks)} chunks from {len(processed_pages)} pages "
            f"for product '{product}' into {manifest['artifact_dir']}"
        )
        job_status.completed_at = datetime.now().isoformat()
        ingestion_jobs[job_id] = job_status

        print(f"[OK] Ingestion complete: {len(chunks)} chunks from {len(processed_pages)} pages for product '{product}'")
        print(f"   JSON vector artifacts: {manifest['artifact_dir']}")

        # Clear query cache if enabled
        try:
            from services.artifact_store.db import get_retriever
            retriever = get_retriever()
            if hasattr(retriever, 'clear_cache'):
                retriever.clear_cache()
        except Exception as e:
            print(f"Note: Could not clear cache: {e}")
        
    except Exception as e:
        job_status = ingestion_jobs.get(job_id)
        if job_status:
            job_status.status = "failed"
            job_status.error = str(e)
            job_status.completed_at = datetime.now().isoformat()
            ingestion_jobs[job_id] = job_status


@router.post("/start", response_model=IngestionResponse)
async def start_ingestion(request: IngestionRequest, background_tasks: BackgroundTasks):
    """Start a background ingestion job"""
    job_id = str(uuid.uuid4())
    
    # Initialize job status
    job_status = IngestionStatus(
        job_id=job_id,
        status="pending",
        progress=0,
        pages_processed=0,
        chunks_created=0,
        started_at=datetime.now().isoformat()
    )
    
    ingestion_jobs[job_id] = job_status
    
    # Start background task
    background_tasks.add_task(run_ingestion, job_id, request)
    
    return IngestionResponse(
        job_id=job_id,
        status="started",
        message="Ingestion job started successfully"
    )


@router.get("/status/{job_id}", response_model=IngestionStatus)
async def get_ingestion_status(job_id: str):
    """Get status of an ingestion job"""
    job_status = ingestion_jobs.get(job_id)
    
    if not job_status:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job_status


@router.delete("/products/{product}")
async def delete_product(product: str):
    """Delete product data from the active JSON vector store."""
    try:
        product = product.lower().strip()
        store = get_vector_store(project=product, create=False)
        points_to_delete = store.delete_product(product)

        # Clear query cache after deletion
        try:
            from services.artifact_store.db import get_retriever
            retriever = get_retriever()
            if hasattr(retriever, 'clear_cache'):
                retriever.clear_cache()
        except Exception as e:
            print(f"Note: Could not clear cache: {e}")
            
        return {
            "success": True,
            "message": f"Successfully deleted data for product '{product}'",
            "deleted_count": points_to_delete
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
