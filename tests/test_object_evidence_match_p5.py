from __future__ import annotations

from utils.object_evidence_match import descriptor, match_object_evidence
from utils.pdf_reader import PDFContent, PageContent
from utils.source_verifier import build_source_object_inventory


def test_exact_evidence_id_has_priority_over_text_similarity() -> None:
    expected = descriptor(
        key="source", page=10, evidence_ids="ev-10", number="표 3-1",
        caption="표 3-1 감축 목표", content="2030 40.4%",
    )
    candidates = [
        descriptor(
            key="wrong-text", page=10, evidence_ids="ev-10",
            caption="전혀 다른 캡션", content="다른 값",
        )
    ]

    result = match_object_evidence(expected, candidates)

    assert result.status == "exact_id"
    assert result.confirmed is True


def test_exact_evidence_id_with_page_conflict_requires_review() -> None:
    result = match_object_evidence(
        descriptor(key="source", page=10, evidence_ids="ev-10"),
        [descriptor(key="result", page=11, evidence_ids="ev-10")],
    )

    assert result.status == "ambiguous"
    assert result.confirmed is False


def test_number_caption_and_key_value_form_a_composite_match() -> None:
    result = match_object_evidence(
        descriptor(
            key="source", page=20, number="표 5-2",
            caption="표 5-2 부문별 온실가스 감축 목표",
            content="2030 40.4% 건물",
        ),
        [descriptor(
            key="result", page=20, evidence_ids="legacy-20",
            number="[표 5-2]", caption="표 5-2 부문별 온실가스 감축 목표",
            content="건물 | 2030 | 40.4%",
        )],
    )

    assert result.status == "composite"
    assert "40.4%" in result.numeric_hits


def test_same_page_without_object_identity_is_not_a_match() -> None:
    result = match_object_evidence(
        descriptor(
            key="source", page=30, number="그림 7-1",
            caption="그림 7-1 폭염 취약성 지도", content="2040 높음",
        ),
        [descriptor(
            key="other", page=30, number="그림 7-2",
            caption="그림 7-2 홍수 위험도", content="2050 낮음",
        )],
    )

    assert result.status == "missing"


def test_matching_number_and_caption_without_key_value_is_partial() -> None:
    result = match_object_evidence(
        descriptor(
            key="source", page=40, number="표 8-1",
            caption="표 8-1 연차별 투자계획", content="2030 120억원",
        ),
        [descriptor(
            key="result", page=40, number="표 8-1",
            caption="표 8-1 연차별 투자계획", content="제목만 기록",
        )],
    )

    assert result.status == "partial"
    assert result.confirmed is False


def _document() -> PDFContent:
    return PDFContent(
        total_pages=1,
        pages=[PageContent(page_number=1, text="본문", tables=[], images=[])],
        full_text="본문",
    )


def test_skip_non_data_object_is_preserved_outside_extraction_denominator() -> None:
    final_data = {
        "document_objects": [{
            "object_id": "p1_image_1",
            "object_type": "image",
            "page_number": 1,
            "sequence": 1,
            "caption": "그림 1-1 행사 사진",
            "metadata": {
                "triage_action": "skip_non_data",
                "final_status": "not_relevant",
                "evidence_id": "ev-photo",
            },
        }],
    }

    report = build_source_object_inventory(final_data, _document())

    assert report.total_objects == 1
    assert report.extraction_denominator_objects == 0
    assert report.excluded_candidate_objects == 1
    assert report.rows[0].status == "제외후보"
    assert report.rows[0].evaluation_target == "triage_negative"


def test_strong_data_object_skipped_by_triage_is_counted_as_miss_candidate() -> None:
    final_data = {
        "document_objects": [{
            "object_id": "p1_chart_1",
            "object_type": "chart",
            "page_number": 1,
            "sequence": 1,
            "caption": "그림 1-2 2030년 감축목표 40.4%",
            "metadata": {
                "triage_action": "skip_non_data",
                "triage_reasons": ["strong:chart_structure"],
                "final_status": "not_relevant",
                "evidence_id": "ev-chart",
            },
        }],
    }

    report = build_source_object_inventory(final_data, _document())

    assert report.extraction_denominator_objects == 1
    assert report.triage_missed_objects == 1
    assert report.triage_miss_rate == 1.0
    assert report.rows[0].triage_evaluation_status == "false_negative_candidate"
