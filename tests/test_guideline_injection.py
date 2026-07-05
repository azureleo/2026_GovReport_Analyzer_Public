from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from agents import guideline_agent as guideline_module
from agents.guideline_agent import GuidelineAgent


def _use_guideline(monkeypatch: pytest.MonkeyPatch, path: Path, *, max_chars: int = 3000) -> None:
    monkeypatch.setattr(guideline_module, "_MARKDOWN_GUIDELINE_PATH", path)
    monkeypatch.setattr(config, "GUIDELINE_STRUCTURED_INJECTION", True, raising=False)
    monkeypatch.setattr(config, "GUIDELINE_PROMPT_MAX_CHARS", max_chars, raising=False)


def test_structured_prompt_uses_sheet_anchor_and_preserves_markdown_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given: 시트 앵커가 달린 markdown 섹션과 표가 있는 구조화 가이드라인
    guideline = tmp_path / "carbon_guideline.md"
    guideline.write_text(
        """# 테스트 가이드라인

### 2.2 지역 환경요인 분석

<!-- sheets: regional_conditions -->

지역 여건 본문입니다.

| 범주 | 값 |
|---|---|
| 자연환경 | 최근 5년 |

### 2.3 온실가스 배출 현황

<!-- sheets: emissions_regional -->

배출 현황 본문입니다.
""",
        encoding="utf-8",
    )
    _use_guideline(monkeypatch, guideline)

    # When: 구조적 주입 프롬프트를 만들면
    prompt = GuidelineAgent().get_all_prompts()["regional_conditions"]

    # Then: 해당 앵커 섹션만 포함하고 markdown 표 개행을 그대로 보존한다.
    assert prompt.startswith("[가이드라인 보조 지침: regional_conditions]")
    assert "지역 여건 본문입니다." in prompt
    assert "배출 현황 본문입니다." not in prompt
    assert "| 범주 | 값 |\n|---|---|\n| 자연환경 | 최근 5년 |" in prompt
    assert "<!-- sheets:" not in prompt


def test_prompt_limit_keeps_whole_sections_and_skips_oversized_tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given: 1순위 섹션 2개, 상한 초과 컬럼 표, 관련 §7 매핑 규칙이 있는 가이드라인
    long_table = "\n".join(["| 컬럼 | 설명 |", "|---|---|", *[f"| 매우긴컬럼{i} | {'x' * 40} |" for i in range(12)]])
    guideline = tmp_path / "carbon_guideline.md"
    guideline.write_text(
        f"""# 테스트 가이드라인

### 2.2 지역 환경요인 분석

<!-- sheets: regional_conditions -->

첫 번째 지역 여건 섹션입니다.

### 2.2.1 지역 환경요인 추가

<!-- sheets: regional_conditions -->

두 번째 지역 여건 섹션입니다. 이 문장은 상한 때문에 통째로 제외되어야 합니다. {'y' * 160}

### 5.3 시트별 세부 컬럼 가이드

#### `02_regional_conditions`

<!-- sheets: regional_conditions -->

{long_table}

## 7. 보고서가 가이드라인 형식을 따르지 않을 때의 매핑 규칙

1. **목차명이 다르면 의미 기준으로 매핑**한다. 예를 들어 “도시 여건”, “지역 여건”, “현황 분석”은 `regional_conditions`에 매핑한다.
""",
        encoding="utf-8",
    )
    _use_guideline(monkeypatch, guideline, max_chars=260)

    # When: 작은 상한으로 프롬프트를 만들면
    prompt = GuidelineAgent().get_all_prompts()["regional_conditions"]

    # Then: 섹션은 중간 절단되지 않고, 초과 표는 통째로 제외한 뒤 다음 우선순위로 진행한다.
    assert len(prompt) <= 260
    assert "첫 번째 지역 여건 섹션입니다." in prompt
    assert "두 번째 지역 여건 섹션입니다." not in prompt
    assert "매우긴컬럼" not in prompt
    assert "목차명이 다르면 의미 기준으로 매핑" in prompt


def test_missing_markdown_uses_hwp_keyword_snippet_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given: markdown이 없고 --guideline HWP 경로만 제공되는 상황
    missing_markdown = tmp_path / "missing.md"
    hwp_path = tmp_path / "guide.hwp"
    hwp_path.write_text("dummy", encoding="utf-8")
    _use_guideline(monkeypatch, missing_markdown)
    monkeypatch.setattr(guideline_module, "is_hwp_file", lambda path: True)
    monkeypatch.setattr(
        guideline_module,
        "extract_hwp",
        lambda path: SimpleNamespace(full_text="지역 여건 인구 면적 에너지\n온실가스 배출량 GIR 인벤토리"),
    )

    # When: 가이드라인 에이전트를 실행하면
    prompts = GuidelineAgent(hwp_path=str(hwp_path)).get_all_prompts()

    # Then: 앵커가 없는 HWP도 키워드 스니펫 fallback으로 보조 지침을 만든다.
    assert "지역 여건 인구 면적 에너지" in prompts["regional_conditions"]
    assert "온실가스 배출량 GIR 인벤토리" in prompts["emissions_regional"]


def test_actual_guideline_generates_non_empty_prompts_for_all_extraction_sheets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: WP1 앵커가 반영된 실제 carbon_guideline.md
    monkeypatch.setattr(config, "GUIDELINE_STRUCTURED_INJECTION", True, raising=False)
    monkeypatch.setattr(config, "GUIDELINE_PROMPT_MAX_CHARS", 3000, raising=False)

    # When: 전체 프롬프트를 만들면
    prompts = GuidelineAgent().get_all_prompts()

    # Then: 16개 추출 시트 키 모두 비어 있지 않다.
    missing = [sheet_key for sheet_key in config.EXTRACTION_SHEETS if not prompts.get(sheet_key, "").strip()]
    assert missing == []


def test_removed_legacy_guideline_symbols_are_not_available() -> None:
    # Given/When/Then: WP2에서 제거하기로 한 v6 이전 구 시트·로컬 에이전트 경로가 없다.
    removed_module_symbols = [
        "GUIDELINE" + "_SCHEMA",
        "_EXTRACTOR" + "_KEY_MAP",
        "_GUIDELINE" + "_CONTEXT_RULES",
        "_AGENT" + "_SPEC_SYSTEM",
    ]
    removed_methods = ["get_extraction" + "_prompt", "_build_agent" + "_spec"]

    assert [name for name in removed_module_symbols if hasattr(guideline_module, name)] == []
    assert [name for name in removed_methods if hasattr(GuidelineAgent, name)] == []
