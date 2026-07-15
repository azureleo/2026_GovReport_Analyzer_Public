from __future__ import annotations

import config
from agents.organizer_agent import OrganizerAgent, detect_prior_plan_pages
from utils.pdf_reader import PageContent


def _page(number: int, text: str) -> PageContent:
    return PageContent(page_number=number, text=text, tables=[], images=[])


def test_detect_prior_plan_pages_finds_seoul_like_chapter_range() -> None:
    # Given: 제3장 기존 계획 평가가 p148에서 시작하고 제4장이 p161에서 시작하는 문서
    pages = [
        _page(147, "제2장 지역현황 분석\n본문"),
        *[_page(number, "제3장 기존 계획의 평가\n성과평가 및 시사점") for number in range(148, 161)],
        _page(161, "제4장 상위계획 분석\n본문"),
    ]

    # When: 기존계획 평가 장 구간을 탐지하면
    detected = detect_prior_plan_pages(pages)

    # Then: p148~p160만 prior-plan 구간으로 잡힌다.
    assert detected == set(range(148, 161))


def test_detect_prior_plan_pages_noops_without_heading_pattern() -> None:
    # Given: 기존계획 평가 헤딩이 없는 문서
    pages = [_page(1, "제1장 개요"), _page(2, "제2장 지역현황 분석")]

    # When/Then: 보수적으로 빈 구간을 반환한다.
    assert detect_prior_plan_pages(pages) == set()


def test_prior_plan_rows_are_tagged_and_reported_without_deletion() -> None:
    # Given: 08 시트에 기존계획 페이지 유래 행과 본계획 행이 섞여 있으면
    raw = {
        "municipality_name": "서울특별시",
        "mitigation_projects": [
            {"관리번호": "M1-2", "부문": "수송", "사업명": "청소차량 전환", "출처페이지": "p149"},
            {"관리번호": "M1-2", "부문": "수송", "사업명": "전기차 보급 촉진", "출처페이지": "p253"},
        ],
    }

    # When: prior_plan_pages를 전달해 정제하면
    cleaned = OrganizerAgent().organize(raw, prior_plan_pages={149})

    # Then: 기존계획 행은 내부 태그와 경고를 받되 삭제되지 않는다.
    assert len(cleaned["mitigation_projects"]) == 2
    prior = next(row for row in cleaned["mitigation_projects"] if row["사업명"] == "청소차량 전환")
    current = next(row for row in cleaned["mitigation_projects"] if row["사업명"] == "전기차 보급 촉진")
    assert prior["계획구분출처"] == "기존계획"
    assert current["계획구분출처"] == "본계획"
    assert any("기존계획 평가 장" in issue["문제내용"] for issue in cleaned["validation_report"])


def test_prior_plan_tag_does_not_overwrite_financial_plan_budget_category() -> None:
    # Given: 11_재정투자계획의 실컬럼 계획구분에 예산 분류값이 들어 있고 기존계획 장에서 왔으면
    raw = {
        "municipality_name": "서울특별시",
        "financial_plan": [
            {
                "계획구분": "온실가스감축대책",
                "부문": "수송",
                "사업명": "청소차량 전환",
                "재원구분": "합계",
                "연도": 2030,
                "예산액": 100,
                "출처페이지": "p149",
            }
        ],
    }

    # When: prior_plan_pages를 전달해 정제하면
    cleaned = OrganizerAgent().organize(raw, prior_plan_pages={149})

    # Then: 예산 분류 실컬럼은 보존되고 장 문맥은 비출력 내부 필드로만 남는다.
    row = cleaned["financial_plan"][0]
    assert row["계획구분"] == "온실가스감축대책"
    assert row["계획구분출처"] == "기존계획"
    assert all("계획구분출처" not in headers for headers in config.EXCEL_HEADERS.values())


def test_project_id_registry_warns_on_conflicting_current_plan_names_and_absorbs_id_format() -> None:
    # Given: 본계획 08 시트의 같은 관리번호가 서로 다른 사업명을 가리키고 09 시트는 F2-02 표기를 쓴다.
    raw = {
        "municipality_name": "서울특별시",
        "mitigation_projects": [
            {"관리번호": "F2-2", "부문": "건물", "사업명": "공공건물 효율화", "출처페이지": "p210"},
            {"관리번호": "F2-2", "부문": "건물", "사업명": "민간건물 효율화", "출처페이지": "p211"},
        ],
        "annual_implementation": [
            {"관리번호": "F2-02", "사업명": "다른 사업명", "연간계획": "추진", "출처페이지": "p220"},
        ],
    }

    # When: 관리번호 일관성 검증을 실행하면
    cleaned = OrganizerAgent().organize(raw, prior_plan_pages={149})

    # Then: 08 충돌은 경고, F2-02/F2-2 흡수와 사업명 불일치는 정보로 남는다.
    issues = cleaned["validation_report"]
    assert any(issue["심각도"] == "경고" and "관리번호 F2-2에 사업명" in issue["항목"] for issue in issues)
    assert any(issue["심각도"] == "정보" and "관리번호 표기 정규화" in issue["항목"] for issue in issues)
    assert any(issue["심각도"] == "정보" and "관리번호-사업명 불일치" in issue["항목"] for issue in issues)


def test_project_id_registry_noops_when_prior_plan_not_detected() -> None:
    # Given: prior-plan 탐지가 없는 문서에서 같은 관리번호가 서로 다른 사업명으로 나온다.
    raw = {
        "municipality_name": "서울특별시",
        "mitigation_projects": [
            {"관리번호": "F2-2", "부문": "건물", "사업명": "공공건물 효율화", "출처페이지": "p210"},
            {"관리번호": "F2-2", "부문": "건물", "사업명": "민간건물 효율화", "출처페이지": "p211"},
        ],
    }

    # When: prior_plan_pages 없이 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: WP7 관리번호 일관성 검증은 no-op으로 남아 v4.1 기본 출력에 새 경고를 추가하지 않는다.
    assert not any("관리번호" in issue["항목"] for issue in cleaned["validation_report"])


def test_prior_plan_heading_patterns_env_uses_regex_safe_separator(monkeypatch) -> None:
    # Given: 콤마가 포함된 정규식 수량자를 env로 주입하면
    pattern = r"기존\s*계획.{0,6}평가"
    monkeypatch.setenv("PRIOR_PLAN_HEADING_PATTERNS", f"{pattern};;이전\\s*계획.{{0,6}}평가")

    # When: 전용 패턴 파서를 호출하면
    parsed = config._env_pattern_list("PRIOR_PLAN_HEADING_PATTERNS", [])

    # Then: "{0,6}"이 콤마에서 쪼개지지 않는다.
    assert parsed[0] == pattern
    assert all("{0,6}" in item for item in parsed)
