"""Job storage and background pipeline runner."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pdf2duckdb.api.errors import ErrorInfo, classify_error
from pdf2duckdb.api.schemas import ErrorCategory, ErrorCode
from pdf2duckdb.pipeline import run_extraction_pipeline

JOBS_ROOT = Path(os.getenv("API_JOBS_DIR", "output/jobs"))


@dataclass
class Job:
    id: str
    status: str = "queued"
    message: str = ""
    profile: str = "novel"
    user_request: str | None = None
    resume: bool = False
    skip_wrap_up: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    pdf_path: Path | None = None
    db_path: Path | None = None
    error_code: str | None = None
    error_category: str | None = None
    retryable: bool | None = None
    phase: str | None = None
    hint: str | None = None
    http_status: int | None = None
    failed_at: str | None = None

    @property
    def job_dir(self) -> Path:
        return JOBS_ROOT / self.id

    def clear_error(self) -> None:
        self.error_code = None
        self.error_category = None
        self.retryable = None
        self.phase = None
        self.hint = None
        self.http_status = None
        self.failed_at = None

    def apply_error(self, info: ErrorInfo) -> None:
        self.status = "failed"
        self.message = info.message
        self.error_code = info.code.value
        self.error_category = info.category.value
        self.retryable = info.retryable
        self.phase = info.phase
        self.hint = info.hint
        self.http_status = info.http_status
        self.failed_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pdf_path"] = str(self.pdf_path) if self.pdf_path else None
        data["db_path"] = str(self.db_path) if self.db_path else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        pdf_path = data.get("pdf_path")
        db_path = data.get("db_path")
        return cls(
            id=data["id"],
            status=data.get("status", "queued"),
            message=data.get("message", ""),
            profile=data.get("profile", "novel"),
            user_request=data.get("user_request"),
            resume=bool(data.get("resume", False)),
            skip_wrap_up=bool(data.get("skip_wrap_up", False)),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            pdf_path=Path(pdf_path) if pdf_path else None,
            db_path=Path(db_path) if db_path else None,
            error_code=data.get("error_code"),
            error_category=data.get("error_category"),
            retryable=data.get("retryable"),
            phase=data.get("phase"),
            hint=data.get("hint"),
            http_status=data.get("http_status"),
            failed_at=data.get("failed_at"),
        )


_jobs: dict[str, Job] = {}


def _meta_path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "meta.json"


def save_job(job: Job) -> None:
    job.job_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(job.id).write_text(
        json.dumps(job.to_dict(), indent=2),
        encoding="utf-8",
    )
    _jobs[job.id] = job


def load_jobs_from_disk() -> None:
    """Restore job metadata after a server restart (files remain on disk)."""
    if not JOBS_ROOT.exists():
        return
    for job_dir in JOBS_ROOT.iterdir():
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "meta.json"
        if not meta_file.exists():
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            job = Job.from_dict(data)
            if job.status == "running":
                job.apply_error(
                    ErrorInfo(
                        code=ErrorCode.SERVER_RESTART,
                        category=ErrorCategory.internal,
                        message=(
                            "Server restarted while job was running. "
                            "POST again with resume=true on the same job folder "
                            "is not supported via API — use CLI --resume on the db path, "
                            "or start a new job."
                        ),
                        retryable=True,
                        phase="interrupted",
                        hint=(
                            "Start a new POST /jobs (resume=false), or resume via CLI: "
                            f"pdf2duckdb extract --pdf ... --profile {job.profile} "
                            f"--output {job.db_path} --resume"
                        ),
                    )
                )
                save_job(job)
            _jobs[job.id] = job
        except (json.JSONDecodeError, KeyError):
            continue


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def create_job(
    *,
    profile: str,
    user_request: str | None,
    resume: bool = False,
    skip_wrap_up: bool = False,
) -> Job:
    job_id = str(uuid.uuid4())
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    job = Job(
        id=job_id,
        profile=profile,
        user_request=user_request,
        resume=resume,
        skip_wrap_up=skip_wrap_up,
        pdf_path=job_dir / "input.pdf",
        db_path=job_dir / "extracted.db",
    )
    save_job(job)
    return job


def fail_job(job: Job, info: ErrorInfo) -> None:
    job.apply_error(info)
    save_job(job)


async def run_job(job: Job) -> None:
    if not job.pdf_path or not job.pdf_path.exists():
        fail_job(
            job,
            ErrorInfo(
                code=ErrorCode.PDF_NOT_FOUND,
                category=ErrorCategory.validation,
                message="Uploaded PDF not found on disk",
                retryable=False,
                phase="startup",
                hint="Re-upload the PDF with POST /jobs.",
            ),
        )
        return

    job.status = "running"
    job.message = ""
    job.clear_error()
    save_job(job)

    try:
        result = await run_extraction_pipeline(
            pdf_path=job.pdf_path,
            profile=job.profile,
            user_request=job.user_request,
            db_path=job.db_path,
            resume=job.resume,
            skip_wrap_up=job.skip_wrap_up,
        )
        job.db_path = result.db_path
        job.status = "completed"
        job.message = "Extraction finished successfully"
        job.clear_error()
    except Exception as exc:
        fail_job(job, classify_error(exc))
    else:
        save_job(job)


def job_to_status_dict(job: Job, *, db_ready: bool) -> dict[str, Any]:
    """Build GET /jobs response payload including error fields when failed."""
    payload: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "message": job.message or None,
        "db_ready": db_ready,
        "pdf_path": str(job.pdf_path) if job.pdf_path else None,
        "db_path": str(job.db_path) if job.db_path and db_ready else None,
    }
    if job.status == "failed":
        payload.update(
            {
                "error_code": job.error_code,
                "error_category": job.error_category,
                "retryable": job.retryable,
                "phase": job.phase,
                "hint": job.hint,
                "http_status": job.http_status,
                "failed_at": job.failed_at,
            }
        )
    return payload
