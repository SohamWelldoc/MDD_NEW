"""Ingestion endpoints for Confluence data - Uses Qdrant"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from datetime import datetime
import uuid
import os
from typing import Dict, List
from dotenv import load_dotenv
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

from models.schemas import IngestionRequest, IngestionStatus, IngestionResponse
from services.confluence import create_confluence_client, fetch_all_pages
from services.preprocessor import ConfluencePreprocessor
from services.db import get_qdrant_collection, get_embedding_model, get_collection_name
from services.chunker import SmartChunker

load_dotenv()

router = APIRouter()

# In-memory dict for ingestion job status tracking
ingestion_jobs: Dict[str, IngestionStatus] = {}

SPACE_TO_PRODUCT_MAPPING = {
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


def run_ingestion(job_id: str, request: IngestionRequest):
    """Background task to run ingestion"""
    global ingestion_jobs
    
    try:
        # Get job status
        job_status = ingestion_jobs.get(job_id)
        if not job_status:
            return
        
        # Determine product identifier
        product = request.product
        if not product and request.space_key:
            # Auto-detect product from space key
            product = SPACE_TO_PRODUCT_MAPPING.get(request.space_key.lower().strip(), "default")
        if not product:
            product = "default"
        
        product = product.lower().strip()
        
        # Update status
        job_status.status = "processing"
        job_status.message = "Validating inputs..."
        job_status.product = product
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
        
        # Fetch pages
        pages = fetch_all_pages(
            confluence,
            space_key=request.space_key,
            page_id=request.page_id
        )
        
        if not pages:
            job_status.status = "failed"
            job_status.error = "No pages found"
            ingestion_jobs[job_id] = job_status
            return
        
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
        
        # Store in Qdrant
        client, collection_name = get_qdrant_collection()
        embedding_model = get_embedding_model()
        
        # Clear existing if requested
        if request.clear_existing:
            try:
                if request.clear_product_only and product:
                    # Clear only data for this specific product (using payload filter)
                    client.delete(
                        collection_name=collection_name,
                        points_selector=Filter(
                            must=[FieldCondition(key="product", match=MatchValue(value=product))]
                        )
                    )
                    job_status.message = f"Cleared existing data for product '{product}', storing new chunks..."
                    print(f"[Ingestion] Cleared product '{product}'")
                else:
                    # Clear entire collection
                    collection_info = client.get_collection(collection_name)
                    if collection_info.points_count > 0:
                        from services.db import get_vector_size
                        from qdrant_client.models import Distance, VectorParams
                        vector_size = get_vector_size()
                        client.delete_collection(collection_name)
                        client.create_collection(
                            collection_name=collection_name,
                            vectors_config=VectorParams(
                                size=vector_size,
                                distance=Distance.COSINE
                            )
                        )
                        job_status.message = "Cleared all existing data, storing new chunks..."
            except Exception as e:
                print(f"Warning: Could not clear existing data: {e}")
        
        # Prepare data for Qdrant
        documents = [chunk['text'] for chunk in chunks]
        metadatas = [chunk['metadata'] for chunk in chunks]
        ids = [chunk['id'] for chunk in chunks]
        
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
        
        # Progress: 70% - Embeddings done, preparing upload
        job_status.progress = 70
        job_status.message = f"Preparing {len(chunks)} vectors for upload..."
        ingestion_jobs[job_id] = job_status
        
        # Batch size for Qdrant upload
        batch_size = int(os.getenv("QDRANT_BATCH_SIZE", "256"))
        print(f"Storing {len(chunks)} chunks in Qdrant (batch_size={batch_size}, parallel=4)...")
        
        # Create Qdrant points
        ingestion_timestamp = datetime.now().isoformat()
        points = [
            PointStruct(
                id=hash(chunk_id) % (2**63),  # Convert string ID to int for Qdrant
                vector=embedding.tolist(),
                payload={
                    **meta, 
                    'content': doc, 
                    'text': doc,
                    'product': product,
                    'ingested_at': ingestion_timestamp
                }
            )
            for doc, meta, chunk_id, embedding in zip(documents, metadatas, ids, embeddings)
        ]
        
        # Progress: 80% - Uploading to Qdrant
        job_status.progress = 80
        job_status.message = f"Uploading {len(points)} vectors to Qdrant..."
        ingestion_jobs[job_id] = job_status
        
        # Upload points
        client.upload_points(
            collection_name=collection_name,
            points=points,
            batch_size=batch_size,
            parallel=4,
            wait=True
        )
        
        # Complete
        job_status.status = "completed"
        job_status.progress = 100
        job_status.message = f"Successfully ingested {len(chunks)} chunks from {len(processed_pages)} pages for product '{product}'"
        job_status.completed_at = datetime.now().isoformat()
        ingestion_jobs[job_id] = job_status
        
        print(f"✅ Ingestion complete: {len(chunks)} chunks from {len(processed_pages)} pages for product '{product}'")
        
        # Clear query cache if enabled
        try:
            from services.db import get_retriever
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
    """Delete all data for a specific product"""
    try:
        client, collection_name = get_qdrant_collection()
        product = product.lower().strip()
        
        # Count points before deletion
        count_result = client.count(
            collection_name=collection_name,
            count_filter=Filter(
                must=[FieldCondition(key="product", match=MatchValue(value=product))]
            )
        )
        points_to_delete = count_result.count
        
        if points_to_delete > 0:
            client.delete(
                collection_name=collection_name,
                points_selector=Filter(
                    must=[FieldCondition(key="product", match=MatchValue(value=product))]
                )
            )
        
        # Clear query cache after deletion
        try:
            from services.db import get_retriever
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
