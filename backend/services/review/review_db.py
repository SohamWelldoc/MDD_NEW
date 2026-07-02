"""Local SQLite metadata store for review workflow state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from services.review.review_store import review_root, utc_now


def db_path(product: Optional[str], release: Optional[str]) -> Path:
    root = review_root(product, release, create=True)
    return root / "review_state.db"


def connect(product: Optional[str], release: Optional[str]) -> sqlite3.Connection:
    path = db_path(product, release)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


@contextmanager
def connection(product: Optional[str], release: Optional[str]) -> Iterator[sqlite3.Connection]:
    conn = connect(product, release)
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            product TEXT,
            release TEXT,
            document_type TEXT,
            module_slug TEXT,
            status TEXT,
            current_version TEXT,
            review_path TEXT,
            review_dir TEXT,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feedback_items (
            feedback_id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL,
            base_version TEXT,
            target_section TEXT,
            change_type TEXT,
            priority TEXT,
            target_kind TEXT,
            status TEXT,
            reviewer TEXT,
            created_at TEXT,
            updated_at TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS versions (
            review_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT,
            artifact_name TEXT,
            markdown_path TEXT,
            docx_path TEXT,
            created_at TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (review_id, version)
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL,
            event TEXT,
            actor TEXT,
            role TEXT,
            source_ip TEXT,
            created_at TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS revision_jobs (
            job_id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL,
            feedback_id TEXT,
            status TEXT,
            progress INTEGER,
            message TEXT,
            started_at TEXT,
            completed_at TEXT,
            cancelled INTEGER DEFAULT 0,
            error TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stale_mdd_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hld_review_id TEXT,
            mdd_review_id TEXT,
            module_slug TEXT,
            hld_version TEXT,
            reason TEXT,
            created_at TEXT,
            payload_json TEXT NOT NULL
        );
        """
    )
    conn.commit()


def upsert_review(review: Dict[str, Any]) -> None:
    with connection(review.get("product"), review.get("release")) as conn:
        conn.execute(
            """
            INSERT INTO reviews (
                review_id, product, release, document_type, module_slug, status,
                current_version, review_path, review_dir, created_by, created_at,
                updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_id) DO UPDATE SET
                status=excluded.status,
                current_version=excluded.current_version,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                review.get("review_id"),
                review.get("product"),
                review.get("release"),
                review.get("document_type"),
                review.get("module_slug"),
                review.get("status"),
                review.get("current_version"),
                review.get("review_path"),
                review.get("review_dir"),
                review.get("created_by"),
                review.get("created_at"),
                review.get("updated_at"),
                json.dumps(review, ensure_ascii=False),
            ),
        )
        for item in review.get("feedback_items", []) or []:
            upsert_feedback(conn, review["review_id"], item)
        for version in review.get("versions", []) or []:
            upsert_version(conn, review["review_id"], version)
        for event in review.get("audit", []) or []:
            insert_audit_event(conn, review["review_id"], event, dedupe=True)
        conn.commit()


def upsert_feedback(conn: sqlite3.Connection, review_id: str, item: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO feedback_items (
            feedback_id, review_id, base_version, target_section, change_type,
            priority, target_kind, status, reviewer, created_at, updated_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feedback_id) DO UPDATE SET
            status=excluded.status,
            updated_at=excluded.updated_at,
            payload_json=excluded.payload_json
        """,
        (
            item.get("feedback_id"),
            review_id,
            item.get("base_version"),
            item.get("target_section"),
            item.get("change_type"),
            item.get("priority"),
            item.get("target_kind"),
            item.get("status"),
            item.get("reviewer"),
            item.get("created_at"),
            item.get("updated_at"),
            json.dumps(item, ensure_ascii=False),
        ),
    )


def upsert_version(conn: sqlite3.Connection, review_id: str, version: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO versions (
            review_id, version, status, artifact_name, markdown_path, docx_path, created_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id, version) DO UPDATE SET
            status=excluded.status,
            payload_json=excluded.payload_json
        """,
        (
            review_id,
            version.get("version"),
            version.get("status"),
            version.get("artifact_name"),
            version.get("markdown_path"),
            version.get("docx_path"),
            version.get("created_at"),
            json.dumps(version, ensure_ascii=False),
        ),
    )


def insert_audit_event(
    conn: sqlite3.Connection,
    review_id: str,
    event: Dict[str, Any],
    *,
    dedupe: bool = False,
) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    if dedupe:
        existing = conn.execute(
            "SELECT 1 FROM audit_events WHERE review_id=? AND payload_json=? LIMIT 1",
            (review_id, payload),
        ).fetchone()
        if existing:
            return
    conn.execute(
        """
        INSERT INTO audit_events (review_id, event, actor, role, source_ip, created_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id,
            event.get("event"),
            event.get("actor"),
            event.get("role"),
            event.get("source_ip"),
            event.get("at") or event.get("created_at") or utc_now(),
            payload,
        ),
    )


def save_job(product: Optional[str], release: Optional[str], job: Dict[str, Any]) -> None:
    with connection(product, release) as conn:
        conn.execute(
            """
            INSERT INTO revision_jobs (
                job_id, review_id, feedback_id, status, progress, message,
                started_at, completed_at, cancelled, error, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                progress=excluded.progress,
                message=excluded.message,
                completed_at=excluded.completed_at,
                cancelled=excluded.cancelled,
                error=excluded.error,
                payload_json=excluded.payload_json
            """,
            (
                job.get("job_id"),
                job.get("review_id"),
                job.get("feedback_id"),
                job.get("status"),
                job.get("progress"),
                job.get("message"),
                job.get("started_at"),
                job.get("completed_at"),
                1 if job.get("cancelled") else 0,
                job.get("error"),
                json.dumps(job, ensure_ascii=False),
            ),
        )
        conn.commit()


def load_job(product: Optional[str], release: Optional[str], job_id: str) -> Optional[Dict[str, Any]]:
    with connection(product, release) as conn:
        row = conn.execute("SELECT payload_json FROM revision_jobs WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def find_active_job(product: Optional[str], release: Optional[str], review_id: str, feedback_id: str) -> Optional[Dict[str, Any]]:
    with connection(product, release) as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM revision_jobs
            WHERE review_id=? AND feedback_id=? AND status IN ('pending', 'processing')
            ORDER BY started_at DESC LIMIT 1
            """,
            (review_id, feedback_id),
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def list_audit(product: Optional[str], release: Optional[str], review_id: str) -> List[Dict[str, Any]]:
    with connection(product, release) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM audit_events WHERE review_id=? ORDER BY id ASC",
            (review_id,),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]
