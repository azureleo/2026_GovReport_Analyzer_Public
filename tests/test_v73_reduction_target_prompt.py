"""v7-3 06_감축목표 추출 프롬프트 회귀 테스트."""

from agents.extractor_agent import _SHEET_CONFIGS


def test_06_프롬프트가_목표수준_5코드와_사업명힌트를_요구한다() -> None:
    prompt = _SHEET_CONFIGS["reduction_targets"]["prompt"]

    assert '"목표수준": "총괄|부문|세부부문|세부사업|연차경로"' in prompt
    assert '"사업명힌트": "세부사업일 때 해당 사업명, 그 외 생략"' in prompt


def test_06_프롬프트가_카드와_연차경로를_구분하고_범위_추측을_금지한다() -> None:
    prompt = _SHEET_CONFIGS["reduction_targets"]["prompt"]

    assert "개별 사업·과제 카드" in prompt
    assert "부문 전체 목표 표" in prompt
    assert "연차별 감축량·감축률 경로표" in prompt
    assert "5년 단위 이정표 목표 표는 해당 없음" in prompt
    assert "목표수준은 항상 기입" in prompt
    assert "목표범위는 원문 근거가 불확실하면 생략(추측 금지)" in prompt
