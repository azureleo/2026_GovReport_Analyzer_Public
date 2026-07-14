"""소수정 v6.3 결정론 회귀 테스트."""

from agents.organizer_agent import OrganizerAgent


def _감축목표행(**overrides):
    row = {
        "지자체명": "테스트시",
        "목표수준": "총괄",
        "목표범위": "지역전체",
        "부문": "건물",
        "기준연도": 2018,
        "목표연도": 2030,
    }
    row.update(overrides)
    return row


def _감축목표정제(rows):
    return OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "reduction_targets": rows,
    })


def test_S1_감축목표는_기준연도가_다르면_병합하지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준연도=2005, 기준배출량=100),
        _감축목표행(기준연도=2018, 기준배출량=100),
    ])

    assert [row["기준연도"] for row in cleaned["reduction_targets"]] == [2005, 2018]


def test_S1_감축목표의_빈_기준연도는_수치충돌이_없으면_흡수한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준연도=None, 기준배출량=100, 목표배출량=70),
    ])

    assert len(cleaned["reduction_targets"]) == 1
    assert cleaned["reduction_targets"][0]["기준연도"] == 2018
    assert cleaned["reduction_targets"][0]["목표배출량"] == 70.0


def test_S1_감축목표의_빈_기준연도는_수치충돌이면_분리하고_기록한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준연도=None, 기준배출량=120),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert any("중복 키 값 충돌" in issue["항목"] for issue in cleaned["validation_report"])


def test_S1_감축목표의_공통필드가_천배면_선행값을_유지하고_채우지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=49_445),
        _감축목표행(기준배출량=49_445_000, 목표감축량=9_802_000),
    ])

    rows = cleaned["reduction_targets"]
    assert len(rows) == 1
    assert rows[0]["기준배출량"] == 49_445.0
    assert rows[0]["목표감축량"] is None
    assert any("스케일 표기 차 의심(톤↔천톤)" in issue["항목"] for issue in cleaned["validation_report"])


def test_S1_감축목표의_스케일서명이_천배면_교차채움하지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=49_445),
        _감축목표행(목표감축량=49_445_000),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert [row.get("목표감축량") for row in cleaned["reduction_targets"]] == [None, 49_445_000.0]
    assert [row.get("기준배출량") for row in cleaned["reduction_targets"]] == [49_445.0, None]


def test_S1_감축목표의_일반_수치충돌은_기존_충돌경로를_유지한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준배출량=120),
    ])

    assert len(cleaned["reduction_targets"]) == 1
    assert cleaned["reduction_targets"][0]["데이터상태"] == "conflicting"
    assert any("중복 키 값 충돌" in issue["항목"] for issue in cleaned["validation_report"])
