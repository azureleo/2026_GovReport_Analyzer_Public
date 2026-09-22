from agents.organizer_agent import OrganizerAgent


def test_unresolved_numeric_conflict_is_warning() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 10},
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 20, "단위": "천톤"},
        ],
    })

    conflicts = [
        issue for issue in cleaned["validation_report"]
        if "중복 키 값 충돌" in issue.get("항목", "")
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["심각도"] == "경고"
    assert "자동해결" not in conflicts[0]["항목"]
    assert "충돌값 자동선택 금지" in conflicts[0]["문제내용"]


def test_financial_subtotals_are_not_compared_across_projects() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "financial_plan": [
            {"계획구분": "본계획", "부문": "건물", "사업명": "사업 A", "재원구분": "합계", "연도": 2030, "예산액": 100, "예산단위": "백만원"},
            {"계획구분": "본계획", "부문": "건물", "사업명": "사업 B", "재원구분": "국비", "연도": 2030, "예산액": 50, "예산단위": "백만원"},
            {"계획구분": "본계획", "부문": "건물", "사업명": "사업 B", "재원구분": "시비", "연도": 2030, "예산액": 60, "예산단위": "백만원"},
        ],
    })

    assert not any(
        "합계≠부분합" in issue.get("항목", "")
        for issue in cleaned["validation_report"]
    )
