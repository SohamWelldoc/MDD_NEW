"""Pydantic models for API request/response"""

from pydantic import BaseModel, Field, field_validator, HttpUrl
from typing import List, Optional, Dict
from datetime import datetime
import re


# Chat Models
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    session_id: Optional[str] = Field(None, max_length=100)
    user_name: str = Field(default="default_user", min_length=1, max_length=100)
    n_results: int = Field(default=10, ge=1, le=100)
    use_context: bool = True
    show_sources: bool = True
    product: Optional[str] = Field(None, max_length=100, description="Product identifier for filtering (e.g., 'cvs', 'lillyhealth', 'welldoc')")
    
    @field_validator('message')
    @classmethod
    def sanitize_message(cls, v: str) -> str:
        """Sanitize message input"""
        # Remove control characters except newlines and tabs
        v = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', v)
        # Strip leading/trailing whitespace
        v = v.strip()
        if not v:
            raise ValueError('Message cannot be empty after sanitization')
        return v
    
    @field_validator('user_name')
    @classmethod
    def sanitize_user_name(cls, v: str) -> str:
        """Sanitize username"""
        # Allow only alphanumeric, underscore, hyphen, and space
        v = re.sub(r'[^a-zA-Z0-9_\-\s]', '', v)
        v = v.strip()
        if not v:
            raise ValueError('Invalid user_name format')
        return v
    
    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v: Optional[str]) -> Optional[str]:
        """Validate session ID format"""
        if v is None:
            return v
        # Allow only alphanumeric and hyphens
        if not re.match(r'^[a-zA-Z0-9\-]+$', v):
            raise ValueError('Invalid session_id format')
        return v
    
    @field_validator('product')
    @classmethod
    def validate_product(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize product identifier"""
        if v is None:
            return v
        v = v.strip().lower()
        # Allow only alphanumeric, underscore, and hyphen
        if not re.match(r'^[a-z0-9_\-]+$', v):
            raise ValueError('Invalid product format (use lowercase alphanumeric, hyphens, underscores)')
        return v


class SourceReference(BaseModel):
    title: str
    url: str
    score: float


class ChatResponse(BaseModel):
    answer: str
    sources: List[SourceReference]
    session_id: str
    n_results: int
    avg_score: float
    timestamp: str
    error: bool = False


# Ingestion Models
class IngestionRequest(BaseModel):
    confluence_url: str = Field(..., min_length=10, max_length=500)
    username: str = Field(..., min_length=3, max_length=200)
    api_token: str = Field(..., min_length=10, max_length=500)
    space_key: Optional[str] = Field(None, max_length=100)
    page_id: Optional[str] = Field(None, max_length=100)
    product: Optional[str] = Field(None, max_length=100, description="Product identifier (e.g., 'cvs', 'lillyhealth', 'welldoc')")
    chunk_size: int = Field(default=1500, ge=500, le=3000)
    chunk_overlap: int = Field(default=200, ge=0, le=500)
    clear_existing: bool = False
    clear_product_only: bool = Field(default=True, description="When clear_existing is True, only clear data for the specified product")
    
    @field_validator('confluence_url')
    @classmethod
    def validate_confluence_url(cls, v: str) -> str:
        """Validate Confluence URL format"""
        v = v.strip().rstrip('/')
        # Check if it's a valid URL
        if not re.match(r'^https?://', v):
            raise ValueError('URL must start with http:// or https://')
        # Check for common Confluence patterns
        if not any(pattern in v.lower() for pattern in ['atlassian.net', 'confluence', 'wiki']):
            # Allow any URL but warn it might not be Confluence
            pass
        # Basic URL validation
        if not re.match(r'^https?://[\w\-\.]+(:\d+)?(/.*)?$', v):
            raise ValueError('Invalid URL format')
        return v
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate and sanitize username (usually email)"""
        v = v.strip()
        # Basic email or username validation
        if '@' in v:  # Email format
            if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', v):
                raise ValueError('Invalid email format')
        return v
    
    @field_validator('api_token')
    @classmethod
    def validate_api_token(cls, v: str) -> str:
        """Validate API token format"""
        v = v.strip()
        if not v:
            raise ValueError('API token cannot be empty')
        # Check for suspicious characters
        if any(char in v for char in ['<', '>', '"', "'", '\\']):
            raise ValueError('API token contains invalid characters')
        return v
    
    @field_validator('space_key')
    @classmethod
    def validate_space_key(cls, v: Optional[str]) -> Optional[str]:
        """Validate space key format"""
        if v is None:
            return v
        v = v.strip()
        # Space keys are typically uppercase alphanumeric
        if not re.match(r'^[A-Z0-9\-_]+$', v, re.IGNORECASE):
            raise ValueError('Invalid space_key format (use alphanumeric, hyphens, underscores)')
        return v
    
    @field_validator('page_id')
    @classmethod
    def validate_page_id(cls, v: Optional[str]) -> Optional[str]:
        """Validate page ID format"""
        if v is None:
            return v
        v = v.strip()
        # Page IDs are typically numeric or alphanumeric
        if not re.match(r'^[a-zA-Z0-9]+$', v):
            raise ValueError('Invalid page_id format (use alphanumeric only)')
        return v
    
    @field_validator('product')
    @classmethod
    def validate_product_ingestion(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize product identifier"""
        if v is None:
            return v
        v = v.strip().lower()
        # Allow only alphanumeric, underscore, and hyphen
        if not re.match(r'^[a-z0-9_\-]+$', v):
            raise ValueError('Invalid product format (use lowercase alphanumeric, hyphens, underscores)')
        return v


class IngestionStatus(BaseModel):
    job_id: str
    status: str  # "pending", "processing", "completed", "failed"
    progress: int  # 0-100
    pages_processed: int
    chunks_created: int
    product: Optional[str] = None  # Product being ingested
    current_page: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None


class IngestionResponse(BaseModel):
    job_id: str
    status: str
    message: str


# Session Models
class SessionInfo(BaseModel):
    session_id: str
    user_name: str
    created_at: str
    total_messages: int
    questions_asked: int
    unique_sources: int
    duration_minutes: float


class SessionHistory(BaseModel):
    session_id: str
    user_name: str
    chat_history: List[Dict]
    stats: Dict


# Health/Status Models
class HealthResponse(BaseModel):
    status: str
    timestamp: str
    details: Optional[Dict] = None


class StatsResponse(BaseModel):
    total_documents: int
    total_chunks: int
    spaces: List[str]
    collection_name: str


# Product Management Models
class ProductInfo(BaseModel):
    """Information about a product/tenant in the database"""
    product: str
    display_name: str
    chunk_count: int
    space_keys: List[str]
    last_updated: Optional[str] = None


class ProductStatsResponse(BaseModel):
    """Response containing product-wise statistics"""
    total_chunks: int
    total_products: int
    products: List[ProductInfo]
    collection_name: str


class ClearProductRequest(BaseModel):
    """Request to clear data for a specific product"""
    product: str = Field(..., min_length=1, max_length=100)


# ============================================================
# HLD Pipeline Models (MDD_NEW additions)
# ============================================================

class RequirementsGenerationRequest(BaseModel):
    """Trigger requirements extraction from the already-ingested Confluence corpus."""
    product: Optional[str] = Field(None, max_length=100,
                                   description="Optional product filter (matches ingestion product)")
    n_results: int = Field(default=12, ge=1, le=30,
                           description="Top-K chunks per probe query")


class RequirementsGenerationResponse(BaseModel):
    job_id: str
    product: Optional[str] = None
    artifact_path: str
    started_at: str
    completed_at: str
    requirements: Dict


class CodebaseAnalysisRequest(BaseModel):
    """Resolve a feature contract against the monolith graph and requirements."""
    contract_path: Optional[str] = Field(
        None, max_length=1000,
        description="Path to contract_{ticket}.json (feature scope + seedSymbols)",
    )
    ticket: Optional[str] = Field(
        None, max_length=50,
        description="Feature ticket e.g. AL-27103; auto-loads contract_{ticket}.json if contract_path omitted",
    )
    graph_path: Optional[str] = Field(
        None, max_length=1000,
        description="Override GRAPH_PATH env (monolith graph.json); rarely needed",
    )
    source_path: Optional[str] = Field(
        None, max_length=1000,
        description="Deprecated alias for graph_path override",
    )


class CodebaseAnalysisResponse(BaseModel):
    job_id: str
    source_path: str
    contract_path: Optional[str] = None
    artifact_path: str
    stats: Dict
    started_at: str
    completed_at: str


class HLDGenerationRequest(BaseModel):
    """Generate HLD from the latest (or specified) requirements + code_graph artifacts."""
    requirements_path: Optional[str] = Field(None, max_length=1000)
    code_graph_path: Optional[str] = Field(None, max_length=1000)


class HLDGenerationResponse(BaseModel):
    job_id: str
    plan: Dict
    diagram_report: Dict
    artifact_paths: Dict
    started_at: str
    completed_at: str


class PipelineRunRequest(BaseModel):
    """End-to-end orchestration: requirements -> codebase -> HLD."""
    confluence_product: Optional[str] = Field(None, max_length=100)
    contract_path: Optional[str] = Field(None, max_length=1000)
    ticket: Optional[str] = Field(None, max_length=50)
    graph_path: Optional[str] = Field(None, max_length=1000)
    source_path: Optional[str] = Field(
        None, max_length=1000,
        description="Deprecated alias for graph_path override",
    )
    n_results: int = Field(default=8, ge=1, le=30)


class PipelineRunResponse(BaseModel):
    requirements: RequirementsGenerationResponse
    codebase: CodebaseAnalysisResponse
    hld: HLDGenerationResponse


# ============================================================
# MDD Pipeline Models
# ============================================================


class MDDModuleInfo(BaseModel):
    """Single logical module entry from mdd_modules.json."""

    id: str
    logical_name: str
    slug: str

    architectural_layer: Optional[str] = None
    summary: Optional[str] = None

    target_projects: List[str] = Field(default_factory=list)
    primary_symbols: List[str] = Field(default_factory=list)

    flow_count: int = 0
    has_hld_section: bool = False
    has_code_mapping: bool = False

    in_requirements: bool = False
    in_hld: bool = False
    in_code_graph: bool = False
    dependency_only: bool = False


class MDDModuleCatalogResponse(BaseModel):
    job_id: str
    ticket: Optional[str] = None

    catalog_source: str
    catalog_warnings: List[str] = Field(default_factory=list)

    hld_path: Optional[str] = None
    module_count: int = 0

    modules: List[MDDModuleInfo] = Field(default_factory=list)


class MDDGenerateRequest(BaseModel):
    selected_modules: List[str] = Field(..., min_length=1)
    ticket: Optional[str] = Field(None, max_length=50)


class MDDGeneratedModule(BaseModel):
    module: str
    slug: str
    path: str
    sections_included: List[str] = Field(default_factory=list)
    sections_skipped: List[str] = Field(default_factory=list)


class MDDGenerateResponse(BaseModel):
    job_id: str
    ticket: Optional[str] = None
    started_at: str
    completed_at: str
    generated: List[MDDGeneratedModule] = Field(default_factory=list)
    manifest_path: str
