from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import config
from agents import supervisor as supervisor_module
from agents.organizer_agent import OrganizerAgent
from utils import llm_client
from utils.pdf_reader import PageContent


SHEET_A = "emissions_management"
SHEET_B = "reduction_targets"
_IDEMPOTENT_ROWS = {
    "document_meta": [{"계획명": "계획", "계획시작연도": "2021", "계획종료연도": "2030", "기준연도": "2018"}],
    "plan_overview": [{"개요유형": "절차", "항목명": "수립", "항목값": "완료"}],
    "regional_conditions": [{"지표범주": "인구", "지표명": "인구", "연도": "2020", "값": "1"}],
    "emissions_regional": [{"인벤토리출처": "GIR", "배출유형": "직접 배출", "부문": "건물", "세부부문": "공공", "연도": "2020", "배출량": "1", "단위": "톤CO2eq"}],
    "emissions_management": [{"관리부문": "건물부문", "세부부문": "공공", "직간접구분": "직접", "연도": "2030", "배출량": "10.5", "단위": "tCO2eq", "출처페이지": [3]}],
    "emissions_forecast": [{"시나리오": "BAU", "전망방법원문": "시계열", "부문": "건물", "연도": "2030", "전망값": "1", "단위": "톤CO2eq"}],
    "reduction_targets": [{"목표수준": "총괄", "목표범위": "GIR", "부문": "합계", "기준배출량": "100", "목표배출량": "60", "목표연도": "2030"}],
    "vision_strategy": [{"비전문구": "탄소중립 도시"}],
    "mitigation_projects": [{"관리번호": "M1", "부문": "건물부문", "사업명": "맞춤형 테스트 사업"}],
    "annual_implementation": [{"관리번호": "M1", "사업명": "맞춤형 테스트 사업", "연도": "2030", "연간계획": "추진", "목표물량": "1"}],
    "quantitative_reductions": [{"관리번호": "M1", "사업명": "맞춤형 테스트 사업", "연도": "2030", "활동량": "1", "예상감축량": "1"}],
    "financial_plan": [{"계획구분": "총계", "부문": "건물", "사업명": "맞춤형 테스트 사업", "재원구분": "합계", "연도": "2030", "예산액": "1"}],
    "foundation_measures": [{"과제명": "교육", "주요내용": "내용"}],
    "governance_feedback": [{"거버넌스기구": "위원회", "역할": "심의"}],
    "monitoring_performance": [{"점검연도": "2030", "사업명": "맞춤형 테스트 사업", "이행실적": "완료", "달성여부": "정상 추진", "사업유형": "기존"}],
    "changes_actions": [{"점검연도": "2030", "사업명": "맞춤형 테스트 사업", "변경사유": "변경"}],
}


def _page(page_number: int, text: str = "서울특별시 탄소중립 기본계획") -> PageContent:
    return PageContent(page_number=page_number, text=text, tables=[], images=[])


@dataclass
class _QuotaExtractor:
    events: list[str]
    routes: dict[str, list[PageContent]]
    ledger: list = field(default_factory=list)
    routed_page_nums: dict[str, set[int]] = field(default_factory=dict)

    def _extract_municipality_name(self, full_text: str) -> str:
        return "서울특별시"

    def route_pages(self, pages: list[PageContent]) -> dict[str, list[PageContent]]:
        self.routed_page_nums = {key: {page.page_number for page in value} for key, value in self.routes.items()}
        return self.routes

    def extract_sheet_pages(
        self,
        sheet_key: str,
        sheet_pages: list[PageContent],
        municipality: str,
        extraction_prompts: dict[str, str],
        batch_size: int = config.BATCH_SIZE,
    ) -> list[dict]:
        if sheet_key == SHEET_B:
            self.events.append(f"{sheet_key}:extract:quota")
            raise llm_client.LLMQuotaExceededError("quota")
        self.events.append(f"{sheet_key}:extract")
        return [{"지자체명": municipality, "시트": sheet_key, "출처페이지": [page.page_number for page in sheet_pages]}]


@dataclass
class _FakeOrganizer:
    events: list[str]

    def organize_sheet(self, sheet_key: str, rows: list[dict], municipality: str) -> list[dict]:
        self.events.append(f"{sheet_key}:clean")
        return [dict(row, 지자체명=municipality) for row in rows]


@pytest.mark.parametrize("sheet_key", config.EXTRACTION_SHEETS)
def test_organize_sheet_is_idempotent_for_all_extraction_sheets(sheet_key: str) -> None:
    # Given: 폐루프가 재정제할 추출 대상 시트별 대표 행
    organizer = OrganizerAgent()
    raw_rows = _IDEMPOTENT_ROWS[sheet_key]

    # When: 같은 cleaner를 두 번 통과시키면
    first = organizer.organize_sheet(sheet_key, raw_rows, "서울특별시")
    second = organizer.organize_sheet(sheet_key, first, "서울특별시")

    # Then: 폐루프 재정제에서 행이 흔들리지 않는다.
    assert second == first


def test_supervisor_closed_loop_extraction_quota_preserves_completed_sheet_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 두 번째 시트 추출에서 quota가 발생하는 폐루프
    events: list[str] = []
    routes = {SHEET_A: [_page(1)], SHEET_B: [_page(2)]}
    fake_extractor = _QuotaExtractor(events, routes)
    fake_organizer = _FakeOrganizer(events)
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", False)
    monkeypatch.setattr(supervisor_module, "ExtractorAgent", lambda: fake_extractor)
    monkeypatch.setattr(supervisor_module, "OrganizerAgent", lambda: fake_organizer)

    # When: 폐루프 텍스트 경로를 실행하면
    raw_data, extractor, candidates, merge_log = supervisor_module.Supervisor()._run_sheet_closed_loop(
        pages=[_page(1), _page(2)],
        full_text="서울특별시 탄소중립 기본계획",
        extraction_prompts={},
    )

    # Then: 새 빈 extractor로 갈아타지 않고 완료된 첫 시트의 부분 상태를 그대로 반환한다.
    assert extractor is fake_extractor
    assert SHEET_A in raw_data
    assert SHEET_B not in raw_data
    assert candidates == []
    assert merge_log == []
    assert events == [f"{SHEET_A}:extract", f"{SHEET_A}:clean", f"{SHEET_B}:extract:quota"]
