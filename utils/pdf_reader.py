"""
PDF 파일에서 텍스트, 표, 이미지를 추출하는 유틸리티

PyMuPDF(fitz) 기반으로 구현되었으며,
텍스트 블록, 표(HTML 형태), 이미지(base64 PNG) 를 페이지 단위로 반환합니다.
"""

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)   # MuPDF C라이브러리 노이즈 억제 (파싱에 영향 없음)
from PIL import Image

import config

logger = logging.getLogger(__name__)

_TABLE_MARKER_RE = re.compile(r"(?m)(?:^|\[|\()\s*표\s*\d")


@dataclass
class PageContent:
    """한 페이지에서 추출된 콘텐츠"""
    page_number: int
    text: str
    tables: list[str]          # HTML 문자열 리스트
    images: list[dict]         # {"base64": str, "width": int, "height": int, "caption": str}


@dataclass
class PDFContent:
    """PDF 전체에서 추출된 콘텐츠"""
    total_pages: int
    pages: list[PageContent]
    full_text: str             # 전체 텍스트 (페이지 구분자 포함)


def _resize_image(pil_img: Image.Image, max_size: int = config.MAX_IMAGE_SIZE) -> Image.Image:
    """이미지를 최대 크기 이하로 비율을 유지하며 리사이즈"""
    w, h = pil_img.size
    if max(w, h) <= max_size:
        return pil_img
    ratio = max_size / max(w, h)
    return pil_img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)


def _image_to_base64(pil_img: Image.Image) -> str:
    """PIL 이미지를 base64 PNG 문자열로 변환"""
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _tables_with_strategy(page: fitz.Page, strategy: str) -> tuple[list[str], int]:
    """주어진 전략으로 표를 찾아 (HTML 리스트, 총 셀 수)를 반환."""
    htmls: list[str] = []
    cells = 0
    try:
        tab_finder = page.find_tables(strategy=strategy)
    except Exception:
        return [], 0
    for tab in tab_finder.tables:
        try:
            df = tab.to_pandas()
        except Exception:
            continue
        if df.size == 0:
            continue
        cells += int(df.shape[0]) * int(df.shape[1])
        htmls.append(df.to_html(index=False, border=1, na_rep=""))
    return htmls, cells


def _extract_tables_from_page(page: fitz.Page) -> list[str]:
    """
    페이지 내 표를 HTML 문자열 리스트로 반환.

    한국 정부 보고서는 괘선 일부 생략·셀 병합이 많아 기본 전략만으로는 표를 놓치거나
    깨뜨린다. 괘선 기반(lines_strict→lines)을 먼저 시도하고, 둘 다 못 찾으면 텍스트
    정렬 기반(text)으로 폴백한다. (text를 max-cells로 경쟁시키면 무괘선 표를 과분할해
    오히려 이기는 경우가 있어, 괘선 우선·텍스트 폴백 순서를 쓴다.)

    '표 N' 마커가 있는데 표를 0개 찾으면 조용한 유실 대신 경고를 남긴다.
    """
    for strategy in ("lines_strict", "lines"):
        htmls, _ = _tables_with_strategy(page, strategy)
        if htmls:
            return htmls

    htmls, _ = _tables_with_strategy(page, "text")
    if htmls:
        return htmls

    try:
        if _TABLE_MARKER_RE.search(page.get_text("text")):
            logger.warning("페이지 %s: '표 N' 마커는 있으나 표 파싱 결과 0개", page.number + 1)
    except Exception:
        pass
    return []


def _extract_images_from_page(
    page: fitz.Page,
    doc: fitz.Document,
    min_width: int = 100,
    min_height: int = 100,
) -> list[dict]:
    """
    페이지에 삽입된 이미지를 추출해 base64로 인코딩하여 반환.
    너무 작은 이미지(로고, 아이콘 등)는 제외.
    """
    result = []
    image_list = page.get_images(full=True)
    seen_xrefs = set()

    for img_info in image_list:
        xref = img_info[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)

        try:
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

            w, h = pil_img.size
            if w < min_width or h < min_height:
                continue

            pil_img = _resize_image(pil_img)
            b64 = _image_to_base64(pil_img)
            result.append({
                "base64": b64,
                "width": pil_img.width,
                "height": pil_img.height,
                "caption": "",
            })
        except Exception:
            continue

    return result


def _render_page_as_image(page: fitz.Page) -> dict:
    """
    페이지 전체를 이미지로 렌더링 (그래프·도표가 포함된 경우 사용).
    """
    mat = fitz.Matrix(config.IMAGE_DPI / 72, config.IMAGE_DPI / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    pil_img = _resize_image(pil_img)
    return {
        "base64": _image_to_base64(pil_img),
        "width": pil_img.width,
        "height": pil_img.height,
        "caption": f"Page {page.number + 1} full render",
    }


_VISUAL_RENDER_PHRASES = [
    "그래프", "차트", "도표", "배출량 추이", "온실가스 배출",
    "감축 목표", "배출 현황", "에너지 소비",
]
_VISUAL_RENDER_PATTERNS = [
    re.compile(r"(?m)^\s*\[?\s*그림\s*\d"),
    re.compile(r"\b(?:Figure|Fig\.|Chart)\b", re.I),
]


def _has_graph_keywords(text: str) -> bool:
    """그래프/차트/도표가 있을 가능성이 높은 페이지인지 판별."""
    if any(phrase in text for phrase in _VISUAL_RENDER_PHRASES):
        return True
    return any(pattern.search(text) for pattern in _VISUAL_RENDER_PATTERNS)


def _is_vector_chart_page(
    page: fitz.Page,
    tables: list[str],
    images: list[dict],
) -> bool:
    """
    벡터로 그려진 차트가 있을 법한 페이지인지 판별(전체 렌더링 대상 승격용).

    벡터 차트는 get_images()에 안 잡혀 누락된다. 다만 한국 정부 보고서의 차트 페이지는
    텍스트가 많아 '텍스트가 적다'는 신호는 쓸 수 없다. 대신 다음을 본다:
    - 파싱된 표가 없음(있으면 데이터는 표로 이미 확보)
    - 임베드 이미지가 없음(있으면 이미지로 이미 확보)
    - 벡터 path가 충분히 많음(축·격자·막대 등 차트 구성요소)
    이 조건은 보수적이라 표/이미지로 데이터가 잡힌 페이지는 과렌더링하지 않는다.
    """
    if not getattr(config, "VECTOR_RENDER_ENABLED", True):
        return False
    if tables or images:
        return False
    try:
        drawings = page.get_drawings()
    except Exception:
        return False
    return len(drawings) >= int(getattr(config, "VECTOR_RENDER_MIN_DRAWINGS", 60))


def extract_pdf(
    pdf_path: str | Path,
    render_graph_pages: bool = True,
) -> PDFContent:
    """
    PDF 전체를 파싱해 PDFContent 를 반환.

    Args:
        pdf_path: PDF 파일 경로
        render_graph_pages: 그래프가 있을 법한 페이지를 전체 렌더링할지 여부
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(str(pdf_path))

    pages: list[PageContent] = []
    all_texts: list[str] = []

    for page_num in range(len(doc)):
        page = doc[page_num]

        # 텍스트 추출
        text = page.get_text("text", sort=True)
        all_texts.append(f"[페이지 {page_num + 1}]\n{text}")

        # 표 추출
        tables = _extract_tables_from_page(page)

        # 이미지 추출 (embedded images)
        images = _extract_images_from_page(page, doc)

        # 그래프가 있을 법한 페이지는 전체 렌더링 추가.
        # (1) 텍스트에 그래프/차트 키워드가 있거나, (2) 표·임베드이미지가 없는데 벡터 path가
        # 많아 벡터 차트로 추정되는 페이지(키워드가 없어 누락되던 케이스)를 잡는다.
        if render_graph_pages and (
            _has_graph_keywords(text) or _is_vector_chart_page(page, tables, images)
        ):
            rendered = _render_page_as_image(page)
            # 이미 embedded image가 없거나, rendered 페이지가 더 풍부한 경우 추가
            images.append(rendered)

        pages.append(PageContent(
            page_number=page_num + 1,
            text=text,
            tables=tables,
            images=images,
        ))

    doc.close()

    return PDFContent(
        total_pages=len(pages),
        pages=pages,
        full_text="\n\n".join(all_texts),
    )


def get_batches(
    pdf_content: PDFContent,
    batch_size: int = config.BATCH_SIZE,
) -> list[list[PageContent]]:
    """페이지 리스트를 batch_size 단위로 나눠서 반환"""
    pages = pdf_content.pages
    return [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
