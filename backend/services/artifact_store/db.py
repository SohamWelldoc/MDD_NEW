"""Vector store and embedding model initialization.

Uses a lightweight JSONL-backed vector store: chunks are embedded, persisted,
and searched by cosine similarity, with Confluence vectors under
artifacts/<project>/<release>/confluence/.
"""

import os
import threading
from datetime import datetime

from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

from services.artifact_store.json_vector_store import JsonVectorStore, active_artifact_dir
from services.confluence.retriever import EnhancedConfluenceRetriever

# Load environment variables
load_dotenv()

# Global instances
_vector_store = None
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
        stats['is_connected'] = _vector_store is not None
        stats['uptime_seconds'] = (
            (datetime.now() - datetime.fromisoformat(_connection_stats['created_at'])).total_seconds()
            if _connection_stats['created_at'] else 0
        )
        return stats


def is_connection_healthy():
    """Check whether the active JSON vector store is ready."""
    global _vector_store, _connection_stats
    _connection_stats['last_health_check'] = datetime.now().isoformat()
    return _vector_store is not None


def close_vector_store():
    """Clear retriever/vector-store handles and embedding model cache."""
    global _vector_store, _embedding_model, _retriever

    with _connection_lock:
        print("Closing JSON vector store handles...")

        if _retriever:
            try:
                if hasattr(_retriever, 'clear_cache'):
                    _retriever.clear_cache()
            except Exception as e:
                print(f"Warning: Error cleaning up retriever: {e}")
            finally:
                _retriever = None

        _vector_store = None
        _embedding_model = None

        print("JSON vector store handles closed")


def reset_connections():
    """Reset all connections (useful for recovery from errors)"""
    global _connection_stats
    
    print("Resetting vector store handles...")
    close_vector_store()

    with _connection_lock:
        _connection_stats['reconnect_count'] += 1

    return get_vector_store()


def get_vector_store(
    *,
    project: str = None,
    release: str = None,
    timestamp: str = None,
    create: bool = True,
):
    """Get or create the active JSONL vector store."""
    global _vector_store, _connection_stats

    with _connection_lock:
        artifact_dir = active_artifact_dir(
            project=project,
            release=release,
            timestamp=timestamp,
            create=create,
        )
        if _vector_store is None or _vector_store.artifact_dir != artifact_dir:
            _vector_store = JsonVectorStore(artifact_dir, timestamp=timestamp)
            _connection_stats['created_at'] = datetime.now().isoformat()
            _connection_stats['last_health_check'] = datetime.now().isoformat()
            print(f"Using JSON vector store at: {artifact_dir}")

        _connection_stats['query_count'] += 1

        return _vector_store


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
    """Get logical collection name from environment."""
    global _collection_name

    if _collection_name is None:
        _collection_name = os.getenv("COLLECTION_NAME", "confluence_pages")

    return _collection_name


def ensure_collection_exists():
    """Return active vector store and logical collection name."""
    return get_vector_store(), get_collection_name()


def get_retriever():
    """Get or create retriever instance"""
    global _retriever
    
    if _retriever is None:
        store, collection_name = ensure_collection_exists()

        # Read cache configuration (disabled by default for backward compatibility)
        enable_cache = os.getenv("ENABLE_QUERY_CACHE", "false").lower() in ("true", "1", "yes")
        query_cache_size = int(os.getenv("QUERY_CACHE_SIZE", "1000"))
        query_cache_ttl = int(os.getenv("QUERY_CACHE_TTL", "1800"))
        
        _retriever = EnhancedConfluenceRetriever(
            vector_store=store,
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
    """Alias for get_vector_store (backward compatibility)."""
    return get_vector_store()


def get_chroma_collection():
    """Alias for ensure_collection_exists (backward compatibility)."""
    return ensure_collection_exists()


def close_chroma_client():
    """Alias for close_vector_store (backward compatibility)."""
    return close_vector_store()
