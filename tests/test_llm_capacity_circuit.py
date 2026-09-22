from pathlib import Path

import pytest

from utils import llm_client
from utils.llm_cache import LLMCacheRequest, cached_response


def _configure(monkeypatch, *, fallback: str = "") -> None:
    monkeypatch.setattr(llm_client.config, "LLM_CACHE_ENABLED", False)
    monkeypatch.setattr(llm_client.config, "LLM_SINGLEFLIGHT_ENABLED", True)
    monkeypatch.setattr(llm_client.config, "LLM_CAPACITY_CIRCUIT_ENABLED", True)
    monkeypatch.setattr(llm_client.config, "LLM_CAPACITY_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(llm_client.config, "LLM_CAPACITY_COOLDOWN_SECONDS", 120)
    monkeypatch.setattr(llm_client.config, "LLM_CAPACITY_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        llm_client.config,
        "STAGE_FALLBACK_MODELS",
        {"extraction": fallback, "vision": "", "gap_fill": "", "review": ""},
    )
    llm_client.reset_llm_stats()


def test_run_command_classifies_model_capacity(monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Selected model is at capacity. Please try a different model."

    monkeypatch.setattr(llm_client.subprocess, "run", lambda *args, **kwargs: Completed())

    with pytest.raises(llm_client.LLMCapacityError, match="at capacity"):
        llm_client._run_command(["fake"], "prompt", cwd=Path("."), timeout=5)


def test_capacity_failure_switches_to_configured_stage_fallback(monkeypatch) -> None:
    _configure(monkeypatch, fallback="gpt-fallback")
    calls: list[str] = []

    def fake_local_agent(prompt, system, **kwargs):
        model = str(kwargs.get("model") or "")
        calls.append(model)
        if model == "gpt-primary":
            raise llm_client.LLMCapacityError("Selected model is at capacity")
        return '{"ok": true, "model": "gpt-fallback"}'

    monkeypatch.setattr(llm_client, "_call_local_agent", fake_local_agent)

    result = llm_client._call_local_with_capacity(
        kind="text",
        provider="codex",
        stage="extraction",
        primary_model="gpt-primary",
        prompt="extract",
        system="system",
        max_retries=3,
    )

    assert result == '{"ok": true, "model": "gpt-fallback"}'
    assert calls == ["gpt-primary", "gpt-fallback"]
    stats = llm_client.get_llm_stats()
    assert stats["capacity_errors"] == 1
    assert stats["capacity_circuit_opened"] == 1
    assert stats["capacity_fallback_calls"] == 1
    assert stats["capacity_fallback_successes"] == 1
    assert stats["capacity_fallbacks"] == {
        "extraction:gpt-primary->gpt-fallback": 1,
    }


def test_open_capacity_circuit_rejects_followup_without_calling_model(monkeypatch) -> None:
    _configure(monkeypatch)
    calls = 0

    def fake_local_agent(prompt, system, **kwargs):
        nonlocal calls
        calls += 1
        raise llm_client.LLMCapacityError("Selected model is at capacity")

    monkeypatch.setattr(llm_client, "_call_local_agent", fake_local_agent)
    kwargs = {
        "kind": "text",
        "provider": "codex",
        "stage": "extraction",
        "primary_model": "gpt-primary",
        "system": "system",
        "max_retries": 3,
    }

    with pytest.raises(llm_client.LLMCapacityError):
        llm_client._call_local_with_capacity(prompt="first", **kwargs)
    with pytest.raises(llm_client.LLMCapacityError, match="회로가 열려"):
        llm_client._call_local_with_capacity(prompt="second", **kwargs)

    assert calls == 1
    assert llm_client.get_llm_stats()["capacity_circuit_rejected"] == 1


def test_open_circuit_still_uses_existing_primary_cache(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, fallback="gpt-fallback")
    monkeypatch.setattr(llm_client.config, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(llm_client.config, "LLM_CACHE_DIR", str(tmp_path / "cache"))
    request = LLMCacheRequest(
        call_kind="text",
        provider="codex",
        model=llm_client._local_model_identity("codex", "gpt-primary"),
        system="system",
        prompt="cached",
    )
    cached_response(request, lambda: '{"cached": true}')
    llm_client._capacity_record_failure("codex", "gpt-primary", "extraction")
    calls = 0

    def should_not_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("cached primary response should win")

    monkeypatch.setattr(llm_client, "_call_local_agent", should_not_call)

    result = llm_client._call_local_with_capacity(
        kind="text",
        provider="codex",
        stage="extraction",
        primary_model="gpt-primary",
        prompt="cached",
        system="system",
        max_retries=1,
    )

    assert result == '{"cached": true}'
    assert calls == 0
