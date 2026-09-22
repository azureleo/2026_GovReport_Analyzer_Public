"""감축목표 전용 문맥 분류기의 일반화·보수적 게이트 테스트."""

import config
from agents.organizer_agent import OrganizerAgent
from utils.reduction_target_context import classify_reduction_target_rows


def _target(**overrides) -> dict:
    row = {
        "지자체명": "가상군",
        "목표수준": "부문",
        "목표범위": "관리권한",
        "부문": "건물",
        "기준연도": 2018,
        "목표연도": 2030,
        "목표감축량": 120,
        "출처페이지": 842,
    }
    row.update(overrides)
    return row


def _object(caption: str, rows: list[list[str]], *, evidence: str = "") -> dict:
    metadata = {"final_status": "extracted"}
    if evidence:
        metadata["evidence_id"] = evidence
    return {
        "object_id": "p842_table_1",
        "object_type": "table",
        "page_number": 842,
        "sequence": 1,
        "caption": caption,
        "section": caption,
        "rows": rows,
        "metadata": metadata,
    }


def test_서울_페이지_연도에_의존하지_않고_사업카드를_분류한다() -> None:
    rows = [_target()]
    decisions = classify_reduction_target_rows(rows, {
        "document_objects": [_object(
            "건물 분야 세부 감축사업",
            [["관리번호", "사업명", "성과지표", "기준연도", "목표연도", "목표감축량"],
             ["B-7", "공공청사 개선", "에너지 절감", "2018", "2030", "120"]],
        )],
        "mitigation_projects": [{"관리번호": "B-7", "사업명": "공공청사 개선", "출처페이지": 842}],
    })

    assert decisions[0].status == "retag"
    assert decisions[0].suggested_level == "세부사업"
    assert decisions[0].source_pages == (842,)


def test_감축목표_구조표는_같은페이지의_사업행이_있어도_보존한다() -> None:
    rows = [_target(목표수준="총괄", 부문="합계", 목표배출량=880, 감축률=12)]
    decisions = classify_reduction_target_rows(rows, {
        "document_objects": [_object(
            "부문별 온실가스 감축목표",
            [["기준연도", "목표연도", "목표배출량", "감축률"],
             ["2018", "2030", "880", "12"]],
        )],
        "mitigation_projects": [{"사업명": "인접 페이지 사업", "출처페이지": 842}],
    })

    assert decisions[0].status == "keep"
    assert decisions[0].suggested_level == "총괄"
    assert "matched_target_summary_object" in decisions[0].reason_codes


def test_정확한_근거ID_교차시트일치는_세부사업_강한근거다() -> None:
    rows = [_target(근거ID="ev-project-1")]
    decisions = classify_reduction_target_rows(rows, {
        "mitigation_projects": [{
            "사업명": "공공청사 개선",
            "근거ID": "ev-project-1",
            "출처페이지": 842,
        }],
    })

    assert decisions[0].status == "retag"
    assert decisions[0].suggested_level == "세부사업"
    assert "exact_related_sheet_evidence" in decisions[0].reason_codes


def test_단일_사업용어만으로_세부사업에_자동변경하지_않는다() -> None:
    rows = [_target()]
    decisions = classify_reduction_target_rows(rows, {
        "document_objects": [_object(
            "사업명별 감축량",
            [["기준연도", "목표연도", "목표감축량"], ["2018", "2030", "120"]],
        )],
        "mitigation_projects": [{"사업명": "인접 사업", "출처페이지": 842}],
    })

    assert decisions[0].status == "needs_review"
    assert decisions[0].suggested_level == "부문"
    assert "score_or_margin_below_gate" in decisions[0].reason_codes


def test_비전정책표의_추진계획과_세부사업_단어를_사업카드로_보지_않는다() -> None:
    rows = [_target(목표수준="총괄", 감축률=40, 목표감축량=None)]
    decisions = classify_reduction_target_rows(rows, {
        "document_objects": [_object(
            "기후 에너지계획 비전 및 정책 목표",
            [["연도", "기후·에너지계획", "비전", "정책 목표"],
             ["2030", "온실가스 감축 추진계획", "지속가능한 도시", "세부사업 추진"]],
        )],
    })

    assert decisions[0].status != "retag"
    assert decisions[0].suggested_level == "총괄"


def test_기존_세부사업이_목표구조표와_충돌하면_부문으로_복원한다() -> None:
    rows = [_target(목표수준="세부사업", 목표배출량=880, 감축률=12)]
    decisions = classify_reduction_target_rows(rows, {
        "document_objects": [_object(
            "부문별 온실가스 감축목표",
            [["부문", "기준연도", "목표연도", "목표배출량", "감축률"],
             ["건물", "2018", "2030", "880", "12"]],
        )],
    })

    assert decisions[0].status == "retag"
    assert decisions[0].suggested_level == "부문"
    assert "specialized_level_conflicts_with_target_summary" in decisions[0].reason_codes


def test_기존_특수수준은_근거가_없으면_낮은신뢰도로_보존한다() -> None:
    decisions = classify_reduction_target_rows([_target(목표수준="연차경로")], {})

    assert decisions[0].status == "keep"
    assert decisions[0].confidence == "low"
    assert decisions[0].suggested_level == "연차경로"
    assert "specialized_level_unverified" in decisions[0].reason_codes


def test_판정원장은_엑셀본문에서_제외되는_내부데이터다() -> None:
    agent = OrganizerAgent()
    cleaned = agent.organize({
        "municipality_name": "가상군",
        "reduction_targets": [_target()],
    })

    assert len(cleaned["reduction_target_context"]) == 1
    assert "reduction_target_context" not in agent.get_excel_ready()


def test_off_모드는_입력수준을_변경하지_않는다(monkeypatch) -> None:
    monkeypatch.setattr(config, "REDUCTION_TARGET_CONTEXT_MODE", "off")
    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상군",
        "reduction_targets": [_target()],
        "mitigation_projects": [{"사업명": "사업", "출처페이지": 842}],
    })

    assert cleaned["reduction_targets"][0]["목표수준"] == "부문"
    assert cleaned["reduction_target_context"][0]["status"] == "keep"
    assert cleaned["reduction_target_context"][0]["mode"] == "off"
