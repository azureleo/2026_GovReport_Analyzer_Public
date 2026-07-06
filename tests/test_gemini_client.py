from __future__ import annotations

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
