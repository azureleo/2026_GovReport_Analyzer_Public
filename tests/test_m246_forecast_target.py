from __future__ import annotations

import pytest

from agents.image_agent import CHART_TABLE_SYSTEM, ImageAgent


@pytest.mark.parametrize(
    ("target_sheet", "expected"),
    [
        ("forecast", "emissions_forecast"),
        ("target", "reduction_targets"),
    ],
)
def test_전망과_감축목표_legacy_target_sheet를_신규_시트로_매핑한다(
    target_sheet: str,
    expected: str,
) -> None:
    assert ImageAgent()._infer_target_sheet({"target_sheet": target_sheet}) == expected


def test_차트표_프롬프트에_전망과_감축목표_분류_규칙을_명시한다() -> None:
    assert (
        "vehicle, energy, ghg, forecast, target, strategy, summary 중 하나"
        in CHART_TABLE_SYSTEM
    )
    assert "배출·흡수 전망" in CHART_TABLE_SYSTEM
    assert "기후 시나리오(SSP·RCP 등의 기온·강수 전망)는 forecast가 아니라 summary" in CHART_TABLE_SYSTEM
    assert "감축목표 차트" in CHART_TABLE_SYSTEM
    assert "배출전망(forecast)" in CHART_TABLE_SYSTEM
    assert "차트·캡션의 시나리오 표기 그대로(BAU|목표|전망 등), 없으면 생략" in CHART_TABLE_SYSTEM
    assert "감축목표(target)" in CHART_TABLE_SYSTEM
    assert "LEAP" not in CHART_TABLE_SYSTEM


def test_차트표_프롬프트의_estimated_true_only_관례를_보존한다() -> None:
    assert (
        '7. 막대/선의 값이 축 눈금만으로 추정된 값이면 fields에 {"estimated": true}를 넣고 '
        "confidence는 medium 이하로 두세요."
        in CHART_TABLE_SYSTEM
    )
