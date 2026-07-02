"""File-backed vector store for release-scoped RAG artifacts.

Embeddings and chunks are stored as JSONL under:

    artifacts/<project>/<release>/confluence/
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from services.artifact_store.artifact_paths import (
    artifact_base_dir,
    artifact_context,
    current_release,
    current_timestamp,
    latest_artifact_root,
    latest_matching,
    safe_segment,
    timestamp_slug,
    write_latest_run_pointer,
    write_json,
)


def _backend_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _base_artifact_dir() -> Path:
    return artifact_base_dir()


def _safe_segment(value: str, default: str) -> str:
    return safe_segment(value, default)


def _timestamp() -> str:
    return timestamp_slug()


def release_artifact_dir(
    *,
    project: Optional[str] = None,
    release: Optional[str] = None,
    timestamp: Optional[str] = None,
    create: bool = True,
) -> Path:
    """Resolve artifacts/<project>/<release>/confluence and optionally create it."""
    return artifact_context(
        product=project,
        release=release,
        timestamp=timestamp,
        create=create,
    ).stage_dir("confluence", create=create)


def latest_artifact_dir(project: Optional[str] = None, release: Optional[str] = None) -> Optional[Path]:
    """Return the confluence artifact directory for project/release, if present."""
    root = latest_artifact_root(project, release)
    if not root:
        return None
    confluence_dir = root / "confluence"
    return confluence_dir if confluence_dir.is_dir() else None


def active_artifact_dir(
    *,
    project: Optional[str] = None,
    release: Optional[str] = None,
    timestamp: Optional[str] = None,
    create: bool = True,
) -> Path:
    """Resolve the active confluence directory, preferring explicit ARTIFACT_DIR/latest."""
    explicit = os.getenv("ARTIFACT_DIR")
    if explicit:
        path = Path(explicit).resolve()
        if path.name != "confluence" and (path / "confluence").is_dir():
            path = path / "confluence"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    latest = None if timestamp else latest_artifact_dir(project, release)
    if latest and not create:
        return latest
    return release_artifact_dir(
        project=project,
        release=release,
        timestamp=timestamp,
        create=create,
    )


def _write_latest_pointer(path: Path, project: str, release: str, timestamp: str) -> None:
    root = path.parent if path.name == "confluence" else path
    write_latest_run_pointer(project, release, timestamp, root)


def _jsonl_write(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def _jsonl_read(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    total = sum(x * y for x, y in zip(a, b))
    if total == 0:
        return 0.0
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return total / (norm_a * norm_b)


class JsonVectorStore:
    """Small JSONL vector store for Confluence chunks."""

    def __init__(self, artifact_dir: Path, timestamp: Optional[str] = None):
        self.artifact_dir = Path(artifact_dir).resolve()
        self.timestamp = current_timestamp(timestamp)
        self.chunks_path = self._resolve_read_path("chunks", ".jsonl")
        self.embeddings_path = self._resolve_read_path("embeddings", ".jsonl")
        self.manifest_path = self._resolve_read_path("manifest", ".json")
        self._records_cache: Optional[List[Dict[str, Any]]] = None
        self._cache_key: Optional[tuple] = None

    def _timestamped_path(self, prefix: str, suffix: str) -> Path:
        return self.artifact_dir / f"{prefix}_{self.timestamp}{suffix}"

    def _alias_path(self, prefix: str, suffix: str) -> Path:
        return self.artifact_dir / f"{prefix}{suffix}"

    def _resolve_read_path(self, prefix: str, suffix: str) -> Path:
        latest = latest_matching(self.artifact_dir, f"{prefix}_*{suffix}")
        return latest or self._timestamped_path(prefix, suffix)

    def clear(self) -> None:
        if self.artifact_dir.is_dir():
            for pattern in ("chunks*.jsonl", "embeddings*.jsonl", "manifest*.json"):
                for path in self.artifact_dir.glob(pattern):
                    if path.exists():
                        path.unlink()
        self._records_cache = None
        self._cache_key = None

    def write(
        self,
        *,
        chunks: List[Dict[str, Any]],
        embeddings: List[Any],
        product: str,
        release: str,
        pages_processed: int,
        model_name: str,
    ) -> Dict[str, Any]:
        """Persist chunks and embeddings as JSONL files."""
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        ingested_at = current_timestamp()
        timestamped_chunks_path = self._timestamped_path("chunks", ".jsonl")
        timestamped_embeddings_path = self._timestamped_path("embeddings", ".jsonl")
        timestamped_manifest_path = self._timestamped_path("manifest", ".json")

        chunk_rows = []
        embedding_rows = []
        for chunk, embedding in zip(chunks, embeddings):
            metadata = {
                **(chunk.get("metadata") or {}),
                "product": product,
                "release": release,
                "ingested_at": ingested_at,
            }
            text = chunk.get("text", "")
            chunk_id = chunk.get("id", "")
            chunk_rows.append({
                "id": chunk_id,
                "text": text,
                "content": text,
                "metadata": metadata,
            })
            vector = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            embedding_rows.append({
                "id": chunk_id,
                "embedding": vector,
                "metadata": metadata,
            })

        chunk_count = _jsonl_write(timestamped_chunks_path, chunk_rows)
        embedding_count = _jsonl_write(timestamped_embeddings_path, embedding_rows)
        manifest = {
            "storage": "jsonl",
            "artifact_dir": str(self.artifact_dir),
            "chunks_path": str(timestamped_chunks_path),
            "embeddings_path": str(timestamped_embeddings_path),
            "product": product,
            "release": release,
            "pages_processed": pages_processed,
            "chunks_created": chunk_count,
            "embeddings_created": embedding_count,
            "embedding_model": model_name,
            "timestamp": self.timestamp,
            "created_at": ingested_at,
        }
        write_json(timestamped_manifest_path, manifest)
        self.chunks_path = timestamped_chunks_path
        self.embeddings_path = timestamped_embeddings_path
        self.manifest_path = timestamped_manifest_path
        _retain_latest_confluence_runs(self.artifact_dir)
        self._records_cache = None
        self._cache_key = None
        return manifest

    def records(self) -> List[Dict[str, Any]]:
        """Load chunk rows joined with embeddings."""
        cache_key = (
            self.chunks_path.stat().st_mtime if self.chunks_path.exists() else None,
            self.embeddings_path.stat().st_mtime if self.embeddings_path.exists() else None,
        )
        if self._records_cache is not None and self._cache_key == cache_key:
            return self._records_cache

        chunks = {row.get("id"): row for row in _jsonl_read(self.chunks_path)}
        embeddings = _jsonl_read(self.embeddings_path)
        records = []
        for row in embeddings:
            chunk = chunks.get(row.get("id"), {})
            metadata = {
                **(chunk.get("metadata") or {}),
                **(row.get("metadata") or {}),
            }
            text = chunk.get("text") or chunk.get("content") or metadata.get("content") or ""
            records.append({
                "id": row.get("id"),
                "content": text,
                "text": text,
                "metadata": metadata,
                "embedding": row.get("embedding") or [],
            })
        self._records_cache = records
        self._cache_key = cache_key
        return records

    def search(
        self,
        *,
        query_vector: List[float],
        limit: int,
        product: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Return top records by cosine similarity."""
        scored = []
        for record in self.records():
            metadata = record.get("metadata") or {}
            if product and metadata.get("product") != product:
                continue
            if filters and any(metadata.get(k) != v for k, v in filters.items()):
                continue
            similarity = _cosine(query_vector, record.get("embedding") or [])
            scored.append({
                "id": record.get("id"),
                "content": record.get("content", ""),
                "metadata": metadata,
                "vector_similarity": similarity,
                "vector_distance": 1 - similarity,
                "combined_score": 0.0,
            })
        scored.sort(key=lambda item: item["vector_similarity"], reverse=True)
        return scored[:limit]

    def delete_product(self, product: str) -> int:
        """Remove product rows from chunks/embeddings JSONL."""
        product = product.lower().strip()
        chunks = _jsonl_read(self.chunks_path)
        embeddings = _jsonl_read(self.embeddings_path)
        keep_chunk_ids = {
            row.get("id")
            for row in chunks
            if (row.get("metadata") or {}).get("product") != product
        }
        deleted = len(chunks) - len(keep_chunk_ids)
        if deleted <= 0:
            return 0
        _jsonl_write(self.chunks_path, [row for row in chunks if row.get("id") in keep_chunk_ids])
        _jsonl_write(self.embeddings_path, [row for row in embeddings if row.get("id") in keep_chunk_ids])
        self._records_cache = None
        self._cache_key = None
        return deleted

def _retain_latest_confluence_runs(artifact_dir: Path, keep: int = 1) -> None:
    """Keep only the latest timestamped Confluence vector/artifact set."""
    timestamps: set[str] = set()
    for pattern in ("chunks_*.jsonl", "embeddings_*.jsonl", "manifest_*.json", "confluence_*.json"):
        for path in artifact_dir.glob(pattern):
            stem = path.stem
            if "_" not in stem:
                continue
            timestamps.add(stem.rsplit("_", 1)[-1])

    keep_timestamps = set(sorted(timestamps, reverse=True)[:keep])
    for path in artifact_dir.iterdir() if artifact_dir.is_dir() else []:
        if not path.is_file():
            continue
        name = path.name
        if name in {
            "chunks.jsonl",
            "chunks_latest.jsonl",
            "embeddings.jsonl",
            "embeddings_latest.jsonl",
            "manifest.json",
            "manifest_latest.json",
            "confluence_latest.json",
        }:
            path.unlink(missing_ok=True)
            continue
        if name.startswith(("chunks_", "embeddings_", "manifest_", "confluence_")):
            timestamp = path.stem.rsplit("_", 1)[-1]
            if timestamp not in keep_timestamps:
                path.unlink(missing_ok=True)
