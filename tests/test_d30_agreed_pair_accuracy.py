"""D3-0 합의쌍 골든 정답률 측정 도구의 산식 회귀 테스트."""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.d30_agreed_pair_accuracy import measure
from scripts.golden_score_contract import 계약헤더, 골든열

_시트 = "05_배출전망"


def _출력워크북(path: Path, forecasts: dict[str, int]) -> None:
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = _시트
    headers = 계약헤더(_시트)
    ws.append(headers)
    for 부문, 값 in forecasts.items():
        row = {"지자체명": "서울", "시나리오": "BAU", "부문": 부문, "연도": 2030, "전망값": 값, "단위": "천톤"}
        ws.append([row.get(header) for header in headers])
    workbook.save(path)


def _골든워크북(path: Path, forecasts: dict[str, int]) -> None:
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = _시트
    headers = 계약헤더(_시트) + list(골든열)
    ws.append(headers)
    for 부문, 값 in forecasts.items():
        row = {
            "지자체명": "서울", "시나리오": "BAU", "부문": 부문, "연도": 2030,
            "전망값": 값, "단위": "천톤",
            "골든_출처유형": "텍스트표", "골든_출처페이지": 10,
        }
        ws.append([row.get(header) for header in headers])
    workbook.save(path)


def test_합의쌍_q는_골든과의_교집합에서만_계산된다(tmp_path):
    a_path = tmp_path / "a.xlsx"
    b_path = tmp_path / "b.xlsx"
    golden_path = tmp_path / "golden.xlsx"
    # 건물: A=B=골든(합의·정답) / 수송: A=B≠골든(합의·공통 오류) / 폐기물: A≠B(불합의)
    _출력워크북(a_path, {"건물": 100, "수송": 200, "폐기물": 300})
    _출력워크북(b_path, {"건물": 100, "수송": 200, "폐기물": 999})
    _골든워크북(golden_path, {"건물": 100, "수송": 250, "폐기물": 300})

    result = measure(a_path, b_path, golden_path)
    stats = result["시트별"][_시트]

    assert stats["값비교쌍수"] == 3
    assert stats["값합의쌍수"] == 2
    assert stats["골든매칭_합의쌍수"] == 2
    assert stats["골든미판정_합의쌍수"] == 0
    # 필드 축: 건물(전망값·단위 일치 2/2) + 수송(단위만 일치 1/2) = 3/4
    assert stats["q_필드"] == 0.75
    # 행 축: 건물만 전 필드 일치 = 1/2
    assert stats["q_행"] == 0.5
    detail = stats["합의쌍_골든불일치_상세"]
    assert len(detail) == 1 and detail[0]["필드"] == "전망값"
    assert result["LLM호출수"] == 0
