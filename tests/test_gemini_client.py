from __future__ import annotations

from pathlib import Path

import pytest

from utils import llm_client


def test_gemini_client_uses_configured_request_timeout(monkeypatch) -> None:
    created = {}

    class FakeHttpOptions:
        def __init__(self, timeout) -> None:
            self.timeout = timeout

    class FakeTypes:
        HttpOptions = FakeHttpOptions

    class FakeGenai:
        class Client:
            def __init__(self, **kwargs) -> None:
                created.update(kwargs)

    monkeypatch.setattr(llm_client.config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(llm_client.config, "GEMINI_REQUEST_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(llm_client, "_get_gemini_modules", lambda: (FakeGenai, FakeTypes, object()))

    llm_client._gemini_client()

    assert created["api_key"] == "gemini-key"
    assert created["http_options"].timeout == 7000


def test_gemini_timeout_enters_retry_path(monkeypatch) -> None:
    calls = {"count": 0, "sleeps": []}

    class FakeResponse:
        text = '{"ok": true}'

    class FakeModels:
        def generate_content(self, **kwargs) -> FakeResponse:
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("timed out")
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    class FakeTypes:
        class Content:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class Part:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        class GenerateContentConfig:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

    class FakeGoogleExceptions:
        class ResourceExhausted(Exception):
            pass

        class ServiceUnavailable(Exception):
            pass

    monkeypatch.setattr(llm_client, "_get_gemini_modules", lambda: (object(), FakeTypes, FakeGoogleExceptions))
    monkeypatch.setattr(llm_client, "_gemini_client", lambda: FakeClient())
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: calls["sleeps"].append(seconds))

    result = llm_client._call_gemini_text("{}", max_retries=2)

    assert result == '{"ok": true}'
    assert calls == {"count": 2, "sleeps": [5]}


def test_gemini_missing_genai_module_message_names_module(monkeypatch) -> None:
    def fake_import_module(name: str):
        raise ImportError("missing", name=name)

    monkeypatch.setattr(llm_client.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match="google.genai"):
        llm_client._get_gemini_modules()


def test_gemini_missing_api_core_module_message_names_module(monkeypatch) -> None:
    class FakeGenai:
        pass

    class FakeTypes:
        pass

    def fake_import_module(name: str):
        if name == "google.genai":
            return FakeGenai
        if name == "google.genai.types":
            return FakeTypes
        raise ImportError("missing", name=name)

    monkeypatch.setattr(llm_client.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match="google.api_core.exceptions"):
        llm_client._get_gemini_modules()


def test_requirements_lists_google_api_core() -> None:
    body = Path("requirements.txt").read_text(encoding="utf-8")

    assert "google-genai" in body
    assert "google-api-core" in body
