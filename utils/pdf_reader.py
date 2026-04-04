"""
PDF 파일에서 텍스트, 표, 이미지를 추출하는 유틸리티

PyMuPDF(fitz) 기반으로 구현되었으며,
텍스트 블록, 표(HTML 형태), 이미지(base64 PNG) 를 페이지 단위로 반환합니다.
"""

import base64
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)   # MuPDF C라이브러리 노이즈 억제 (파싱에 영향 없음)
from PIL import Image

import config


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


def _extract_tables_from_page(page: fitz.Page) -> list[str]:
    """
    PyMuPDF의 find_tables()를 사용해 페이지 내 표를 HTML 문자열 리스트로 반환.
    표가 없으면 빈 리스트 반환.
    """
    tables = []
    try:
        tab_finder = page.find_tables()
        for tab in tab_finder.tables:
            df = tab.to_pandas()
            html = df.to_html(index=False, border=1, na_rep="")
            tables.append(html)
    except Exception:
        pass
    return tables


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


def _has_graph_keywords(text: str) -> bool:
    """그래프/차트가 있을 가능성이 높은 페이지인지 키워드로 판별"""
    keywords = [
        "그림", "그래프", "차트", "Figure", "Fig.", "Chart",
        "배출량 추이", "온실가스 배출", "감축 목표", "배출 현황",
        "에너지 소비", "탄소중립", "NDC",
    ]
    for kw in keywords:
        if kw in text:
            return True
    return False


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

        # 그래프가 있을 법한 페이지는 전체 렌더링 추가
        if render_graph_pages and _has_graph_keywords(text):
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
