"""Map pipeline and provider exceptions to structured API error fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError

from api.schemas import ErrorCategory, ErrorCode


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    category: ErrorCategory
    message: str
    retryable: bool = False
    phase: str | None = None
    hint: str | None = None
    http_status: int | None = None


def classify_error(exc: BaseException, *, phase: str | None = None) -> ErrorInfo:
    """Turn an exception into stable error_code / category fields for the API."""
    message = str(exc).strip() or exc.__class__.__name__

    if isinstance(exc, ValueError):
        return _classify_value_error(exc, message, phase=phase)

    if isinstance(exc, ModelHTTPError):
        return _classify_model_http_error(exc, message, phase=phase)

    error_type = exc.__class__.__name__
    module = getattr(exc.__class__, "__module__", "")

    if error_type in {"APIConnectionError", "ConnectError"} or "connection" in message.lower():
        return ErrorInfo(
            code=ErrorCode.NETWORK_ERROR,
            category=ErrorCategory.network,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            hint="Check your internet connection and retry the job.",
        )

    if error_type in {"APITimeoutError", "TimeoutError"} or "timeout" in message.lower():
        return ErrorInfo(
            code=ErrorCode.REQUEST_TIMEOUT,
            category=ErrorCategory.network,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            hint="The model provider took too long. Retry or use a faster model.",
        )

    if error_type == "RateLimitError" or "rate limit" in message.lower():
        return ErrorInfo(
            code=ErrorCode.RATE_LIMITED,
            category=ErrorCategory.provider,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            hint="OpenRouter rate limit hit. Wait and retry.",
        )

    if "401" in message or "unauthorized" in message.lower():
        return ErrorInfo(
            code=ErrorCode.OPENROUTER_UNAUTHORIZED,
            category=ErrorCategory.auth,
            message=message,
            retryable=False,
            phase=phase,
            hint="Check OPENROUTER_API_KEY in .env and restart the server.",
        )

    if "402" in message or "credit" in message.lower() or "payment" in message.lower():
        return ErrorInfo(
            code=ErrorCode.OPENROUTER_PAYMENT_REQUIRED,
            category=ErrorCategory.billing,
            message=message,
            retryable=False,
            phase=phase,
            hint="Add credits at https://openrouter.ai/",
        )

    if "404" in message and ("model" in message.lower() or "not found" in message.lower()):
        return ErrorInfo(
            code=ErrorCode.MODEL_NOT_FOUND,
            category=ErrorCategory.model,
            message=message,
            retryable=False,
            phase=phase,
            hint="Fix OPENROUTER_MODEL in .env to a valid OpenRouter model id.",
        )

    if module.startswith("openai") or module.startswith("httpx"):
        return ErrorInfo(
            code=ErrorCode.PROVIDER_ERROR,
            category=ErrorCategory.provider,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            hint="Transient OpenRouter/OpenAI client error. Retry the job.",
        )

    return ErrorInfo(
        code=ErrorCode.UNKNOWN,
        category=ErrorCategory.internal,
        message=message,
        retryable=False,
        phase=phase,
        hint="See server logs for the full traceback.",
    )


def _classify_value_error(
    exc: ValueError,
    message: str,
    *,
    phase: str | None,
) -> ErrorInfo:
    if "OPENROUTER_API_KEY" in message or "Missing OPENROUTER" in message:
        return ErrorInfo(
            code=ErrorCode.MISSING_API_KEY,
            category=ErrorCategory.auth,
            message=message,
            retryable=False,
            phase=phase or "startup",
            hint="Copy .env.example to .env, add your key, restart uvicorn.",
        )

    if "OPENROUTER_TEMPERATURE" in message or "OPENROUTER_WRAPUP_TEMPERATURE" in message:
        return ErrorInfo(
            code=ErrorCode.INVALID_SETTINGS,
            category=ErrorCategory.configuration,
            message=message,
            retryable=False,
            phase=phase or "startup",
            hint="Fix temperature values in .env (must be 0.0–2.0).",
        )

    if "Cannot --resume" in message:
        if "no checkpoints" in message:
            return ErrorInfo(
                code=ErrorCode.RESUME_CHECKPOINT_MISMATCH,
                category=ErrorCategory.configuration,
                message=message,
                retryable=False,
                phase=phase or "database_setup",
                hint=(
                    "Leave resume unchecked for a new job, or delete the job folder "
                    "under output/jobs/ and POST again."
                ),
            )
        return ErrorInfo(
            code=ErrorCode.RESUME_RUN_MISMATCH,
            category=ErrorCategory.configuration,
            message=message,
            retryable=False,
            phase=phase or "database_setup",
            hint="Resume only works when PDF and chunk settings match the saved run.",
        )

    return ErrorInfo(
        code=ErrorCode.VALIDATION_ERROR,
        category=ErrorCategory.validation,
        message=message,
        retryable=False,
        phase=phase,
    )


def _classify_model_http_error(
    exc: ModelHTTPError,
    message: str,
    *,
    phase: str | None,
) -> ErrorInfo:
    status = exc.status_code
    retryable = status in {400, 408, 429, 500, 502, 503, 504}

    if status == 401:
        return ErrorInfo(
            code=ErrorCode.OPENROUTER_UNAUTHORIZED,
            category=ErrorCategory.auth,
            message=message,
            retryable=False,
            phase=phase or "chunk_extraction",
            http_status=status,
            hint="Invalid or revoked OPENROUTER_API_KEY.",
        )

    if status == 402:
        return ErrorInfo(
            code=ErrorCode.OPENROUTER_PAYMENT_REQUIRED,
            category=ErrorCategory.billing,
            message=message,
            retryable=False,
            phase=phase or "chunk_extraction",
            http_status=status,
            hint="Add OpenRouter credits.",
        )

    if status == 404:
        return ErrorInfo(
            code=ErrorCode.MODEL_NOT_FOUND,
            category=ErrorCategory.model,
            message=message,
            retryable=False,
            phase=phase or "chunk_extraction",
            http_status=status,
            hint="OPENROUTER_MODEL is not available on OpenRouter.",
        )

    if status == 429:
        return ErrorInfo(
            code=ErrorCode.RATE_LIMITED,
            category=ErrorCategory.provider,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            http_status=status,
            hint="Rate limited. Wait and retry.",
        )

    if status in {500, 502, 503, 504}:
        return ErrorInfo(
            code=ErrorCode.UPSTREAM_ERROR,
            category=ErrorCategory.provider,
            message=message,
            retryable=True,
            phase=phase or "chunk_extraction",
            http_status=status,
            hint="OpenRouter or model provider error. Safe to retry.",
        )

    return ErrorInfo(
        code=ErrorCode.PROVIDER_ERROR,
        category=ErrorCategory.provider,
        message=message,
        retryable=retryable,
        phase=phase or "chunk_extraction",
        http_status=status,
        hint="OpenRouter HTTP error during extraction.",
    )


def error_info_to_dict(info: ErrorInfo) -> dict[str, Any]:
    return {
        "error_code": info.code.value,
        "error_category": info.category.value,
        "retryable": info.retryable,
        "phase": info.phase,
        "hint": info.hint,
        "http_status": info.http_status,
    }
