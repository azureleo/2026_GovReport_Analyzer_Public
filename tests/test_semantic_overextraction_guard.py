from __future__ import annotations

import config
from agents.organizer_agent import OrganizerAgent
from utils.semantic_contract_guard import (
    guard_foundation_measures,
    guard_reduction_targets,
)


def _target(**overrides) -> dict:
    row = {
        "목표수준": "부문",
        "목표범위": "관리권한",
        "부문": "건물",
        "기준연도": 2018,
        "목표연도": 2030,
        "목표감축량": 100,
        "출처페이지": 10,
    }
    row.update(overrides)
    return row


def test_06_keeps_summary_and_unmatched_detailed_targets() -> None:
    rows = [
        _target(),
        _target(
            목표수준="세부사업",
            _사업명힌트="공공건물 효율화",
            목표감축량=25,
            출처페이지=11,
        ),
    ]

    retained, records = guard_reduction_targets(rows)

    assert retained == rows
    assert records == []


def test_06_quarantines_only_exact_canonical_quantitative_duplicate() -> None:
    target = _target(
        목표수준="세부사업",
        _사업명힌트="공공건물 효율화",
        목표감축량=25,
        출처페이지=11,
    )
    quantitative = {
        "사업명": "공공건물 효율화",
        "연도": 2030,
        "예상감축량": 25,
        "출처페이지": 11,
    }

    retained, records = guard_reduction_targets(
        [target],
        quantitative_rows=[quantitative],
    )

    assert retained == []
    assert records[0]["reason_codes"] == ["canonical_quantitative_duplicate"]


def test_06_does_not_quarantine_same_value_from_different_evidence() -> None:
    target = _target(
        목표수준="세부사업",
        _사업명힌트="공공건물 효율화",
        목표감축량=25,
        출처페이지=11,
    )
    quantitative = {
        "사업명": "공공건물 효율화",
        "연도": 2030,
        "예상감축량": 25,
        "출처페이지": 311,
    }

    retained, records = guard_reduction_targets(
        [target],
        quantitative_rows=[quantitative],
    )

    assert retained == [target]
    assert records == []


def test_06_quarantines_empty_rows_and_visual_metric_labels() -> None:
    empty = {
        "목표수준": "총괄",
        "부문": "합계",
        "목표연도": 2030,
        "출처페이지": 20,
    }
    visual_fragment = {
        "목표수준": "총괄",
        "부문": "BAU",
        "목표연도": 2030,
        "배출전망": 300,
        "데이터상태": "visual_only",
        "출처페이지": 21,
    }

    retained, records = guard_reduction_targets([empty, visual_fragment])

    assert retained == []
    assert {reason for record in records for reason in record["reason_codes"]} == {
        "no_target_quantity",
        "visual_metric_label_as_sector",
    }


def test_12_keeps_policy_tasks_and_structured_climate_risks() -> None:
    rows = [
        {
            "대응기반영역": "교육소통",
            "과제명": "시민 기후교육",
            "주요내용": "학교와 시민 대상 교육 운영",
            "주관부서": "환경과",
            "출처페이지": 30,
        },
        {
            "대응기반영역": "적응대책",
            "평가유형": "vulnerability",
            "기후변수": "폭염일수",
            "리스크항목": "온열질환",
            "값": 0.8,
            "단위": "지수",
            "출처페이지": 31,
        },
    ]

    retained, records = guard_foundation_measures(rows)

    assert retained == rows
    assert records == []


def test_12_quarantines_repeated_heading_when_stronger_row_exists() -> None:
    weak = {
        "대응기반영역": "국제협력",
        "과제명": "국제협력 및 지자체 간 협력",
        "주요내용": "국제협력 및 지자체 간 협력",
        "출처페이지": 40,
    }
    strong = {
        "대응기반영역": "국제협력",
        "과제명": "국제협력 및 지자체 간 협력",
        "주요내용": "도시간 공동 연구와 정책 교류",
        "주관부서": "기후정책과",
        "기간": "2025~2030",
        "출처페이지": 41,
    }

    retained, records = guard_foundation_measures([weak, strong])

    assert retained == [strong]
    assert records[0]["reason_codes"] == ["repeated_heading_echo"]


def test_12_keeps_heading_like_row_when_it_is_the_only_source() -> None:
    only_row = {
        "대응기반영역": "공유재산",
        "과제명": "공유재산 영향 및 대응",
        "주요내용": "공유재산 영향 및 대응",
        "출처페이지": 50,
    }

    retained, records = guard_foundation_measures([only_row])

    assert retained == [only_row]
    assert records == []


def test_pipeline_preserves_quarantine_ledger_and_groups_warnings() -> None:
    agent = OrganizerAgent()
    cleaned = agent.organize({
        "municipality_name": "가상도",
        "reduction_targets": [
            _target(
                목표수준="세부사업",
                사업명힌트="공공건물 효율화",
                목표감축량=25,
                출처페이지=61,
            )
        ],
        "quantitative_reductions": [{
            "사업명": "공공건물 효율화",
            "연도": 2030,
            "예상감축량": 25,
            "출처페이지": 61,
        }],
        "foundation_measures": [{
            "대응기반영역": "적응대책",
            "주요내용": "기후변화 대응 필요성을 검토한다",
            "출처페이지": 62,
        }],
    })

    assert cleaned["reduction_targets"] == []
    assert cleaned["foundation_measures"] == []
    assert len(cleaned["semantic_contract_review"]) == 2
    grouped = [
        issue
        for issue in cleaned["validation_report"]
        if issue["항목"] == "의미 계약 과잉 추출 격리"
    ]
    assert {issue["대상시트키"] for issue in grouped} == {
        "reduction_targets",
        "foundation_measures",
    }
    assert "semantic_contract_review" not in agent.get_excel_ready()


def test_guard_can_be_disabled_for_ab_comparison(monkeypatch) -> None:
    monkeypatch.setattr(config, "SEMANTIC_OVEREXTRACTION_GUARD_ENABLED", False)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상군",
        "foundation_measures": [{
            "주요내용": "일반 서술",
            "출처페이지": 70,
        }],
    })

    assert len(cleaned["foundation_measures"]) == 1
    assert cleaned["semantic_contract_review"] == []
