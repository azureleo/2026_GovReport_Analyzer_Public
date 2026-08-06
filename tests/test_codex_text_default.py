"""codex 모드 텍스트 기본 모델(CODEX_TEXT_MODEL) 채택 회귀 테스트."""

import config
from utils.llm_client import _codex_default_model, _model_identity


def test_텍스트단계_기본은_luna다(monkeypatch) -> None:
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "")
    monkeypatch.setattr(config, "CODEX_TEXT_MODEL", "gpt-5.6-luna")
    assert _codex_default_model(None) == "gpt-5.6-luna"
    assert _codex_default_model("extraction") == "gpt-5.6-luna"


def test_사용자_agent_model이_기본보다_우선한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "gpt-5.6-terra")
    monkeypatch.setattr(config, "CODEX_TEXT_MODEL", "gpt-5.6-luna")
    assert _codex_default_model(None) == "gpt-5.6-terra"


def test_vision단계는_기존_CODEX_VISION_MODEL_경로를_유지한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "")
    monkeypatch.setattr(config, "CODEX_VISION_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(config, "CODEX_TEXT_MODEL", "다른값")
    assert _codex_default_model("vision") == "gpt-5.6-luna"


def test_캐시키가_기본_텍스트모델을_반영한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "")
    monkeypatch.setattr(config, "CODEX_TEXT_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(config, "CODEX_COMMAND", "codex")
    monkeypatch.setattr(
        config,
        "STAGE_MODELS",
        {**config.STAGE_MODELS, "extraction": ""},
    )
    assert _model_identity("codex", "extraction") == "codex:gpt-5.6-luna"


def test_빈_기본값이면_CLI_기본_모델로_폴백한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "")
    monkeypatch.setattr(config, "CODEX_TEXT_MODEL", "")
    assert _codex_default_model(None) == ""
