"""v7-3 vision fields 공급 지침 회귀 테스트."""

from agents.image_agent import CHART_TABLE_SYSTEM


def test_vision_fields는_명시된_분류근거를_반드시_공급하고_추측은_금지한다() -> None:
    caution = (
        "확신이 없으면 그 필드를 생략하세요(추측 금지)."
    )
    explicit_basis = (
        "차트 제목·축 라벨·범례·캡션에 필수 분류 근거가 명시돼 있으면 "
        "해당 필드를 생략하지 말고 반드시 fields에 포함하세요."
    )

    assert caution in CHART_TABLE_SYSTEM
    assert explicit_basis in CHART_TABLE_SYSTEM
    assert CHART_TABLE_SYSTEM.index(caution) < CHART_TABLE_SYSTEM.index(explicit_basis)
