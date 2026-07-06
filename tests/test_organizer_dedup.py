import unittest

from agents.organizer_agent import OrganizerAgent, _deduplicate_rows


class OrganizerDedupTests(unittest.TestCase):
    def test_dedup_key_normalizes_spacing_and_middle_dot_variants(self):
        # Given: 같은 키가 공백/가운뎃점 표기만 다르게 들어오면
        rows = [
            {"지자체명": "서울", "부문": "도로 수송", "연도": 2030, "배출량": 10, "출처페이지": 12},
            {"지자체명": "서울", "부문": "도로수송", "연도": 2030, "배출량": 10, "단위": "천톤", "출처페이지": 13},
            {"지자체명": "서울", "부문": "공원·녹지", "연도": 2030, "배출량": 1},
            {"지자체명": "서울", "부문": "공원ㆍ녹지", "연도": 2030, "배출량": 1},
        ]

        # When: dedup 키를 만들면
        deduped = _deduplicate_rows(rows, ["지자체명", "부문", "연도"])

        # Then: 표기 차이 행은 합쳐지고 provenance는 병합된다.
        self.assertEqual(len(deduped), 2)
        road = next(row for row in deduped if row["부문"] == "도로 수송")
        self.assertEqual(road["출처페이지"], "12,13")
        self.assertEqual(road["단위"], "천톤")

    def test_blank_detail_row_is_absorbed_when_values_do_not_conflict(self):
        # Given: 세부부문만 빈 행과 채워진 행이 같은 값을 담고 있으면
        rows = [
            {"지자체명": "서울", "배출유형": "직접배출", "부문": "수송", "세부부문": "", "연도": 2030, "배출량": 10, "단위": "천톤"},
            {"지자체명": "서울", "배출유형": "직접배출", "부문": "수송", "세부부문": "도로", "연도": 2030, "배출량": 10},
        ]

        # When: 세부부문을 포함한 키로 dedup하면
        deduped = _deduplicate_rows(rows, ["지자체명", "배출유형", "부문", "세부부문", "연도"])

        # Then: 빈 세부행은 채워진 세부행에 흡수된다.
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["세부부문"], "도로")
        self.assertEqual(deduped[0]["단위"], "천톤")

    def test_conflicting_blank_detail_rows_are_kept_and_reported(self):
        # Given: 같은 논리 키에서 값이 충돌하면
        raw = {
            "municipality_name": "서울특별시",
            "emissions_regional": [
                {"지자체명": "서울특별시", "배출유형": "직접배출", "부문": "수송", "세부부문": "", "연도": 2030, "배출량": 10},
                {"지자체명": "서울특별시", "배출유형": "직접배출", "부문": "수송", "세부부문": "도로", "연도": 2030, "배출량": 20},
            ],
        }

        # When: organize가 dedup/검증 리포트를 만든다.
        cleaned = OrganizerAgent().organize(raw)

        # Then: 두 행을 모두 유지하고 충돌을 검증리포트에 남긴다.
        self.assertEqual(len(cleaned["emissions_regional"]), 2)
        self.assertTrue(any("중복 키 값 충돌" in row["항목"] for row in cleaned["validation_report"]))

    def test_management_direct_label_variants_share_dedup_key(self):
        # Given: 관리권한 직간접구분이 영어/한국어 표기만 다르게 들어오면
        raw = {
            "municipality_name": "서울특별시",
            "emissions_management": [
                {"지자체명": "서울특별시", "관리부문": "건물", "세부부문": "가정", "직간접구분": "direct", "연도": 2020, "배출량": 10, "단위": "천톤"},
                {"지자체명": "서울특별시", "관리부문": "건물", "세부부문": "가정", "직간접구분": "직접", "연도": 2020, "배출량": 10},
            ],
        }

        # When: organizer가 관리권한 시트를 정제하면
        cleaned = OrganizerAgent().organize(raw)

        # Then: 직간접구분 정규화 후 같은 dedup 키로 병합된다.
        self.assertEqual(cleaned["emissions_management"], [
            {"지자체명": "서울특별시", "관리부문": "건물", "세부부문": "가정", "직간접구분": "직접", "연도": 2020, "배출량": 10.0, "단위": "천톤", "데이터상태": "reported"}
        ])


if __name__ == "__main__":
    unittest.main()
