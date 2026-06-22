"""Shared artifact path helpers for product/release pipeline outputs."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


STAGES = ("ref", "confluence", "hld", "mdd", "codebase")


def backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def artifact_base_dir() -> Path:
    return Path(os.getenv("ARTIFACT_BASE_DIR", str(backend_dir() / "artifacts"))).resolve()


def safe_segment(value: Optional[str], default: str = "default") -> str:
    value = (value or default).strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._-") or default


def timestamp_slug(value: Optional[str] = None) -> str:
    if value:
        compact = re.sub(r"[^0-9]", "", value)
        if len(compact) >= 14:
            return compact[:14]
    return datetime.now().strftime("%Y%m%d%H%M%S")


def current_product(product: Optional[str] = None) -> str:
    return safe_segment(product or os.getenv("PROJECT") or os.getenv("PRODUCT") or "default")


def current_release(release: Optional[str] = None) -> str:
    return safe_segment(release or os.getenv("RELEASE") or os.getenv("TICKET") or "default")


def current_timestamp(timestamp: Optional[str] = None) -> str:
    resolved = timestamp_slug(timestamp or os.getenv("ARTIFACT_TIMESTAMP"))
    os.environ.setdefault("ARTIFACT_TIMESTAMP", resolved)
    return resolved


@dataclass(frozen=True)
class ArtifactContext:
    product: str
    release: str
    timestamp: str
    root_dir: Path

    def stage_dir(self, stage: str, *, create: bool = True) -> Path:
        if stage not in STAGES:
            raise ValueError(f"Unknown artifact stage '{stage}'")
        path = self.root_dir / stage
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def filename(self, prefix: str, suffix: str) -> str:
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return f"{prefix}_{self.timestamp}{suffix}"


def artifact_context(
    *,
    product: Optional[str] = None,
    release: Optional[str] = None,
    timestamp: Optional[str] = None,
    create: bool = True,
) -> ArtifactContext:
    if not create and not timestamp:
        root = latest_artifact_root(product, release)
        if root:
            try:
                data = json.loads((artifact_base_dir() / "latest_run.json").read_text(encoding="utf-8"))
                return ArtifactContext(
                    safe_segment(data.get("product") or data.get("project") or product, "default"),
                    safe_segment(data.get("release") or release, "default"),
                    timestamp_slug(data.get("timestamp")),
                    root,
                )
            except Exception:
                return ArtifactContext(
                    current_product(product),
                    current_release(release),
                    current_timestamp(timestamp),
                    root,
                )
    product_slug = current_product(product)
    release_slug = current_release(release)
    ts = current_timestamp(timestamp)
    root = artifact_base_dir() / product_slug / release_slug
    if create:
        root.mkdir(parents=True, exist_ok=True)
        for stage in STAGES:
            (root / stage).mkdir(parents=True, exist_ok=True)
        write_latest_run_pointer(product_slug, release_slug, ts, root)
    return ArtifactContext(product_slug, release_slug, ts, root)


def write_latest_run_pointer(product: str, release: str, timestamp: str, root_dir: Path) -> None:
    pointer = artifact_base_dir() / "latest_run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "product": product,
                "project": product,
                "release": release,
                "timestamp": timestamp,
                "artifact_root": str(root_dir),
                "artifact_dir": str(root_dir),
                "updated_at": datetime.now().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def latest_artifact_root(product: Optional[str] = None, release: Optional[str] = None) -> Optional[Path]:
    base = artifact_base_dir()
    pointer = base / "latest_run.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            if product and safe_segment(product) != data.get("product") and safe_segment(product) != data.get("project"):
                raise ValueError("pointer product mismatch")
            if release and safe_segment(release) != data.get("release"):
                raise ValueError("pointer release mismatch")
            path = Path(data.get("artifact_root") or data.get("artifact_dir") or "")
            if path.is_dir():
                return path
        except Exception:
            pass

    root = base / current_product(product) / current_release(release)
    return root if root.is_dir() else None


def stage_dir(
    stage: str,
    *,
    product: Optional[str] = None,
    release: Optional[str] = None,
    timestamp: Optional[str] = None,
    create: bool = True,
) -> Path:
    return artifact_context(product=product, release=release, timestamp=timestamp, create=create).stage_dir(stage, create=create)


def latest_matching(stage_path: Path, pattern: str) -> Optional[Path]:
    if not stage_path.is_dir():
        return None
    matches = [path for path in stage_path.glob(pattern) if path.is_file()]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def latest_stage_file(
    stage: str,
    pattern: str,
    *,
    product: Optional[str] = None,
    release: Optional[str] = None,
) -> Optional[Path]:
    root = latest_artifact_root(product, release)
    if not root:
        return None
    return latest_matching(root / stage, pattern)


def copy_alias(source: Path, alias: Path) -> None:
    alias.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != alias.resolve():
        shutil.copy2(source, alias)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
