"""Database and service initialization with Qdrant"""

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer
import os
import time
import threading
from datetime import datetime
from dotenv import load_dotenv
from services.retriever import EnhancedConfluenceRetriever

# Load environment variables
load_dotenv()

# Global instances
_qdrant_client = None
_embedding_model = None
_retriever = None
_connection_lock = threading.RLock()

# Connection health tracking
_connection_stats = {
    'created_at': None,
    'last_health_check': None,
    'query_count': 0,
    'reconnect_count': 0,
    'last_error': None
}

# Cache collection name and vector size
_collection_name = None
_vector_size = None


def get_connection_stats():
    """Get connection statistics for monitoring"""
    with _connection_lock:
        stats = _connection_stats.copy()
        stats['is_connected'] = _qdrant_client is not None
        stats['uptime_seconds'] = (
            (datetime.now() - datetime.fromisoformat(_connection_stats['created_at'])).total_seconds()
            if _connection_stats['created_at'] else 0
        )
        return stats


def is_connection_healthy():
    """Check if Qdrant connection is healthy"""
    global _qdrant_client, _connection_stats
    
    if _qdrant_client is None:
        return False
    
    try:
        # Try a simple operation to verify connection
        _qdrant_client.get_collections()
        _connection_stats['last_health_check'] = datetime.now().isoformat()
        return True
    except Exception as e:
        _connection_stats['last_error'] = str(e)
        _connection_stats['last_health_check'] = datetime.now().isoformat()
        print(f"❌ Qdrant health check failed: {e}")
        return False


def close_qdrant_client():
    """Properly close Qdrant client and clean up resources"""
    global _qdrant_client, _embedding_model, _retriever
    
    with _connection_lock:
        print("🔄 Closing Qdrant connections...")
        
        # Clean up retriever cache
        if _retriever:
            try:
                if hasattr(_retriever, 'clear_cache'):
                    _retriever.clear_cache()
            except Exception as e:
                print(f"Warning: Error cleaning up retriever: {e}")
            finally:
                _retriever = None
        
        # Close Qdrant client
        if _qdrant_client:
            try:
                _qdrant_client.close()
            except Exception as e:
                print(f"Warning: Error closing Qdrant client: {e}")
            finally:
                _qdrant_client = None
        
        # Clear embedding model
        _embedding_model = None
        
        print("✓ Qdrant connections closed")


def reset_connections():
    """Reset all connections (useful for recovery from errors)"""
    global _connection_stats
    
    print("🔄 Resetting all database connections...")
    close_qdrant_client()
    
    with _connection_lock:
        _connection_stats['reconnect_count'] += 1
    
    # Force reconnect on next access
    return get_qdrant_client()


def get_qdrant_client():
    """Get or create Qdrant client with health check and auto-recovery"""
    global _qdrant_client, _connection_stats
    
    with _connection_lock:
        # Check if existing connection is healthy
        if _qdrant_client is not None:
            # Perform periodic health checks (every 60 seconds)
            should_check = (
                _connection_stats['last_health_check'] is None or
                (datetime.now() - datetime.fromisoformat(_connection_stats['last_health_check'])).total_seconds() > 60
            )
            
            if should_check and not is_connection_healthy():
                print("⚠️  Unhealthy connection detected, attempting reconnect...")
                close_qdrant_client()
                _qdrant_client = None
        
        # Create new connection if needed with retry logic for pod-to-pod connectivity
        if _qdrant_client is None:
            qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
            qdrant_host = os.getenv("QDRANT_HOST")
            qdrant_port = os.getenv("QDRANT_PORT", "6333")
            qdrant_api_key = os.getenv("QDRANT_API_KEY")

            # --- Embedded mode ----------------------------------------------------
            # If QDRANT_URL is ':memory:' or a filesystem path (doesn't start with
            # http/https and no QDRANT_HOST override), run Qdrant in-process. This
            # lets local development skip Docker entirely.
            is_embedded = (
                not qdrant_host
                and qdrant_url
                and not qdrant_url.startswith(("http://", "https://"))
            )
            if is_embedded:
                if qdrant_url == ":memory:":
                    print("🧠 Starting Qdrant in IN-MEMORY mode (non-persistent)")
                    _qdrant_client = QdrantClient(location=":memory:")
                else:
                    print(f"💾 Starting Qdrant in LOCAL-FILE mode at: {qdrant_url}")
                    os.makedirs(qdrant_url, exist_ok=True)
                    _qdrant_client = QdrantClient(path=qdrant_url)

                _connection_stats['created_at'] = datetime.now().isoformat()
                _connection_stats['last_health_check'] = datetime.now().isoformat()
                _qdrant_client.get_collections()
                print("✓ Qdrant embedded client ready")
                _connection_stats['query_count'] += 1
                return _qdrant_client

            # --- Server mode (Docker / k8s / remote) ------------------------------
            # Retry configuration for Kubernetes pod-to-pod connectivity
            max_retries = int(os.getenv("QDRANT_CONNECT_RETRIES", "5"))
            base_delay = float(os.getenv("QDRANT_CONNECT_RETRY_DELAY", "2.0"))
            
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    # Use QDRANT_HOST and QDRANT_PORT if available, otherwise fall back to QDRANT_URL
                    if qdrant_host:
                        connect_url = f"http://{qdrant_host}:{qdrant_port}"
                    else:
                        connect_url = qdrant_url
                    
                    print(f"🔌 Connecting to Qdrant at: {connect_url} (attempt {attempt}/{max_retries})")
                    
                    _qdrant_client = QdrantClient(
                        url=connect_url,
                        api_key=qdrant_api_key if qdrant_api_key else None,
                        timeout=60,
                        prefer_grpc=False  # Use HTTP for better cross-namespace compatibility
                    )
                    
                    # Update connection stats
                    _connection_stats['created_at'] = datetime.now().isoformat()
                    _connection_stats['last_health_check'] = datetime.now().isoformat()
                    
                    # Verify connection works
                    _qdrant_client.get_collections()
                    print("✓ Qdrant connection established")
                    break  # Success, exit retry loop
                    
                except Exception as e:
                    last_error = e
                    _connection_stats['last_error'] = str(e)
                    print(f"⚠️  Connection attempt {attempt}/{max_retries} failed: {e}")
                    
                    if attempt < max_retries:
                        # Exponential backoff with jitter
                        delay = base_delay * (2 ** (attempt - 1))
                        print(f"   Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                    else:
                        print(f"❌ Failed to connect to Qdrant after {max_retries} attempts")
                        raise last_error
        
        # Increment query counter
        _connection_stats['query_count'] += 1
        
        return _qdrant_client


def get_embedding_model():
    """Get or create embedding model"""
    global _embedding_model
    
    if _embedding_model is None:
        model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
        print(f"Loading embedding model: {model_name}")
        _embedding_model = SentenceTransformer(model_name)
    
    return _embedding_model


def get_vector_size():
    """Get the vector dimension from the embedding model"""
    global _vector_size
    
    if _vector_size is None:
        model = get_embedding_model()
        _vector_size = model.get_sentence_embedding_dimension()
        print(f"Vector dimension: {_vector_size}")
    
    return _vector_size


def get_collection_name():
    """Get collection name from environment"""
    global _collection_name
    
    if _collection_name is None:
        _collection_name = os.getenv("COLLECTION_NAME", "confluence_pages")
    
    return _collection_name


def ensure_collection_exists():
    """Ensure the Qdrant collection exists, create if not"""
    client = get_qdrant_client()
    collection_name = get_collection_name()
    vector_size = get_vector_size()
    
    try:
        # Check if collection exists
        collections = client.get_collections().collections
        collection_exists = any(c.name == collection_name for c in collections)
        
        if not collection_exists:
            # Create collection with vector configuration
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=vector_size,
                    distance=Distance.COSINE
                )
            )
            print(f"✓ Created new collection: {collection_name}")
        else:
            print(f"✓ Collection exists: {collection_name}")
        
        return client, collection_name
        
    except Exception as e:
        print(f"❌ Error accessing Qdrant collection: {e}")
        raise


def get_qdrant_collection():
    """Get Qdrant client and collection name (replaces get_chroma_collection)"""
    return ensure_collection_exists()


def get_retriever():
    """Get or create retriever instance"""
    global _retriever
    
    if _retriever is None:
        client, collection_name = get_qdrant_collection()
        
        # Read cache configuration (disabled by default for backward compatibility)
        enable_cache = os.getenv("ENABLE_QUERY_CACHE", "false").lower() in ("true", "1", "yes")
        query_cache_size = int(os.getenv("QUERY_CACHE_SIZE", "1000"))
        query_cache_ttl = int(os.getenv("QUERY_CACHE_TTL", "1800"))
        
        _retriever = EnhancedConfluenceRetriever(
            qdrant_client=client,
            collection_name=collection_name,
            use_bm25=True,
            use_hybrid_search=True,
            enable_cache=enable_cache,
            query_cache_size=query_cache_size,
            query_cache_ttl=query_cache_ttl
        )
    
    return _retriever


# Aliases for backward compatibility
def get_chroma_client():
    """Alias for get_qdrant_client (backward compatibility)"""
    return get_qdrant_client()


def get_chroma_collection():
    """Alias for get_qdrant_collection (backward compatibility)"""
    return get_qdrant_collection()


def close_chroma_client():
    """Alias for close_qdrant_client (backward compatibility)"""
    return close_qdrant_client()
