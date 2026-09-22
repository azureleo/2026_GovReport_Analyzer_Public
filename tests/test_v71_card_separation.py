"""06_감축목표 문맥 분류와 v7-1 호환 모드 회귀 테스트."""

import config
from agents.organizer_agent import OrganizerAgent
from utils.reference_data import build_codebook_rows


def _target_row(**overrides) -> dict:
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


def _organize_targets(rows: list[dict], **other_raw) -> dict:
    return OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "reduction_targets": rows,
        **other_raw,
    })


def _table_object(page: int, caption: str, rows: list[list[str]]) -> dict:
    return {
        "object_id": f"p{page}_table_1",
        "object_type": "table",
        "page_number": page,
        "sequence": 1,
        "caption": caption,
        "section": caption,
        "rows": rows,
        "metadata": {"final_status": "extracted"},
    }


def test_S1_사업시트와_페이지만_겹치면_부문행을_보존한다(capsys) -> None:
    cleaned = _organize_targets(
        [
            _target_row(출처페이지="10,11"),
            _target_row(출처페이지=99, 부문="수송", 목표감축량=200),
        ],
        mitigation_projects=[{"사업명": "고효율 건물 사업", "출처페이지": 10}],
        annual_implementation=[{"사업명": "고효율 건물 사업", "연도": 2030, "출처페이지추정": 11}],
    )

    assert [row["목표수준"] for row in cleaned["reduction_targets"]] == ["부문", "부문"]
    assert all("_재태깅사유" not in row for row in cleaned["reduction_targets"])
    assert "재태깅 0건" in capsys.readouterr().out


def test_S1_수치서명과_사업카드헤더가_같은_객체면_세부사업으로_재태깅한다() -> None:
    cleaned = _organize_targets(
        [_target_row(출처페이지=10)],
        mitigation_projects=[{"관리번호": "B-1", "사업명": "고효율 건물 사업", "출처페이지": 10}],
        document_objects=[_table_object(
            10,
            "건물 부문 감축사업 카드",
            [["관리번호", "사업명", "성과지표", "기준연도", "목표연도", "목표감축량"],
             ["B-1", "고효율 건물 사업", "에너지 절감", "2018", "2030", "100"]],
        )],
    )

    row = cleaned["reduction_targets"][0]
    assert row["목표수준"] == "세부사업"
    assert row["_재태깅사유"] == "문맥분류_세부사업"
    decision = cleaned["reduction_target_context"][0]
    assert decision["status"] == "retag"
    assert "matched_project_card_object" in decision["reason_codes"]


def test_S1_출처페이지일부만_사업문맥이면_목표장행을_보존한다() -> None:
    cleaned = _organize_targets(
        [_target_row(출처페이지="10,99")],
        quantitative_reductions=[{"사업명": "고효율 건물 사업", "출처페이지": 10}],
    )

    assert cleaned["reduction_targets"][0]["목표수준"] == "부문"
    assert "_재태깅사유" not in cleaned["reduction_targets"][0]


def test_S1_교차시트신호가_없는_폐루프_정리는_noop이다() -> None:
    cleaned = OrganizerAgent().organize_sheet(
        "reduction_targets",
        [_target_row(출처페이지=10)],
        "테스트시",
    )

    assert cleaned[0]["목표수준"] == "부문"
    assert "_재태깅사유" not in cleaned[0]


def test_S2_연속4연도와_시트페이지겹침만으로는_재태깅하지_않는다() -> None:
    cleaned = _organize_targets(
        [
            _target_row(목표연도=year, 목표감축량=year - 2000, 출처페이지=20)
            for year in (2029, 2030, 2031, 2032)
        ],
        annual_implementation=[{"사업명": "연차 사업", "연도": 2030, "출처페이지": 20}],
    )

    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"부문"}
    assert {row["목표연도"] for row in cleaned["reduction_targets"]} == {2029, 2030, 2031, 2032}
    assert {row["status"] for row in cleaned["reduction_target_context"]} == {"needs_review"}


def test_S2_명시적_연차감축경로_객체면_연차경로로_재태깅한다() -> None:
    rows = [
        _target_row(목표연도=year, 목표감축량=year - 2000, 출처페이지=20)
        for year in (2029, 2030, 2031, 2032)
    ]
    cleaned = _organize_targets(
        rows,
        document_objects=[_table_object(
            20,
            "연차별 온실가스 감축량 및 감축경로",
            [["연도", "2029", "2030", "2031", "2032"], ["감축량", "29", "30", "31", "32"]],
        )],
    )

    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"연차경로"}
    assert all(row["_재태깅사유"] == "문맥분류_연차경로" for row in cleaned["reduction_targets"])
    assert all(
        "matched_annual_path_object" in item["reason_codes"]
        for item in cleaned["reduction_target_context"]
    )


def test_S2_5년간격_이정표표는_기존_목표수준을_보존한다() -> None:
    cleaned = _organize_targets([
        _target_row(목표연도=year, 목표감축량=year - 2000, 출처페이지=20)
        for year in (2030, 2035, 2040, 2045)
    ])

    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"부문"}


def test_S2_연속구간길이3이하는_기존_목표수준을_보존한다() -> None:
    cleaned = _organize_targets([
        _target_row(목표연도=year, 목표감축량=year - 2000, 출처페이지=20)
        for year in (2029, 2030, 2031)
    ])

    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"부문"}


def test_S2는_S1보다_우선하고_최종_검증사유도_연차문맥이다() -> None:
    cleaned = _organize_targets(
        [
            _target_row(목표연도=year, 목표감축량=year - 2000, 출처페이지=20)
            for year in (2029, 2030, 2031, 2032)
        ],
        mitigation_projects=[{"사업명": "연차 전개 사업", "출처페이지": 20}],
        document_objects=[_table_object(
            20,
            "연도별 감축경로",
            [["연도", "2029", "2030", "2031", "2032"], ["감축량", "29", "30", "31", "32"]],
        )],
    )

    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"연차경로"}
    retag_issues = [
        issue for issue in cleaned["validation_report"]
        if issue["항목"].startswith("목표수준 문맥 재태깅")
    ]
    assert len(retag_issues) == 4
    assert all("matched_annual_path_object" in issue["문제내용"] for issue in retag_issues)
    assert all("matched_project_card_object" not in issue["문제내용"] for issue in retag_issues)


def test_카드행과_진짜부문행은_재태깅후_dedup키가_분리된다() -> None:
    cleaned = _organize_targets(
        [
            _target_row(출처페이지=10, 목표감축량=86_200),
            _target_row(출처페이지=99, 목표감축량=9_877),
        ],
        mitigation_projects=[{"사업명": "카드 사업", "출처페이지": 10}],
        document_objects=[_table_object(
            10,
            "감축사업 카드",
            [["관리번호", "사업명", "성과지표", "목표연도", "목표감축량"],
             ["B-1", "카드 사업", "감축량", "2030", "86200"]],
        )],
    )

    assert len(cleaned["reduction_targets"]) == 2
    assert {row["목표수준"] for row in cleaned["reduction_targets"]} == {"세부사업", "부문"}


def test_legacy_모드는_v7_페이지규칙을_재현한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "REDUCTION_TARGET_CONTEXT_MODE", "legacy")
    cleaned = _organize_targets(
        [_target_row(출처페이지=10)],
        mitigation_projects=[{"사업명": "카드 사업", "출처페이지": 10}],
    )

    assert cleaned["reduction_targets"][0]["목표수준"] == "세부사업"
    assert cleaned["reduction_targets"][0]["_재태깅사유"] == "사업문맥페이지"
    assert cleaned["reduction_target_context"][0]["mode"] == "legacy"


def test_S3_같은값의_지역전체_총괄행은_관리권한행에_흡수하고_경고한다() -> None:
    cleaned = _organize_targets([
        _target_row(
            목표수준="총괄",
            목표범위="지역전체",
            부문="합계",
            기준연도=2018,
            목표감축량=9_802,
            출처페이지="6,200,251",
        ),
        _target_row(
            목표수준="총괄",
            목표범위="관리권한",
            부문="전체",
            기준연도=2005,
            목표감축량=9_850,
            목표배출량=19_778,
            감축률=40,
            출처페이지=185,
        ),
    ])

    assert len(cleaned["reduction_targets"]) == 1
    kept = cleaned["reduction_targets"][0]
    assert kept["목표범위"] == "관리권한"
    assert kept["기준연도"] == 2005
    assert kept["목표감축량"] == 9_850.0
    assert kept["출처페이지"] == "6,185,200,251"
    assert any(
        issue["심각도"] == "경고"
        and issue["항목"].startswith("목표범위혼입의심")
        and issue["대상시트키"] == "reduction_targets"
        and issue["대상행번호"] == 1
        for issue in cleaned["validation_report"]
    )


def test_S3_값이_다른_지역전체_총괄행은_보존한다() -> None:
    cleaned = _organize_targets([
        _target_row(
            목표수준="총괄",
            목표범위="지역전체",
            부문="합계",
            목표감축량=9_802,
            출처페이지=6,
        ),
        _target_row(
            목표수준="총괄",
            목표범위="관리권한",
            부문="합계",
            목표감축량=10_000,
            목표배출량=19_778,
            출처페이지=185,
        ),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert {row["목표범위"] for row in cleaned["reduction_targets"]} == {"지역전체", "관리권한"}
    assert not any(
        issue["항목"].startswith("목표범위혼입의심")
        for issue in cleaned["validation_report"]
    )


def test_S3_관리권한행이_목표구조를_갖추지_않으면_흡수하지_않는다() -> None:
    cleaned = _organize_targets([
        _target_row(
            목표수준="총괄",
            목표범위="지역전체",
            부문="",
            목표감축량=9_802,
            출처페이지=6,
        ),
        _target_row(
            목표수준="총괄",
            목표범위="관리권한",
            부문="합계",
            목표감축량=9_802,
            목표배출량=None,
            감축률=None,
            출처페이지=185,
        ),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert not any(
        issue["항목"].startswith("목표범위혼입의심")
        for issue in cleaned["validation_report"]
    )


def test_S4_코드북에_목표수준_코드체계를_정의한다() -> None:
    target_level_rows = {
        row["코드"]: row
        for row in build_codebook_rows()
        if row["코드유형"] == "목표수준"
    }

    assert set(target_level_rows) == {"총괄", "부문", "세부부문", "세부사업", "연차경로"}
    assert target_level_rows["세부사업"]["정의"] == "개별 사업 카드의 감축 목표"
    assert target_level_rows["연차경로"]["정의"] == (
        "연차별 감축 경로표에서 유래한 행 — 목표연도 시점의 목표가 아님"
    )
