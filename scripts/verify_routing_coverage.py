"""
라우팅 커버리지 검증기 (LLM 토큰 0).

라우팅 변경의 유일한 품질 리스크는 "데이터가 실제로 있는 페이지를 라우팅이 누락하는가?"
뿐이다. 이 스크립트는 두 가지 무료 검사로 그것을 증명한다.

  Check A (정답 리콜 비회귀):
    정답지(서울..._정리.xlsx)의 시트별 고유 텍스트 앵커를 원문 PDF 페이지에 매핑한 뒤,
    라우팅이 그 페이지를 후보로 포함하는 비율을 시트별로 측정. 후보 라우팅의 커버리지가
    베이스라인보다 떨어지면 회귀(FAIL).

  Check B (원문 완전성):
    정답지는 의도적으로 불완전하다(예: 09시트 ~250개 관리카드 누락). 그래서 정답지로는
    못 보는 영역을 보강한다. 후보 라우팅이 떨구지만 베이스라인은 포함한 페이지 중,
    '표가 있고 해당 시트 strong 키워드가 있는' = 데이터 보유 가능성이 높은 페이지 수를
    시트별로 보고한다(수동 점검용).

베이스라인 = config.ROUTE_DROP_UBIQUITOUS_WEAK=False, 후보 = True 로 같은 PDF에서 비교.
LLM 호출은 전혀 없다.

사용:
  python scripts/verify_routing_coverage.py <golden.xlsx> <source.pdf>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from utils.pdf_reader import extract_pdf  # noqa: E402
from agents import extractor_agent as ea  # noqa: E402

_NAME_TO_KEY = {
    name: key
    for key, name in config.SHEET_KEY_TO_NAME.items()
    if key in config.EXTRACTION_SHEETS
}

_GENERIC = {
    "관리번호", "담당부서", "세부사업명", "사업명", "내용", "구분", "번호", "단위", "비고",
    "부문", "합계", "현황", "목표", "전망", "지자체명", "서울특별시", "서울시",
    "탄소중립", "녹색성장", "기본계획", "직접배출", "간접배출", "온실가스",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _harvest_anchors(ws) -> list[str]:
    anchors: list[str] = []
    seen: set[str] = set()
    for row in ws.iter_rows(values_only=True):
        for cell in row:
            if not isinstance(cell, str):
                continue
            t = cell.strip()
            if not t or t.startswith("▣") or re.match(r"^\d+\.", t):
                continue
            tokens = re.findall(r"[가-힣A-Za-z0-9]{3,}", t)
            if not [tok for tok in tokens if tok not in _GENERIC]:
                continue
            key = _norm(t)
            if len(key) < 6 or key in seen:
                continue
            seen.add(key)
            anchors.append(t)
    return anchors


def _routed_nums(pages) -> dict[str, set[int]]:
    routed = ea._route_pages_by_sheet(pages)
    return {k: {p.page_number for p in v} for k, v in routed.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("golden")
    ap.add_argument("pdf")
    args = ap.parse_args()

    print(f"[1/4] PDF 파싱: {args.pdf}")
    pc = extract_pdf(args.pdf, render_graph_pages=False)
    pages = pc.pages
    by_num = {p.page_number: p for p in pages}
    page_norm = {
        p.page_number: _norm((p.text or "") + "\n" + "\n".join(p.tables or []))
        for p in pages
    }

    print("[2/4] 라우팅 2종 계산 (베이스라인=모든 샤프닝 off / 후보=weak+strong on, LLM 없음)")
    saved_weak = getattr(config, "ROUTE_DROP_UBIQUITOUS_WEAK", True)
    saved_strong = getattr(config, "ROUTE_DROP_UBIQUITOUS_STRONG", False)
    # 베이스라인: 샤프닝을 모두 끈 가장 넓은 라우팅(최대 리콜).
    config.ROUTE_DROP_UBIQUITOUS_WEAK = False
    config.ROUTE_DROP_UBIQUITOUS_STRONG = False
    base = _routed_nums(pages)
    # 후보: weak + strong 샤프닝을 모두 켠 좁은 라우팅. 이 커버리지가 베이스라인
    # 이상이어야(=리콜 무회귀) 토큰 절감 변경을 안전하게 적용할 수 있다.
    config.ROUTE_DROP_UBIQUITOUS_WEAK = True
    config.ROUTE_DROP_UBIQUITOUS_STRONG = True
    cand = _routed_nums(pages)
    config.ROUTE_DROP_UBIQUITOUS_WEAK = saved_weak
    config.ROUTE_DROP_UBIQUITOUS_STRONG = saved_strong

    print("[3/4] 정답지 앵커 → 페이지 매핑")
    wb = openpyxl.load_workbook(args.golden, read_only=True)
    located_by_sheet: dict[str, list[list[int]]] = {}
    for name, key in _NAME_TO_KEY.items():
        if name not in wb.sheetnames:
            continue
        loc = []
        for anchor in _harvest_anchors(wb[name]):
            a = _norm(anchor)
            pages_hit = [pno for pno, ptext in page_norm.items() if a in ptext]
            if pages_hit:
                loc.append(pages_hit)
        located_by_sheet[key] = loc

    print("[4/4] 비교 결과\n")
    hdr = f"{'시트키':24s} {'탐지':>4s} {'기준%':>6s} {'후보%':>6s} {'기준p':>5s} {'후보p':>5s} {'드롭':>4s} {'위험':>4s}"
    print(hdr)
    print("-" * len(hdr))

    fails: list[str] = []
    tot_pages_base = tot_pages_cand = 0
    tot_risky = 0
    for key in _NAME_TO_KEY.values():
        if key not in located_by_sheet:
            continue
        loc = located_by_sheet[key]
        bset, cset = base.get(key, set()), cand.get(key, set())

        def cov(rset):
            if not loc:
                return 100.0
            ok = sum(1 for pages_hit in loc if rset & set(pages_hit))
            return ok / len(loc) * 100

        bcov, ccov = cov(bset), cov(cset)
        dropped = bset - cset
        # Check B: 떨군 페이지 중 표 + strong 키워드를 가진 데이터 보유 가능 페이지
        strong = ea._ROUTE_CONFIGS.get(key, {}).get("strong", [])
        risky = 0
        for pno in dropped:
            pg = by_num.get(pno)
            if pg is None:
                continue
            body = (pg.text or "") + "\n" + "\n".join(pg.tables or [])
            if pg.tables and any(kw in body for kw in strong):
                risky += 1

        tot_pages_base += len(bset)
        tot_pages_cand += len(cset)
        tot_risky += risky
        if ccov + 1e-6 < bcov:
            fails.append(f"{key}: 커버리지 회귀 {bcov:.1f}%→{ccov:.1f}%")
        print(f"{key:24s} {len(loc):4d} {bcov:6.1f} {ccov:6.1f} {len(bset):5d} {len(cset):5d} "
              f"{len(dropped):4d} {risky:4d}")

    print("-" * len(hdr))
    print(f"{'TOTAL':24s} {'':4s} {'':6s} {'':6s} {tot_pages_base:5d} {tot_pages_cand:5d} "
          f"{'':4s} {tot_risky:4d}")
    reduction = (1 - tot_pages_cand / tot_pages_base) * 100 if tot_pages_base else 0
    print(f"\n라우팅 페이지-인스턴스 합: {tot_pages_base} → {tot_pages_cand}  ({reduction:.1f}% 감소)")
    print(f"Check B 위험 드롭(표+strong 보유) 총 {tot_risky}건")
    if fails:
        print("\n❌ FAIL — 시트별 정답 리콜 회귀:")
        for f in fails:
            print("  -", f)
        return 1
    print("\n✅ PASS — 모든 시트에서 정답 리콜 비회귀 (Check B는 위 수치로 수동 확인)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
