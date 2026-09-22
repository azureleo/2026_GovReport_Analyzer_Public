from __future__ import annotations

import config
from agents.organizer_agent import OrganizerAgent


def test_expanded_sheet_contracts_include_semantic_fields() -> None:
    assert {"기준배출량기준", "목표배출량기준", "감축률계산값"} <= set(
        config.EXCEL_HEADERS["06_감축목표"]
    )
    assert {"감축량유형", "시간기준"} <= set(config.EXCEL_HEADERS["10_정량감축량"])
    assert {"평가유형", "시나리오", "리스크항목", "취약성지표", "리스크등급"} <= set(
        config.EXCEL_HEADERS["12_대응기반강화"]
    )
    assert {"소요예산", "예산액", "예산유형", "예산집행률"} <= set(
        config.EXCEL_HEADERS["14_점검실적"]
    )
    for sheet_name, headers in config.EXCEL_HEADERS.items():
        if sheet_name[:2].isdigit() and int(sheet_name[:2]) <= 15:
            assert "derivation_type" in headers
            assert "근거ID" in headers


def test_reported_reduction_rate_is_preserved_and_calculation_is_separate() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "reduction_targets": [{
            "목표수준": "총괄", "목표범위": "관리권한", "부문": "합계",
            "기준연도": 2018, "기준배출량": 100, "목표연도": 2030,
            "목표배출량": 80, "감축률": 25, "출처페이지": 10,
        }],
    })

    row = cleaned["reduction_targets"][0]
    assert row["감축률"] == 25.0
    assert row["감축률계산값"] == 20.0
    assert row["기준배출량기준"] == "gross"
    assert row["목표배출량기준"] == "net"
    assert row["근거ID"].startswith("txt-reduction_targets-p10-")
    assert any("감축률 불일치" in issue["항목"] for issue in cleaned["validation_report"])


def test_campaign_name_does_not_force_qualitative_classification() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "mitigation_projects": [{"사업명": "탄소중립 시민 캠페인", "출처페이지": 20}],
    })

    row = cleaned["mitigation_projects"][0]
    assert row["정량여부"] is None
    assert row["derivation_type"] == "explicit"
    assert row["근거ID"].startswith("txt-mitigation_projects-p20-")


def test_climate_risk_and_budget_are_structured_without_semantic_overreach() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "foundation_measures": [{
            "평가유형": "취약성 평가", "기후변수": "폭염일수", "시나리오": "SSP5-8.5",
            "미래기간": "2041~2060", "공간단위": "자치구", "부문": "건강",
            "리스크항목": "폭염 건강피해", "취약성지표": "취약성지수",
            "값": "0.82", "단위": "지수", "리스크등급": "높음", "출처페이지": 30,
        }],
        "monitoring_performance": [{
            "점검연도": 2025, "사업명": "건물 효율화", "이행실적": "완료",
            "소요예산": "120", "예산단위": "백만원", "출처페이지": 40,
        }],
    })

    risk = cleaned["foundation_measures"][0]
    assert risk["평가유형"] == "vulnerability"
    assert risk["값"] == 0.82
    assert risk["근거ID"].startswith("txt-foundation_measures-p30-")
    budget = cleaned["monitoring_performance"][0]
    assert budget["소요예산"] == "120"
    assert budget["예산액"] == 120.0
    assert budget["예산유형"] == "reported_unspecified"
    assert budget["derivation_type"] == "normalized"
