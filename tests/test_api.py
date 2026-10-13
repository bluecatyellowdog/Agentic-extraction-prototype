import pytest
from fastapi.testclient import TestClient

from pdf2duckdb.api.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("pdf2duckdb.api.jobs.JOBS_ROOT", tmp_path / "jobs")
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_rejects_unknown_profile(client):
    response = client.post(
        "/jobs",
        data={"profile": "nope"},
        files={"pdf": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 400
    assert "Unknown profile" in response.json()["detail"]["message"]


def test_rejects_profile_paths(client):
    response = client.post(
        "/jobs",
        data={"profile": "../secrets.yaml"},
        files={"pdf": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 400


def test_rejects_non_pdf(client):
    response = client.post("/jobs", files={"pdf": ("a.txt", b"hello", "text/plain")})
    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "Upload must be a .pdf file"


def test_missing_api_key_is_reported(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    response = client.post("/jobs", files={"pdf": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "missing_api_key"


def test_unknown_job_is_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404
