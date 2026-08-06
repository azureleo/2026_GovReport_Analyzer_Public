from __future__ import annotations

import json

import pytest

import config
from agents.organizer_agent import OrganizerAgent


def _관리권한_관찰값(
    *,
    value: float = 100.0,
    confidence: str = "high",
    fields: dict | None = None,
    title: str = "관리권한 배출량",
):
    판독필드 = {
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접",
    }
    if fields is not None:
        판독필드 = fields
    return {
        "지자체명": "서울특별시",
        "페이지": 188,
        "대상시트": "emissions_management",
        "그래프유형": "표",
        "제목": title,
        "단위": "천톤CO2eq",
        "항목": "건물",
        "연도": 2030,
        "값": value,
        "신뢰도": confidence,
        "반영여부": "검토",
        "근거": json.dumps(판독필드, ensure_ascii=False),
    }


def _텍스트_관리권한행(value: float):
    return {
        "지자체명": "서울특별시",
        "인벤토리출처": "본문",
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접",
        "연도": 2030,
        "배출량": value,
        "단위": "천톤CO2eq",
    }


def _시각_관찰값(target_sheet: str, fields: dict, *, value: float = 100.0):
    return {
        "지자체명": "서울특별시",
        "페이지": 188,
        "대상시트": target_sheet,
        "그래프유형": "표",
        "제목": "지원 시트별 시각 판독",
        "단위": "천톤CO2eq",
        "항목": fields.get("지표명") or fields.get("부문") or fields.get("관리부문") or fields.get("사업명") or "합계",
        "연도": fields.get("목표연도") or fields.get("연도") or 2030,
        "값": value,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
    }


def test_시각_라벨병합은_기본_활성화되고_환경변수로_끌_수_있다(monkeypatch) -> None:
    # Given/Then: 재로드 없이 읽은 설정의 기본값은 활성화 상태다.
    assert config.VISUAL_MERGE_LABELED_ENABLED is True

    # When: 기존 환경변수 경로에 0을 지정하면
    monkeypatch.setenv("VISUAL_MERGE_LABELED_ENABLED", "0")

    # Then: 같은 불리언 파서가 비활성 값을 반환한다.
    assert config._env_bool("VISUAL_MERGE_LABELED_ENABLED", True) is False


def test_시각_라벨병합은_플래그를_끄면_본시트와_리포트를_바꾸지_않는다(monkeypatch) -> None:
    # Given: 병합 가능한 라벨 기반 시각 판독값이 있지만 플래그를 명시적으로 끄면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", False, raising=False)
    raw = {"municipality_name": "서울특별시", "chart_observations": [_관리권한_관찰값()]}

    # When: organizer가 비활성 설정으로 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 기존 격리 정책처럼 본 시트와 검증리포트는 조용히 유지된다.
    assert cleaned["emissions_management"] == []
    assert not any(row.get("영역") == "시각병합" for row in cleaned["validation_report"])


def test_시각_라벨병합은_게이트_미충족_사유를_행단위로_기록한다(monkeypatch) -> None:
    # Given: G1~G4 중 하나씩 실패하는 판독값이 들어오면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [
            _관리권한_관찰값(fields={"estimated": True, "관리부문": "건물", "세부부문": "공공", "직간접구분": "직접"}),
            _관리권한_관찰값(confidence="low"),
            _관리권한_관찰값(fields={"관리부문": "건물", "세부부문": "공공"}),
            _관리권한_관찰값(title="세계도시 사례 관리권한 배출량"),
        ],
    }

    # When: opt-in 병합 게이트를 적용하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 본 시트 병합 없이 각 차단 근거가 검증리포트에 남는다.
    assert cleaned["emissions_management"] == []
    visual_issues = [row for row in cleaned["validation_report"] if row.get("영역") == "시각병합"]
    assert len(visual_issues) == 4
    details = "\n".join(row["문제내용"] for row in visual_issues)
    assert "G1" in details
    assert "G2" in details
    assert "G3" in details
    assert "G4" in details
    assert all(row["항목"] == "차단" for row in visual_issues)


def test_시각_라벨병합은_실스키마의_estimated_부재만으로_G1을_차단하지_않는다(monkeypatch) -> None:
    # Given: 실제 라벨 판독처럼 estimated가 없거나, 추정값이거나, 판독필드 자체가 없는 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    라벨_판독 = _관리권한_관찰값(value=100.0)
    축_추정 = _관리권한_관찰값(
        value=200.0,
        fields={"estimated": True, "관리부문": "건물", "세부부문": "공공", "직간접구분": "직접"},
    )
    판독필드_없음 = _관리권한_관찰값(value=300.0)
    판독필드_없음["근거"] = "표 요약만 존재"
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [라벨_판독, 축_추정, 판독필드_없음],
    }

    # When: organizer가 G1을 판정하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: estimated 부재 관찰값만 병합되고 두 결함 관찰값은 각 사유로 차단된다.
    assert [row["배출량"] for row in cleaned["emissions_management"]] == [100.0]
    details = "\n".join(
        row["문제내용"]
        for row in cleaned["validation_report"]
        if row.get("영역") == "시각병합" and row.get("항목") == "차단"
    )
    assert "G1 축 기반 추정값" in details
    assert "G1 판독필드 없음" in details


def test_시각_라벨병합은_종류추론_목표만_차단하고_현황은_기존대로_병합한다(monkeypatch) -> None:
    # Given: 동일 스키마의 라벨 판독 중 목표와 현황 관찰값이 함께 들어오면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    목표 = _관리권한_관찰값(value=200.0)
    목표["종류추론"] = "목표"
    현황 = _관리권한_관찰값(value=100.0)
    현황["종류추론"] = "현황"

    # When: organizer가 라벨 병합 게이트를 적용하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [목표, 현황],
    })

    # Then: 목표 값은 차단 사유를 남기고 목표가 아닌 값만 병합한다.
    assert [row["배출량"] for row in cleaned["emissions_management"]] == [100.0]
    assert any(
        issue.get("항목") == "차단"
        and "G4 종류=목표 — 감축 경로표 값" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize(
    ("title", "expected_count", "expected_status"),
    [
        ("온실가스 흡수 전망", 0, "needs_review"),
        ("온실가스 BAU 산정", 1, "accept"),
    ],
)
def test_시각_라벨병합은_03_전망성_제목을_05로_재지정해_시나리오_계약을_적용한다(
    monkeypatch,
    title: str,
    expected_count: int,
    expected_status: str,
) -> None:
    # Given: 03 후보의 제목에 전망 키워드가 있고 시나리오 표기는 없으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "emissions_regional",
        {"배출유형": "흡수원", "부문": "흡수원", "연도": 2040},
    )
    observation["제목"] = title

    # When: 전망 시트로 재지정해 동일한 G3 심사를 수행하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 03은 오염되지 않고, BAU를 식별한 후보만 05에 반영된다.
    assert cleaned["emissions_regional"] == []
    assert len(cleaned["emissions_forecast"]) == expected_count
    assert observation["병합상태"] == expected_status
    if expected_status == "needs_review":
        assert "G3 1차 키 누락(시나리오)" in observation["병합차단사유"]
    else:
        assert cleaned["emissions_forecast"][0]["시나리오"] == "BAU"
        assert any(
            issue.get("대상시트키") == "emissions_forecast"
            and issue.get("항목") == "병합"
            and "대상시트 재지정(전망 키워드)" in issue.get("문제내용", "")
            for issue in cleaned["validation_report"]
        )


def test_시각_라벨병합은_근거의_전망_문자열로_03을_재지정하지_않는다(monkeypatch) -> None:
    # Given: 제목은 현황이고 판독필드의 무관한 근거 문자열에만 전망이 있으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "emissions_regional",
        {"배출유형": "직접배출", "부문": "에너지", "연도": 2030, "메모": "전망 표와 비교"},
    )
    observation["제목"] = "온실가스 배출 현황"

    # When: 제목만으로 재지정 여부를 판단하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 근거 문자열 오탐 없이 03 후보로 병합된다.
    assert len(cleaned["emissions_regional"]) == 1
    assert cleaned["emissions_forecast"] == []


@pytest.mark.parametrize(
    ("target_sheet", "fields"),
    [
        ("emissions_regional", {"배출유형": "직접배출", "부문": "건물", "연도": 2030}),
        ("emissions_management", {"관리부문": "건물", "직간접구분": "직접", "연도": 2030}),
    ],
)
def test_시각_라벨병합은_03_04의_퍼센트_단위를_차단한다(
    monkeypatch, target_sheet: str, fields: dict
) -> None:
    # Given: 03 또는 04 후보의 비교값 단위가 비율이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(target_sheet, fields, value=88.4)
    observation["단위"] = "%"

    # When: 라벨 병합 게이트를 적용하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 본 시트 병합 없이 단위 부적합 사유를 남긴다.
    assert cleaned[target_sheet] == []
    assert any(
        "G3 비교값 단위 부적합(%)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_시각_라벨병합은_02의_퍼센트_단위를_허용한다(monkeypatch) -> None:
    # Given: 02 지역여건의 정당한 비율 지표이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "regional_conditions",
        {"지표범주": "에너지", "지표명": "재생에너지 비중", "연도": 2030},
        value=88.4,
    )
    observation["단위"] = "%"

    # When: 라벨 병합 게이트를 적용하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 02의 비율값은 기존처럼 병합된다.
    assert cleaned["regional_conditions"][0]["값"] == 88.4


@pytest.mark.parametrize(
    ("target_sheet", "fields"),
    [
        ("emissions_regional", {"배출유형": "직접배출", "부문": "이산화탄소", "연도": 2030}),
        ("emissions_management", {"관리부문": "CO2", "직간접구분": "직접", "연도": 2030}),
    ],
)
def test_시각_라벨병합은_03_04_부문의_IPCC_가스명을_차단한다(
    monkeypatch, target_sheet: str, fields: dict
) -> None:
    # Given: 03 또는 04 부문 필드가 IPCC 표준 가스명과 전체일치하면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(target_sheet, fields)

    # When: 스키마 적합성을 검사하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 가스종을 부문으로 병합하지 않고 명시적 차단 사유를 남긴다.
    assert cleaned[target_sheet] == []
    assert any(
        "G3 부문에 가스종(스키마 불일치)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize("sector", ["에너지", "기타", "메탄가스화시설"])
def test_시각_라벨병합은_IPCC_가스명과_전체일치하지_않는_부문을_허용한다(
    monkeypatch, sector: str
) -> None:
    # Given: 03 부문이 일반 부문이거나 잡음 제거 뒤에도 가스명과 전체일치하지 않으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "emissions_regional",
        {"배출유형": "직접배출", "부문": sector, "연도": 2030},
    )

    # When: 전체일치 규칙으로 검사하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 허용 목록의 정확한 가스명이 아니므로 기존처럼 병합된다.
    assert len(cleaned["emissions_regional"]) == 1


def test_시각_라벨병합은_텍스트가_없을_때_visual_only로_본시트에_반영한다(monkeypatch) -> None:
    # Given: G1~G4를 통과하고 같은 키의 텍스트 행이 없는 시각 판독값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {"municipality_name": "서울특별시", "chart_observations": [_관리권한_관찰값()]}

    # When: organizer가 opt-in 병합을 수행하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 기존 시트 컬럼만 가진 visual_only 행과 병합 기록이 생긴다.
    assert len(cleaned["emissions_management"]) == 1
    row = cleaned["emissions_management"][0]
    assert row["관리부문"] == "건물"
    assert row["세부부문"] == "공공"
    assert row["직간접구분"] == "직접"
    assert row["배출량"] == 100.0
    assert row["출처페이지"] == "188"
    assert row["데이터상태"] == "visual_only"
    allowed = set(config.EXCEL_HEADERS["04_배출현황_관리권한"])
    assert set(row) <= allowed
    assert any(issue.get("영역") == "시각병합" and issue.get("항목") == "병합" for issue in cleaned["validation_report"])


def test_시각_라벨병합은_02_지표범주를_기본값으로_만들지_않는다(monkeypatch) -> None:
    # Given: 지표명과 연도는 있지만 1차 키인 지표범주를 판독하지 못한 02 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값("regional_conditions", {"지표명": "통행량", "연도": 2030})

    # When: organizer가 후보 키를 검사하면
    cleaned = OrganizerAgent().organize({"municipality_name": "서울특별시", "chart_observations": [observation]})

    # Then: 시각자료 기본 범주를 합성하지 않고 지표범주 누락으로 G3 차단한다.
    assert cleaned["regional_conditions"] == []
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 1차 키 누락(지표범주)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize(
    ("text_value", "expected_item", "expected_text"),
    [
        (100.3, "생략", "교차일치"),
        (120.0, "차단", "텍스트-시각 값 불일치"),
    ],
)
def test_시각_라벨병합은_판독한_02_지표범주로_G5를_복구한다(
    monkeypatch,
    text_value: float,
    expected_item: str,
    expected_text: str,
) -> None:
    # Given: 판독필드에서 얻은 지표범주와 동일한 키의 텍스트 행이 있으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "regional_conditions",
        {"지표범주": "교통", "지표명": "통행량", "연도": 2030},
        value=100.0,
    )
    raw = {
        "municipality_name": "서울특별시",
        "regional_conditions": [{
            "지자체명": "서울특별시",
            "지표범주": "교통",
            "지표명": "통행량",
            "연도": 2030,
            "값": text_value,
        }],
        "chart_observations": [observation],
    }

    # When: organizer가 같은 실측 키로 교차검증하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 새 행을 병합하지 않고 값 차이에 따라 생략 또는 경고 차단을 기록한다.
    assert len(cleaned["regional_conditions"]) == 1
    assert cleaned["regional_conditions"][0]["값"] == text_value
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == expected_item
        and expected_text in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize(
    ("text_value", "expected_count", "expected_item", "expected_reason"),
    [
        (100.3, 1, "생략", "세부범주 완화 교차일치"),
        (120.0, 2, "병합", "세부범주 상이 텍스트 값불일치 관찰"),
    ],
)
def test_시각_라벨병합은_02_텍스트가_없을_때만_세부범주를_제외해_재탐색한다(
    monkeypatch,
    text_value: float,
    expected_count: int,
    expected_item: str,
    expected_reason: str,
) -> None:
    # Given: 핵심 키는 같지만 세부범주가 다른 텍스트 행과 시각 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "regional_conditions",
        {"지표범주": "에너지", "지표세부범주": "최종에너지", "지표명": "합계", "연도": 2021},
        value=100.0,
    )
    raw = {
        "municipality_name": "서울특별시",
        "regional_conditions": [{
            "지자체명": "서울특별시",
            "지표범주": "에너지",
            "지표세부범주": "에너지원별 소비",
            "지표명": "합계",
            "연도": 2021,
            "값": text_value,
        }],
        "chart_observations": [observation],
    }

    # When: 세부범주 포함 키에 텍스트가 없어 완화 키로만 재탐색하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 값 동일은 생략하고 값 불일치는 경고 없이 병합을 유지하며 각각 사유를 남긴다.
    assert len(cleaned["regional_conditions"]) == expected_count
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == expected_item
        and expected_reason in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )
    if expected_item == "병합":
        assert cleaned["regional_conditions"][-1]["데이터상태"] == "visual_only"
        assert not any(
            issue.get("영역") == "시각병합" and issue.get("항목") in {"경고", "차단"}
            for issue in cleaned["validation_report"]
        )


def test_시각_라벨병합은_다른_지표세부범주의_차트_관찰값을_모두_병합한다(monkeypatch) -> None:
    # Given: 범주·지표명·연도는 같지만 차트 수준 세부범주가 다른 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [
            _시각_관찰값(
                "regional_conditions",
                {"지표범주": "에너지", "지표세부범주": "2005 실적", "지표명": "도시가스", "연도": 2005},
                value=100.0,
            ),
            _시각_관찰값(
                "regional_conditions",
                {"지표범주": "에너지", "지표세부범주": "2005 전망", "지표명": "도시가스", "연도": 2005},
                value=120.0,
            ),
        ],
    }

    # When: organizer가 세부범주를 포함한 키로 시각 행을 비교하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 두 관찰값이 각각 병합되고 동일 키 시각 행 경고는 발생하지 않는다.
    assert [(row["지표세부범주"], row["값"]) for row in cleaned["regional_conditions"]] == [
        ("2005 실적", 100.0),
        ("2005 전망", 120.0),
    ]
    visual_issues = [row for row in cleaned["validation_report"] if row.get("영역") == "시각병합"]
    assert sum(row.get("항목") == "병합" for row in visual_issues) == 2
    assert not any(
        row.get("심각도") == "경고" or "동일 키 시각 행 존재" in row.get("문제내용", "")
        for row in visual_issues
    )


def test_시각_라벨병합은_빈_지표세부범주를_와일드카드로_비교한다(monkeypatch) -> None:
    # Given: 기존 시각 행에는 세부범주가 있고 같은 값의 신규 관찰값에는 세부범주가 없으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [
            _시각_관찰값(
                "regional_conditions",
                {"지표범주": "에너지", "지표세부범주": "에너지원별 소비", "지표명": "도시가스", "연도": 2030},
            ),
            _시각_관찰값(
                "regional_conditions",
                {"지표범주": "에너지", "지표명": "도시가스", "연도": 2030},
            ),
        ],
    }

    # When: organizer가 선택 키인 세부범주의 공란을 비교하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 공란을 와일드카드로 보아 기존 시각 행을 유지하고 신규 행을 생략한다.
    assert len(cleaned["regional_conditions"]) == 1
    assert cleaned["regional_conditions"][0]["지표세부범주"] == "에너지원별 소비"
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "생략"
        and "동일 키 시각 행 존재" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_시각_라벨병합은_빈_지표세부범주를_G3에서_차단하지_않는다(monkeypatch) -> None:
    # Given: 필수 키는 모두 있고 선택 키인 세부범주만 비어 있는 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "regional_conditions",
        {"지표범주": "에너지", "지표명": "도시가스", "연도": 2030},
    )

    # When: organizer가 G3 1차 키를 검사하면
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
    })

    # Then: 세부범주 공란은 G3 누락이 아니므로 시각 전용 행으로 병합된다.
    assert len(cleaned["regional_conditions"]) == 1
    assert cleaned["regional_conditions"][0]["지표세부범주"] == ""
    assert any(
        issue.get("영역") == "시각병합" and issue.get("항목") == "병합"
        for issue in cleaned["validation_report"]
    )
    assert not any(
        issue.get("영역") == "시각병합"
        and "G3 1차 키 누락(지표세부범주)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_시각_라벨병합은_빈_세부부문을_기본값으로_채우지_않고_상세행과_교차검증한다(monkeypatch) -> None:
    # Given: 필수 키는 판독했지만 absorb 대상 세부부문은 비어 있는 지역배출 관찰값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _시각_관찰값(
        "emissions_regional",
        {"배출유형": "직접배출", "부문": "건물", "연도": 2030},
        value=100.0,
    )
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [{
            "지자체명": "서울특별시",
            "배출유형": "직접배출",
            "부문": "건물",
            "세부부문": "공공",
            "연도": 2030,
            "배출량": 100.3,
        }],
        "chart_observations": [observation],
    }

    # When: organizer가 빈 세부부문을 흡수 가능한 키로 비교하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 세부부문 기본값을 합성하지 않고 기존 상세 텍스트 행과 교차일치해 생략한다.
    assert len(cleaned["emissions_regional"]) == 1
    assert cleaned["emissions_regional"][0]["세부부문"] == "공공"
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "생략"
        and "교차일치" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize(
    ("target_sheet", "fields", "value_field", "excel_sheet", "supported"),
    [
        ("regional_conditions", {"지표범주": "교통", "지표명": "통행량", "연도": 2030}, "값", "02_지역여건", True),
        ("emissions_regional", {"배출유형": "직접배출", "부문": "건물", "세부부문": "공공", "연도": 2030}, "배출량", "03_배출현황_지역", True),
        ("emissions_management", {"관리부문": "건물", "세부부문": "공공", "직간접구분": "직접", "연도": 2030}, "배출량", "04_배출현황_관리권한", True),
        ("emissions_forecast", {"시나리오": "BAU", "부문": "건물", "연도": 2030}, "전망값", "05_배출전망", True),
        ("reduction_targets", {"목표수준": "부문", "목표범위": "지역전체", "부문": "건물", "목표연도": 2030}, "목표배출량", "06_감축목표", True),
        ("financial_plan", {"계획구분": "온실가스감축대책", "부문": "건물", "사업명": "효율화", "재원구분": "합계", "연도": 2030}, "예산액", "11_재정투자계획", True),
    ],
)
def test_시각_라벨병합은_지원_수치시트만_계약컬럼을_사용한다(
    monkeypatch,
    target_sheet: str,
    fields: dict,
    value_field: str,
    excel_sheet: str,
    supported: bool,
) -> None:
    # Given: 지원 대상 수치 시트별 1차 키가 모두 채워진 라벨 기반 시각 판독값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {"municipality_name": "서울특별시", "chart_observations": [_시각_관찰값(target_sheet, fields)]}

    # When: organizer가 opt-in 병합을 수행하면
    cleaned = OrganizerAgent().organize(raw)

    if not supported:
        assert cleaned[target_sheet] == []
        assert any(
            issue.get("영역") == "시각병합"
            and issue.get("항목") == "차단"
            and "G3 대상 시트 미지원" in issue.get("문제내용", "")
            for issue in cleaned["validation_report"]
        )
        return

    # Then: 각 대상 시트는 기존 엑셀 계약 컬럼만 가진 visual_only 행을 받는다.
    assert len(cleaned[target_sheet]) == 1
    row = cleaned[target_sheet][0]
    assert row[value_field] == 100.0
    assert row["데이터상태"] == "visual_only"
    allowed = set(config.EXCEL_HEADERS[excel_sheet])
    if "감축률(%)" in allowed:
        allowed.add("감축률")
    assert set(row) <= allowed
    assert any(issue.get("영역") == "시각병합" and issue.get("항목") == "병합" for issue in cleaned["validation_report"])


def test_시각_라벨병합은_텍스트_일치시_생략하고_교차일치를_기록한다(monkeypatch) -> None:
    # Given: 같은 키의 텍스트 행이 0.5% 이내 값으로 이미 있으면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "emissions_management": [_텍스트_관리권한행(100.3)],
        "chart_observations": [_관리권한_관찰값(value=100.0)],
    }

    # When: organizer가 교차검증하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 중복 병합을 생략하고 교차일치 근거를 남긴다.
    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["emissions_management"][0]["배출량"] == 100.3
    assert any(
        issue.get("영역") == "시각병합" and issue.get("항목") == "생략" and "교차일치" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_시각_라벨병합은_텍스트_불일치시_경고하고_병합하지_않는다(monkeypatch) -> None:
    # Given: 같은 키의 텍스트 행과 0.5% 넘게 불일치하는 시각 판독값이면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "emissions_management": [_텍스트_관리권한행(120.0)],
        "chart_observations": [_관리권한_관찰값(value=100.0)],
    }

    # When: organizer가 교차검증하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 본 시트는 텍스트 행만 유지하고 경고를 남긴다.
    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["emissions_management"][0]["배출량"] == 120.0
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and issue.get("심각도") == "경고"
        and "텍스트-시각 값 불일치" in issue.get("문제내용", "")
        and "텍스트 120.0" in issue.get("문제내용", "")
        and "시각 100.0" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_시각_라벨병합은_같은_키의_시각_관찰값을_한_번만_병합한다(monkeypatch) -> None:
    # Given: 같은 키와 값을 가진 시각 관찰값이 두 번 들어오면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [_관리권한_관찰값(), _관리권한_관찰값()],
    }

    # When: organizer가 시각 전용 행을 순서대로 병합하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 첫 행만 유지하고 두 번째 관찰값은 동일 키 시각 행 존재 사유로 생략한다.
    assert len(cleaned["emissions_management"]) == 1
    visual_issues = [row for row in cleaned["validation_report"] if row.get("영역") == "시각병합"]
    assert sum(row.get("항목") == "병합" for row in visual_issues) == 1
    assert sum(row.get("항목") == "생략" for row in visual_issues) == 1
    assert any("동일 키 시각 행 존재" in row.get("문제내용", "") for row in visual_issues)


def test_시각_라벨병합은_같은_키의_시각값이_다르면_첫_행을_유지하고_경고한다(monkeypatch) -> None:
    # Given: 같은 키의 두 시각 관찰값이 0.5% 넘게 다르면
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [_관리권한_관찰값(value=100.0), _관리권한_관찰값(value=120.0)],
    }

    # When: organizer가 두 번째 관찰값을 비교하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 첫 시각 행을 유지하고 양쪽 값을 포함한 경고만 남긴다.
    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["emissions_management"][0]["배출량"] == 100.0
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "경고"
        and issue.get("심각도") == "경고"
        and "동일 키 시각 행 존재" in issue.get("문제내용", "")
        and "기존 시각 100.0" in issue.get("문제내용", "")
        and "신규 시각 120.0" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )
