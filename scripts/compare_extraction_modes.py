"""
추출 모드 A/B 비교기 (LLM 토큰 0).

per-sheet 모드 결과 xlsx와 시트 클러스터링 모드 결과 xlsx를 시트별로 비교해,
클러스터링이 행을 빠뜨리지 않는지(=추출 리콜 무회귀) 점검한다.

비교 지표(시트별):
  - 데이터 행 수: 클러스터가 per-sheet보다 적으면 회귀 의심(⚠)
  - 채워진 셀 수: 행은 같아도 빈칸이 늘면 품질 저하 신호
  - 핵심 수치 시트(배출/목표/재정 등)의 숫자 셀 수

사용:
  # 1) per-sheet(기본) 추출
  python main.py "<source.pdf>" -o "output/persheet.xlsx"
  # 2) 클러스터링 추출 (같은 입력)
  EXTRACTION_SHEET_CLUSTERING=1 python main.py "<source.pdf>" -o "output/cluster.xlsx"
  # 3) 비교
  python scripts/compare_extraction_modes.py output/persheet.xlsx output/cluster.xlsx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

# 수치 정확도가 특히 중요한 핵심 시트(여기서 회귀가 나면 치명적).
_CRITICAL = {
    "03_배출현황_지역", "04_배출현황_관리권한", "05_배출전망",
    "06_감축목표", "10_정량감축량", "11_재정투자계획",
}


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.replace(",", "").replace("%", "").strip()
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _sheet_stats(ws) -> tuple[int, int, int]:
    """(데이터 행 수, 채워진 셀 수, 숫자 셀 수). 1행은 헤더로 보고 제외."""
    rows = cells = nums = 0
    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if r_idx == 0:
            continue
        filled = [c for c in row if c is not None and str(c).strip() != ""]
        if not filled:
            continue
        rows += 1
        cells += len(filled)
        nums += sum(1 for c in filled if _is_number(c))
    return rows, cells, nums


def _load(path: str) -> dict[str, tuple[int, int, int]]:
    wb = openpyxl.load_workbook(path, read_only=True)
    return {name: _sheet_stats(wb[name]) for name in wb.sheetnames}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("persheet", help="per-sheet 모드 결과 xlsx")
    ap.add_argument("cluster", help="클러스터링 모드 결과 xlsx")
    args = ap.parse_args()

    a = _load(args.persheet)
    b = _load(args.cluster)

    data_sheets = [n for n in config.EXCEL_HEADERS if not n.startswith(("17", "18", "19"))]
    names = [n for n in data_sheets if n in a or n in b]

    print(f"\nA = per-sheet : {args.persheet}")
    print(f"B = cluster   : {args.cluster}\n")
    hdr = f"{'sheet':22s} {'A행':>5s} {'B행':>5s} {'Δ행':>5s} {'A셀':>6s} {'B셀':>6s} {'A숫자':>6s} {'B숫자':>6s}  flag"
    print(hdr)
    print("-" * len(hdr))

    regressions: list[str] = []
    ta_r = tb_r = ta_c = tb_c = 0
    for name in names:
        ar, ac, an = a.get(name, (0, 0, 0))
        br, bc, bn = b.get(name, (0, 0, 0))
        ta_r += ar; tb_r += br; ta_c += ac; tb_c += bc
        flag = ""
        # 회귀 판정: 클러스터 행 수가 per-sheet보다 의미있게 적으면(>5% 또는 절대 -2 이상) 경고
        if br < ar and (ar - br) >= max(2, int(ar * 0.05)):
            flag = "⚠ 행감소"
            regressions.append(f"{name}: 행 {ar}→{br}")
        # 핵심 시트에서 숫자 셀이 줄면 더 강한 경고
        if name in _CRITICAL and bn < an and (an - bn) >= max(2, int(an * 0.05)):
            flag = (flag + " ⚠⚠숫자감소").strip()
            regressions.append(f"{name}: 숫자셀 {an}→{bn} (핵심시트)")
        print(f"{name:22s} {ar:5d} {br:5d} {br-ar:5d} {ac:6d} {bc:6d} {an:6d} {bn:6d}  {flag}")

    print("-" * len(hdr))
    print(f"{'TOTAL':22s} {ta_r:5d} {tb_r:5d} {tb_r-ta_r:5d} {ta_c:6d} {tb_c:6d}")
    print()
    if regressions:
        print("❌ 회귀 의심 — 클러스터링이 추출을 빠뜨렸을 수 있습니다:")
        for r in regressions:
            print("  -", r)
        print("\n→ 클러스터링을 기본 활성화하지 마세요. 프롬프트 조정 후 재검증이 필요합니다.")
        return 1
    print("✅ PASS — 모든 시트에서 클러스터 행 수가 per-sheet 이상(핵심 시트 숫자 셀 무회귀).")
    print("→ 클러스터링을 안전하게 활성화할 수 있습니다: EXTRACTION_SHEET_CLUSTERING=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
