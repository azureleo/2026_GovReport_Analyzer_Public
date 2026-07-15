from __future__ import annotations

from agents.organizer_agent import OrganizerAgent


def test_시트03_표준부문_괄호_한정어를_빈_세부부문으로_분리한다() -> None:
    # Given: 표준 부문 뒤 괄호에 세부 한정어가 붙은 지역 인벤토리 행
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "에너지(연료 공급량 기준)", "세부부문": "", "연도": 2020, "배출량": 10},
        ],
    }

    # When: organizer가 지역 배출현황을 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 부문은 표준 부문으로, 괄호 한정어는 공백 없는 세부부문으로 분리된다.
    row = cleaned["emissions_regional"][0]
    assert row["부문"] == "에너지"
    assert row["세부부문"] == "연료공급량기준"
    assert row["부문원문"] == "에너지(연료 공급량 기준)"


def test_시트03_표준부문_괄호_한정어는_기존_세부부문을_덮어쓰지_않는다() -> None:
    # Given: 괄호 한정어가 있지만 세부부문이 이미 채워진 지역 인벤토리 행
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "에너지(연료 공급량 기준)", "세부부문": "원문세부", "연도": 2020, "배출량": 10},
        ],
    }

    # When: organizer가 지역 배출현황을 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 세부부문 원문은 보존하고 부문만 표준 부문으로 정리된다.
    row = cleaned["emissions_regional"][0]
    assert row["부문"] == "에너지"
    assert row["세부부문"] == "원문세부"


def test_시트03_ipcc_코드_프리픽스를_국가_인벤토리_대분류로_정규화한다() -> None:
    # Given: IPCC 코드가 부문 필드 앞에 들어온 지역 인벤토리 행
    raw = {
        "municipality_name": "강원특별자치도",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "1A 연료연소", "세부부문": "", "연도": 2020, "배출량": 10},
            {"배출유형": "직접배출", "부문": "3C7 벼재배", "세부부문": "", "연도": 2020, "배출량": 5},
        ],
    }

    # When: organizer가 지역 배출현황을 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: IPCC 대분류는 표준 부문으로, 원문 코드는 세부부문과 원문 필드에 보존된다.
    by_detail = {row["세부부문"]: row for row in cleaned["emissions_regional"]}
    assert by_detail["1A 연료연소"]["부문"] == "에너지"
    assert by_detail["1A 연료연소"]["부문원문"] == "1A 연료연소"
    assert by_detail["3C7 벼재배"]["부문"] == "농업"
    assert by_detail["3C7 벼재배"]["부문원문"] == "3C7 벼재배"


def test_시트04_관리부문도_괄호_한정어와_ipcc_코드를_일반_규칙으로_정규화한다() -> None:
    # Given: 관리권한 시트의 관리부문에도 같은 비정형 부문 표기가 들어오면
    raw = {
        "municipality_name": "경기도",
        "emissions_management": [
            {"관리부문": "전력(간접 사용량)", "세부부문": "", "직간접구분": "", "연도": 2020, "배출량": 10},
            {"관리부문": "4A 폐기물 매립(직접)", "세부부문": "", "직간접구분": "direct", "연도": 2021, "배출량": 20},
        ],
    }

    # When: organizer가 관리권한 배출현황을 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 특정 지자체 표기가 아니라 표준 괄호/IPCC 규칙으로 정규화된다.
    by_year = {row["연도"]: row for row in cleaned["emissions_management"]}
    assert by_year[2020]["관리부문"] == "전력"
    assert by_year[2020]["세부부문"] == "간접사용량"
    assert by_year[2020]["직간접구분"] == "간접"
    assert by_year[2021]["관리부문"] == "폐기물"
    assert by_year[2021]["세부부문"] == "4A 폐기물 매립(직접)"
    assert by_year[2021]["직간접구분"] == "직접"


def test_시트03_빈_배출유형_행은_동일_키_채움_행에_흡수된다() -> None:
    # Given: 배출유형만 빈 행이 같은 부문·세부부문·연도·수치를 가진 행과 함께 들어오면
    raw = {
        "municipality_name": "강원특별자치도",
        "emissions_regional": [
            {"배출유형": "", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 10, "단위": "천톤"},
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 10},
        ],
    }

    # When: organizer가 지역 배출현황을 dedup하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 빈 배출유형 행은 채워진 키 행에 흡수되고 보조 값은 병합된다.
    assert cleaned["emissions_regional"] == [
        {
            "지자체명": "강원특별자치도",
            "배출유형": "직접배출",
            "부문": "에너지",
            "세부부문": "연료연소",
            "연도": 2020,
            "배출량": 10.0,
            "데이터상태": "reported",
            "단위": "천톤",
        }
    ]


def test_시트03_빈_배출유형_행도_수치_충돌이면_흡수하지_않는다() -> None:
    # Given: 배출유형만 빈 행이 같은 논리 키에서 다른 수치를 갖고 있으면
    raw = {
        "municipality_name": "강원특별자치도",
        "emissions_regional": [
            {"배출유형": "", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 10},
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 20},
        ],
    }

    # When: organizer가 지역 배출현황을 dedup하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 행을 합치지 않고 중복 키 값 충돌로 보고한다.
    assert len(cleaned["emissions_regional"]) == 2
    assert any("중복 키 값 충돌" in row["항목"] for row in cleaned["validation_report"])


def test_시트04_빈_직간접구분_행은_동일_키_채움_행에_흡수된다() -> None:
    # Given: 직간접구분만 빈 관리권한 행이 같은 관리부문·세부부문·연도·수치를 가진 행과 함께 들어오면
    raw = {
        "municipality_name": "경기도",
        "emissions_management": [
            {"관리부문": "건물", "세부부문": "공공", "직간접구분": "", "연도": 2030, "배출량": 7, "단위": "천톤"},
            {"관리부문": "건물", "세부부문": "공공", "직간접구분": "간접", "연도": 2030, "배출량": 7},
        ],
    }

    # When: organizer가 관리권한 배출현황을 dedup하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 빈 직간접구분 행은 채워진 키 행에 흡수된다.
    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["emissions_management"][0]["직간접구분"] == "간접"
    assert cleaned["emissions_management"][0]["단위"] == "천톤"


def test_중복_값_충돌은_값_채움_필드가_많은_행을_결정론적으로_채택한다() -> None:
    # Given: 같은 dedup 키에서 수치가 충돌하고 뒤 행에 더 많은 원문 필드가 있으면
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 10},
            {"배출유형": "직접배출", "부문": "에너지", "세부부문": "연료연소", "연도": 2020, "배출량": 20, "단위": "천톤"},
        ],
    }

    # When: organizer가 중복 키 충돌을 정리하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 값 채움 필드가 많은 뒤 행을 채택하되 충돌 상태와 검증리포트를 유지한다.
    row = cleaned["emissions_regional"][0]
    assert row["배출량"] == 20.0
    assert row["데이터상태"] == "conflicting"
    assert any("채택근거: 값 채움 필드 수 우선" in issue["문제내용"] for issue in cleaned["validation_report"])


def test_중복_값_충돌은_동률이면_표_마커_출처_행을_채택한다() -> None:
    # Given: 값 채움 수가 같고 뒤 행 출처페이지에 표 마커가 있으면
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "폐기물", "세부부문": "매립", "연도": 2021, "배출량": 10, "출처페이지": "p1"},
            {"배출유형": "직접배출", "부문": "폐기물", "세부부문": "매립", "연도": 2021, "배출량": 20, "출처페이지": "[표 1-1] p2"},
        ],
    }

    # When: organizer가 중복 키 충돌을 정리하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 표 마커가 있는 행을 채택하고 충돌 기록에 근거를 남긴다.
    row = cleaned["emissions_regional"][0]
    assert row["배출량"] == 20.0
    assert row["출처페이지"] == "1,2"
    assert any("채택근거: 표 마커 출처 우선" in issue["문제내용"] for issue in cleaned["validation_report"])


def test_중복_값_충돌은_우선순위가_같으면_기존_first_wins를_유지한다() -> None:
    # Given: 값 채움 수와 표 마커 우선순위가 같은 충돌 행
    raw = {
        "municipality_name": "서울특별시",
        "emissions_regional": [
            {"배출유형": "직접배출", "부문": "농업", "세부부문": "벼재배", "연도": 2022, "배출량": 10, "단위": "천톤"},
            {"배출유형": "직접배출", "부문": "농업", "세부부문": "벼재배", "연도": 2022, "배출량": 20, "단위": "천톤"},
        ],
    }

    # When: organizer가 중복 키 충돌을 정리하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 기존 first-wins 동작을 보존하고 충돌 기록에 근거를 남긴다.
    row = cleaned["emissions_regional"][0]
    assert row["배출량"] == 10.0
    assert any("채택근거: 기존 순서 유지" in issue["문제내용"] for issue in cleaned["validation_report"])


def test_직간접구분_코드성_텍스트는_casefold로_정규화한다() -> None:
    # Given: 직간접구분이 대문자 영문 코드로 들어오면
    raw = {
        "municipality_name": "경기도",
        "emissions_management": [
            {"관리부문": "건물", "세부부문": "공공", "직간접구분": "DIRECT", "연도": 2030, "배출량": 7},
            {"관리부문": "건물", "세부부문": "가정", "직간접구분": "InDiReCt", "연도": 2030, "배출량": 3},
        ],
    }

    # When: organizer가 관리권한 배출현황을 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 대소문자와 무관하게 표준 직간접구분으로 매핑된다.
    by_subsector = {row["세부부문"]: row for row in cleaned["emissions_management"]}
    assert by_subsector["공공"]["직간접구분"] == "직접"
    assert by_subsector["가정"]["직간접구분"] == "간접"
