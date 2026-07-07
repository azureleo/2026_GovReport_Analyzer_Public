"""vision 단계의 Gemini 기본 모델(pro) 채택 회귀 테스트.

벤치마크 근거(docs/benchmark/서울시각요소_VLM벤치마크_v1.md)로 API 모드의 vision
기본만 gemini-2.5-pro로 올린 결정을 고정한다. 핵심 계약: 캐시 키(_model_identity)와
실제 호출 모델이 어떤 stage 조합에서도 일치해야 한다.
"""
from __future__ import annotations

from utils import llm_client


def _reset_stage(monkeypatch) -> None:
    monkeypatch.setattr(llm_client.config, "STAGE_MODELS", {"extraction": "", "vision": "", "gap_fill": "", "review": ""})
    monkeypatch.setattr(llm_client.config, "STAGE_PROVIDERS", {"extraction": "", "vision": "", "gap_fill": "", "review": ""})


def test_vision_스테이지_기본은_pro이고_다른_스테이지는_기존_모델이다(monkeypatch) -> None:
    _reset_stage(monkeypatch)
    monkeypatch.setattr(llm_client.config, "MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setattr(llm_client.config, "GEMINI_VISION_MODEL", "gemini-2.5-pro")

    assert llm_client._model_identity("gemini", "vision") == "gemini-2.5-pro"
    assert llm_client._model_identity("gemini", "extraction") == "gemini-2.5-flash-lite"
    assert llm_client._model_identity("gemini", None) == "gemini-2.5-flash-lite"


def test_STAGE_MODEL_VISION이_GEMINI_VISION_MODEL보다_우선한다(monkeypatch) -> None:
    _reset_stage(monkeypatch)
    monkeypatch.setattr(llm_client.config, "GEMINI_VISION_MODEL", "gemini-2.5-pro")
    monkeypatch.setattr(
        llm_client.config, "STAGE_MODELS",
        {"extraction": "", "vision": "custom-vision", "gap_fill": "", "review": ""},
    )
    assert llm_client._model_identity("gemini", "vision") == "custom-vision"


def test_GEMINI_VISION_MODEL이_비면_기존_MODEL로_폴백한다(monkeypatch) -> None:
    _reset_stage(monkeypatch)
    monkeypatch.setattr(llm_client.config, "MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setattr(llm_client.config, "GEMINI_VISION_MODEL", "")
    assert llm_client._model_identity("gemini", "vision") == "gemini-2.5-flash-lite"


def test_vision_호출_모델이_캐시_키와_일치한다(monkeypatch) -> None:
    # Given: gemini 백엔드 + 캐시 비활성 + vision 스테이지 기본(pro)
    _reset_stage(monkeypatch)
    monkeypatch.setattr(llm_client.config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm_client.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_client.config, "LLM_CACHE_ENABLED", False)
    monkeypatch.setattr(llm_client.config, "MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setattr(llm_client.config, "GEMINI_VISION_MODEL", "gemini-2.5-pro")

    captured: dict[str, object] = {}

    def fake_vision(image_b64, prompt, system="", max_retries=3, model=None):
        captured["model"] = model
        return "{}"

    def fake_vision_batch(images_b64, prompt, system="", max_retries=3, model=None):
        captured["batch_model"] = model
        return "{}"

    monkeypatch.setattr(llm_client, "_call_gemini_vision", fake_vision)
    monkeypatch.setattr(llm_client, "_call_gemini_vision_batch", fake_vision_batch)

    # When: 단건·배치 vision 호출
    llm_client.call_vision("aGk=", "차트를 읽어라", stage="vision")
    llm_client.call_vision_batch(["aGk=", "aGk="], "차트를 읽어라", stage="vision")

    # Then: 실제 호출 모델 == 캐시 키 모델(_model_identity)
    assert captured["model"] == llm_client._model_identity("gemini", "vision") == "gemini-2.5-pro"
    assert captured["batch_model"] == "gemini-2.5-pro"
