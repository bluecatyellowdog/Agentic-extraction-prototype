"""FastAPI app: upload a PDF, run extraction as a background job, download the database."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from pdf2duckdb.api.errors import classify_error
from pdf2duckdb.api.jobs import (
    create_job,
    get_job,
    job_to_status_dict,
    load_jobs_from_disk,
    run_job,
)
from pdf2duckdb.api.schemas import (
    ApiErrorResponse,
    ErrorCategory,
    ErrorCode,
    JobCreateResponse,
    JobStatusResponse,
)
from pdf2duckdb.config import get_settings
from pdf2duckdb.profile import load_profile


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_jobs_from_disk()
    yield


app = FastAPI(
    title="pdf2duckdb API",
    description=(
        "Upload a PDF and start a background extraction job. "
        "Poll GET /jobs/{job_id} until status is completed. "
        "When status is failed, read error_code, error_category, and hint."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _bad_request(message: str, hint: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=ApiErrorResponse(
            error_code=ErrorCode.VALIDATION_ERROR,
            error_category=ErrorCategory.validation,
            message=message,
            hint=hint,
        ).model_dump(),
    )


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
    profile: str = Form(
        "novel",
        description="Built-in extraction profile name (see `pdf2duckdb profiles`)",
    ),
    request: str | None = Form(
        None,
        description="Optional extra guidance for the extractor, added to every chunk prompt",
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

    # Only built-in profiles: a file path would let clients read files on the server.
    if "/" in profile or "\\" in profile or profile.endswith((".yaml", ".yml")):
        raise _bad_request("profile must be a built-in profile name", "e.g. 'novel'")
    try:
        load_profile(profile)
    except ValueError as exc:
        raise _bad_request(str(exc), "Use a built-in profile name, e.g. 'novel'") from exc

    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise _bad_request("Upload must be a .pdf file", "Choose a file ending in .pdf")

    content = await pdf.read()
    if not content:
        raise _bad_request("Uploaded PDF is empty", "Upload a non-empty PDF file")

    job = create_job(
        profile=profile,
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

    db_ready = job.status == "completed" and job.db_path is not None and job.db_path.exists()
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
