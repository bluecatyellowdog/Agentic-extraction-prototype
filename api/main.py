"""FastAPI entry point for local extraction jobs."""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from api.errors import ErrorInfo, classify_error
from api.jobs import create_job, get_job, job_to_status_dict, load_jobs_from_disk, run_job
from api.schemas import ApiErrorResponse, ErrorCategory, ErrorCode, JobCreateResponse, JobStatusResponse
from src.config import get_settings

app = FastAPI(
    title="Agentic Extraction API",
    description=(
        "Upload a PDF and start a background extraction job. "
        "Poll GET /jobs/{job_id} until status is completed. "
        "When status is failed, read error_code, error_category, and hint."
    ),
    version="0.2.0",
)


@app.on_event("startup")
def startup() -> None:
    load_jobs_from_disk()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _validate_settings_or_raise() -> None:
    try:
        get_settings()
    except ValueError as exc:
        info = classify_error(exc, phase="startup")
        raise HTTPException(
            status_code=400,
            detail=ApiErrorResponse(
                error_code=info.code,
                error_category=info.category,
                message=info.message,
                retryable=info.retryable,
                hint=info.hint,
            ).model_dump(),
        ) from exc


@app.post(
    "/jobs",
    response_model=JobCreateResponse,
    status_code=202,
    responses={
        400: {
            "model": ApiErrorResponse,
            "description": "Invalid settings (e.g. missing OPENROUTER_API_KEY)",
        }
    },
)
async def create_extraction_job(
    background_tasks: BackgroundTasks,
    pdf: UploadFile = File(..., description="PDF to extract from"),
    request: str = Form(
        ...,
        description=(
            "Plain-English extraction goal, e.g. "
            "'Extract human characters, named spells, Hogwarts house facts, and house point events'"
        ),
    ),
    resume: bool = Form(
        False,
        description="Continue a previous run in the same job folder (skip checkpointed chunks)",
    ),
    skip_wrap_up: bool = Form(
        False,
        description="Skip the final wrap-up agent that reconciles fact conflicts",
    ),
) -> JobCreateResponse:
    _validate_settings_or_raise()

    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=ApiErrorResponse(
                error_code=ErrorCode.VALIDATION_ERROR,
                error_category=ErrorCategory.validation,
                message="Upload must be a .pdf file",
                hint="Choose a file ending in .pdf",
            ).model_dump(),
        )

    content = await pdf.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail=ApiErrorResponse(
                error_code=ErrorCode.VALIDATION_ERROR,
                error_category=ErrorCategory.validation,
                message="Uploaded PDF is empty",
                hint="Upload a non-empty PDF file",
            ).model_dump(),
        )

    job = create_job(
        user_request=request,
        resume=resume,
        skip_wrap_up=skip_wrap_up,
    )
    assert job.pdf_path is not None
    job.pdf_path.write_bytes(content)

    background_tasks.add_task(run_job, job)
    return JobCreateResponse(job_id=job.id)


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def job_status(job_id: str) -> JobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    db_ready = (
        job.status == "completed"
        and job.db_path is not None
        and job.db_path.exists()
    )
    return JobStatusResponse(**job_to_status_dict(job, db_ready=db_ready))


@app.get("/jobs/{job_id}/db")
def download_database(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or job.db_path is None or not job.db_path.exists():
        detail = job_to_status_dict(job, db_ready=False)
        return JSONResponse(status_code=404, content=detail)

    return FileResponse(
        path=job.db_path,
        filename=f"extracted-{job_id[:8]}.db",
        media_type="application/octet-stream",
    )
