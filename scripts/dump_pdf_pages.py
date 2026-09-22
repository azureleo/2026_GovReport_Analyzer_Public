#!/usr/bin/env python
"""지정한 물리 페이지의 텍스트를 그대로 출력한다 (표본 대사·원문 대조용).

페이지 번호는 PDF 뷰어의 **물리 순번**(1-based)이며 인쇄 페이지 번호가 아니다.
M3 L4 표본 대사에서 판정지의 출처페이지를 원문과 맞춰볼 때 쓴다.

사용법:
    python scripts/dump_pdf_pages.py "문서.pdf" 147 144 104-110
    python scripts/dump_pdf_pages.py "문서.pdf" 147 --out 발췌.md

인자로 준 순서대로 출력하며, `a-b`는 닫힌 구간이다.
텍스트가 비어 있으면 이미지 개수를 함께 표시한다 — 백지 페이지와
'표가 이미지로만 들어간 페이지'를 구별하기 위해서다(강원 p262 사례).
"""

import argparse
import sys

import fitz


def parse_pages(specs):
    """['147', '104-107'] → [147, 104, 105, 106, 107] (입력 순서 유지, 중복 제거)."""
    pages = []
    for spec in specs:
        if "-" in spec.strip("-"):
            start, end = spec.split("-", 1)
            rng = range(int(start), int(end) + 1)
        else:
            rng = [int(spec)]
        for page in rng:
            if page not in pages:
                pages.append(page)
    return pages


def main():
    parser = argparse.ArgumentParser(description="PDF 물리 페이지 텍스트 덤프")
    parser.add_argument("pdf", help="원문 PDF 경로")
    parser.add_argument("pages", nargs="+", help="물리 페이지 번호 (예: 147 104-110)")
    parser.add_argument("--out", help="출력 파일 (생략하면 표준출력)")
    args = parser.parse_args()

    doc = fitz.open(args.pdf)
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        for page_no in parse_pages(args.pages):
            if not 1 <= page_no <= doc.page_count:
                print(f"\n===== p{page_no}: 범위 밖 (총 {doc.page_count}쪽) =====", file=out)
                continue
            page = doc[page_no - 1]
            text = page.get_text()
            print(f"\n===== p{page_no} (물리 순번) =====", file=out)
            if text.strip():
                print(text, file=out)
            else:
                n_img = len(page.get_images())
                kind = f"텍스트 없음 · 이미지 {n_img}개" if n_img else "백지(텍스트·이미지 모두 없음)"
                print(f"[{kind}]", file=out)
    finally:
        if args.out:
            out.close()
        doc.close()


if __name__ == "__main__":
    main()
