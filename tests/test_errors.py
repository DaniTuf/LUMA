"""The error catalog, its documentation, and what students / API clients can see when something fails."""
import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend import db, errors
from backend.main import app
from sdb import reset_database

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_every_code_has_a_message_in_all_five_languages():
    for code, (status, retry, msgs) in errors.CATALOG.items():
        assert set(msgs) == {"en", "ur", "ar", "fa", "de"}, code
        assert all(m.strip() for m in msgs.values()), code
        assert isinstance(status, int) and isinstance(retry, bool), code


def test_error_messages_never_contain_technical_details():
    for code, (_, _, msgs) in errors.CATALOG.items():
        for lang, m in msgs.items():
            assert not re.search(r"(traceback|exception|sql|postgres|redis|gemini|groq|anthropic|openai|api key|stack|asyncpg|sqlalchemy)", m, re.I), (code, lang)


def test_errors_md_documents_every_code():
    doc = open(os.path.join(ROOT, "ERRORS.md"), encoding="utf-8").read()
    missing = [c for c in errors.CATALOG if f"`{c}`" not in doc]
    assert not missing, f"ERRORS.md does not mention: {missing}"


def test_unknown_codes_fall_back_to_internal():
    assert errors.LumaError("NOPE").code == "INTERNAL"
    assert errors.message("NOPE", "xx") == errors.CATALOG["INTERNAL"][2]["en"]
    assert errors.message("RATE_LIMIT_DAILY", "ur") != errors.message("RATE_LIMIT_DAILY", "en")


@pytest.mark.parametrize("exc,code", [
    (OperationalError("SELECT 1", {}, Exception("refused")), "DB_UNAVAILABLE"),
    (db.DatabaseUnavailable("x"), "DB_UNAVAILABLE"),
    (TimeoutError(), "LLM_UNAVAILABLE"),
    (ConnectionResetError(), "LLM_UNAVAILABLE"),
    (ValueError("bug"), "INTERNAL"),
    (errors.LumaError("FORBIDDEN"), "FORBIDDEN"),
])
def test_classify(exc, code):
    assert errors.classify(exc) == code


@pytest.fixture()
def client():
    reset_database()
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/auth/dev", json={"email": "tester@tuf.edu.pk"}).status_code == 200
    return c


def test_http_errors_have_a_code_a_kind_message_and_a_request_id(client):
    r = client.get("/api/conversations/does-not-exist")
    assert r.status_code == 404
    j = r.json()
    assert j["error"]["code"] == "NOT_FOUND" and j["detail"] and j["error"]["request_id"] == r.headers["X-Request-ID"]
    r = client.post("/api/chat", json={"message": "x" * 4001})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MESSAGE_TOO_LONG"
    r = TestClient(app).get("/api/conversations")
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_REQUIRED"
    assert client.post("/api/chat", json={"message": "hi", "level": "wizard"}).json()["error"]["code"] == "BAD_REQUEST"


def test_the_students_language_header_picks_the_message_language(client):
    r = client.get("/api/conversations/nope", headers={"X-Language": "ur"})
    assert r.json()["error"]["message"] == errors.message("NOT_FOUND", "ur")


def test_a_database_outage_is_a_503_with_a_friendly_message_not_a_traceback(client, monkeypatch):
    async def down(*a, **k):
        raise db.DatabaseUnavailable("OperationalError")
    monkeypatch.setattr(db, "list_conversations", down)
    r = client.get("/api/conversations")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DB_UNAVAILABLE" and r.json()["error"]["retryable"] is True
    assert "OperationalError" not in r.text and "Traceback" not in r.text


def test_an_unexpected_bug_is_a_500_that_leaks_nothing(client, monkeypatch):
    async def bug(*a, **k):
        raise RuntimeError("secret internal detail key=AIzaSECRET")
    monkeypatch.setattr(db, "list_conversations", bug)
    r = client.get("/api/conversations")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL"
    assert "secret internal detail" not in r.text and "AIzaSECRET" not in r.text


def test_validation_errors_are_structured(client):
    r = client.post("/api/chat", json={"nope": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_REQUEST"


def test_the_failure_is_recorded_with_its_code_and_request_id(client, monkeypatch):
    import asyncio
    from backend import state

    async def bug(*a, **k):
        raise RuntimeError("boom")
    state.reset_memory()
    monkeypatch.setattr(db, "list_conversations", bug)
    r = client.get("/api/conversations")
    rows = asyncio.run(state.recent_failures(5))
    assert rows[0]["code"] == "INTERNAL" and rows[0]["request"] == r.headers["X-Request-ID"] and rows[0]["component"] == "http"


def test_health_reports_the_database(client):
    j = client.get("/api/health").json()
    assert j["ok"] is True and j["database"]["connected"] is True
