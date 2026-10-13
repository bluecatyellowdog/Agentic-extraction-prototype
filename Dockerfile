FROM python:3.12-slim-bookworm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[api]"

ENV PYTHONUNBUFFERED=1 \
    API_JOBS_DIR=/data/jobs

RUN mkdir -p /data/jobs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "pdf2duckdb.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
