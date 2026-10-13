# HTTP API

The API runs the same pipeline as the CLI, with one background job per upload. It's meant for local or internal use: there is no authentication, and jobs run in-process.

```bash
pip install -e ".[api]"
uvicorn pdf2duckdb.api.main:app --reload --host 127.0.0.1 --port 8000
```

Interactive docs: http://127.0.0.1:8000/docs

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness check |
| `POST` | `/jobs` | Upload a PDF and start a job. Returns `job_id` (HTTP 202). |
| `GET` | `/jobs/{job_id}` | Job status: `queued`, `running`, `completed` or `failed` |
| `GET` | `/jobs/{job_id}/db` | Download the DuckDB file once the job has completed |

`POST /jobs` takes multipart form fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `pdf` | required | The PDF file |
| `profile` | `novel` | Name of a built-in profile. File paths are rejected. |
| `request` | none | Extra guidance added to every chunk prompt |
| `resume` | `false` | Continue a previous run in the same job folder |
| `skip_wrap_up` | `false` | Leave disputes open |

Each job writes `input.pdf`, `extracted.db` and `meta.json` under `output/jobs/{job_id}/`. Set `API_JOBS_DIR` to change the location.

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -F "pdf=@examples/a-scandal-in-bohemia.pdf" -F "profile=novel"
curl http://127.0.0.1:8000/jobs/JOB_ID
curl -OJ http://127.0.0.1:8000/jobs/JOB_ID/db
```

## Errors

`POST /jobs` returns **400** before creating a job if the configuration is invalid (e.g. `OPENROUTER_API_KEY` is missing), the profile is unknown, or the upload is not a non-empty PDF.

When a job's `status` is `failed`, `GET /jobs/{job_id}` includes structured fields, which are also saved in `meta.json`:

| Field | Meaning |
|-------|---------|
| `error_code` | Stable id, e.g. `missing_api_key`, `openrouter_unauthorized`, `model_not_found`, `rate_limited`, `upstream_error` |
| `error_category` | `configuration`, `auth`, `billing`, `model`, `provider`, `network`, `validation` or `internal` |
| `retryable` | `true` if retrying may succeed; `false` means fix the configuration first |
| `phase` | Where it failed, e.g. `startup`, `database_setup`, `chunk_extraction` |
| `hint` | Suggested fix |
| `http_status` | Upstream HTTP status from OpenRouter, when applicable |

## Docker

```bash
cp .env.example .env   # add your key; never commit .env
docker compose up --build
```

The container serves the API on port 8000. Job files are stored in the `job-data` volume (`/data/jobs` inside the container). If port 8000 is taken, change `ports` in `docker-compose.yml` to e.g. `"8001:8000"`.

To run without Compose:

```bash
docker build -t pdf2duckdb .
docker run --rm -p 8000:8000 --env-file .env -v pdf2duckdb-jobs:/data/jobs pdf2duckdb
```
