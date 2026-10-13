"""Request/response models for the extraction API."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class ErrorCode(str, Enum):
    """Stable machine-readable failure id (shown in Swagger on failed jobs)."""

    MISSING_API_KEY = "missing_api_key"
    INVALID_SETTINGS = "invalid_settings"
    RESUME_CHECKPOINT_MISMATCH = "resume_checkpoint_mismatch"
    RESUME_RUN_MISMATCH = "resume_run_mismatch"
    PDF_NOT_FOUND = "pdf_not_found"
    SERVER_RESTART = "server_restart"
    OPENROUTER_UNAUTHORIZED = "openrouter_unauthorized"
    OPENROUTER_PAYMENT_REQUIRED = "openrouter_payment_required"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    PROVIDER_ERROR = "provider_error"
    NETWORK_ERROR = "network_error"
    REQUEST_TIMEOUT = "request_timeout"
    VALIDATION_ERROR = "validation_error"
    UNKNOWN = "unknown"


class ErrorCategory(str, Enum):
    """High-level bucket so you can tell config vs auth vs provider issues."""

    configuration = "configuration"
    auth = "auth"
    billing = "billing"
    model = "model"
    provider = "provider"
    network = "network"
    validation = "validation"
    internal = "internal"


class JobCreateResponse(BaseModel):
    job_id: str


class ApiErrorResponse(BaseModel):
    """Returned on POST /jobs when the request is rejected before a job starts."""

    error_code: ErrorCode
    error_category: ErrorCategory
    message: str
    retryable: bool = False
    hint: str | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str | None = None
    db_ready: bool = False
    pdf_path: str | None = Field(
        default=None,
        description="Server path to the uploaded PDF (for debugging)",
    )
    db_path: str | None = Field(
        default=None,
        description="Server path to the DuckDB file when completed",
    )
    error_code: ErrorCode | None = Field(
        default=None,
        description="Set when status is failed — use this in Swagger to see why",
    )
    error_category: ErrorCategory | None = Field(
        default=None,
        description="configuration | auth | billing | provider | ...",
    )
    retryable: bool | None = Field(
        default=None,
        description="Whether retrying the same job may succeed",
    )
    phase: str | None = Field(
        default=None,
        description="Pipeline phase when the failure occurred",
    )
    hint: str | None = Field(
        default=None,
        description="Suggested fix for this error",
    )
    http_status: int | None = Field(
        default=None,
        description="Upstream HTTP status from OpenRouter when applicable",
    )
    failed_at: str | None = Field(
        default=None,
        description="ISO timestamp when the job failed",
    )
