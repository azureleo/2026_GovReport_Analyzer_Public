"""
HWP/HWPX 파일에서 텍스트와 표를 추출하는 유틸리티

kordoc (Node.js 기반) 라이브러리를 subprocess로 호출하여
HWP 5.x (OLE2/CFB) 및 HWPX (ZIP+XML) 형식을 지원합니다.

사전 요구사항:
    - Node.js 18+ 설치
    - npm install -g kordoc  또는  npx kordoc 사용 가능
"""

import json
import logging
import re
import shutil
import subprocess
import os
from pathlib import Path

from utils.pdf_reader import PageContent, PDFContent

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """현재 프로젝트 루트 경로."""
    return Path(__file__).resolve().parents[1]


def _which_cmd(name: str) -> str | None:
    """
    Windows에서 subprocess가 바로 실행할 수 있는 명령을 우선 탐색.

    shutil.which("npx")가 npx.ps1을 반환하는 경우가 있는데, shell=False 환경의
    subprocess는 PowerShell 스크립트를 직접 실행하지 못하므로 .cmd를 우선 사용합니다.
    """
    candidates = [name]
    if os.name == "nt":
        candidates = [f"{name}.cmd", f"{name}.exe", f"{name}.bat", name]

    for candidate in candidates:
        found = shutil.which(candidate)
        if found and not found.lower().endswith(".ps1"):
            return found
    return None


def _resolve_kordoc_command() -> list[str]:
    """
    kordoc 실행 명령을 결정.

    우선순위:
      1. 프로젝트 로컬 node_modules/.bin/kordoc.cmd
      2. 전역 kordoc 명령
      3. npx -y kordoc
    """
    bin_dir = _project_root() / "node_modules" / ".bin"
    local_candidates = ["kordoc.cmd", "kordoc.exe", "kordoc.bat", "kordoc"]
    for candidate in local_candidates:
        local = bin_dir / candidate
        if local.exists():
            return [str(local)]

    global_kordoc = _which_cmd("kordoc")
    if global_kordoc:
        return [global_kordoc]

    npx = _which_cmd("npx")
    if npx:
        return [npx, "-y", "kordoc"]

    raise RuntimeError(
        "kordoc 실행 파일을 찾을 수 없습니다.\n"
        "  방법 1: 프로젝트 폴더에서 npm install\n"
        "  방법 2: npm install -g kordoc\n"
        "  방법 3: Node.js 설치 후 터미널을 새로 열기"
    )


def _run_kordoc_json(file_path: Path) -> dict:
    """
    kordoc를 JSON 모드로 실행하여 구조화된 출력을 반환.

    Returns:
        dict: kordoc JSON 출력 (blocks, metadata, markdown 등)
    """
    cmd = _resolve_kordoc_command() + [str(file_path), "--format", "json"]
    logger.info(f"kordoc 실행: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("kordoc 실행 시간 초과 (300초)")
    except FileNotFoundError:
        raise RuntimeError(
            "kordoc를 실행할 수 없습니다. Node.js와 kordoc 설치 상태를 확인해주세요."
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"kordoc 실행 실패 (코드 {result.returncode}): {stderr}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("kordoc 출력이 비어 있습니다.")

    return json.loads(output)


def _run_kordoc_markdown(file_path: Path) -> str:
    """
    kordoc를 Markdown 모드로 실행하여 텍스트 출력을 반환.
    JSON 파싱 실패 시 fallback으로 사용.
    """
    cmd = _resolve_kordoc_command() + [str(file_path)]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
    )

    if result.returncode != 0:
        raise RuntimeError(f"kordoc markdown 실행 실패: {result.stderr.strip()}")

    return result.stdout.strip()


def _markdown_table_to_html(md_table: str) -> str:
    """Markdown 표를 간단한 HTML 표로 변환"""
    lines = [l.strip() for l in md_table.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return ""

    html_rows = []
    for i, line in enumerate(lines):
        # 구분자 행 (---|---) 건너뛰기
        if re.match(r"^[\s|:-]+$", line):
            continue

        cells = [c.strip() for c in line.split("|")]
        # 양 끝의 빈 셀 제거 (|로 시작/끝하는 경우)
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        tag = "th" if i == 0 else "td"
        row_html = "".join(f"<{tag}>{c}</{tag}>" for c in cells)
        html_rows.append(f"<tr>{row_html}</tr>")

    if not html_rows:
        return ""
    return f"<table border='1'>{' '.join(html_rows)}</table>"


def _split_markdown_into_pages(markdown: str) -> list[dict]:
    """
    Markdown 텍스트를 논리적 페이지로 분할.
    HWP는 PDF와 달리 물리적 페이지 구분이 없으므로,
    제목(#) 기준 또는 일정 길이로 분할합니다.
    """
    # 페이지 구분자가 있는 경우 (kordoc이 --- 또는 페이지 마커를 넣는 경우)
    page_markers = re.split(r"\n---\n|\n\*\*\*\n|\n___\n", markdown)

    if len(page_markers) > 1:
        return [{"text": p.strip(), "page_num": i + 1}
                for i, p in enumerate(page_markers) if p.strip()]

    # 1단계 제목(#) 기준으로 분할
    sections = re.split(r"\n(?=# )", markdown)
    if len(sections) > 1:
        return [{"text": s.strip(), "page_num": i + 1}
                for i, s in enumerate(sections) if s.strip()]

    # 분할 기준이 없으면 약 3000자 단위로 분할
    chunk_size = 3000
    chunks = []
    for i in range(0, len(markdown), chunk_size):
        chunk = markdown[i:i + chunk_size].strip()
        if chunk:
            chunks.append({"text": chunk, "page_num": len(chunks) + 1})

    return chunks if chunks else [{"text": markdown, "page_num": 1}]


def _extract_tables_from_text(text: str) -> list[str]:
    """텍스트에서 Markdown 표를 찾아 HTML로 변환"""
    tables = []
    # Markdown 표 패턴: | ... | 형식의 연속된 행
    table_pattern = re.compile(
        r"((?:^\|.+\|$\n?){2,})",
        re.MULTILINE,
    )

    for match in table_pattern.finditer(text):
        html = _markdown_table_to_html(match.group(1))
        if html:
            tables.append(html)

    return tables


def _table_block_to_markdown(table: dict) -> str:
    """kordoc table 블록을 Markdown 표 문자열로 변환."""
    rows = table.get("cells") or []
    if not rows:
        return ""

    md_rows: list[str] = []
    for row in rows:
        cells = []
        for cell in row:
            if isinstance(cell, dict):
                text = cell.get("text", "")
            else:
                text = str(cell)
            cells.append(re.sub(r"\s+", " ", text).strip())
        md_rows.append("| " + " | ".join(cells) + " |")

    if len(md_rows) >= 1:
        col_count = md_rows[0].count("|") - 1
        separator = "| " + " | ".join(["---"] * col_count) + " |"
        md_rows.insert(1, separator)

    return "\n".join(md_rows)


def _block_to_text(block: dict) -> str:
    """kordoc JSON block에서 텍스트를 안정적으로 추출."""
    if not isinstance(block, dict):
        return ""

    if block.get("content"):
        return str(block.get("content", "")).strip()
    if block.get("text"):
        return str(block.get("text", "")).strip()

    table = block.get("table")
    if isinstance(table, dict):
        return _table_block_to_markdown(table)

    return ""


def _blocks_to_pages(data: dict) -> list[dict]:
    """
    kordoc JSON 출력의 blocks를 페이지 단위로 그룹핑.
    blocks에 pageIndex가 있으면 사용, 없으면 순차 분할.
    """
    blocks = data.get("blocks", [])
    markdown = data.get("markdown", "")
    if not blocks:
        # blocks가 없으면 markdown fallback
        return _split_markdown_into_pages(markdown)

    # pageIndex/pageNumber 기준 그룹핑 시도
    pages_map: dict[int, list] = {}
    has_page_index = any(
        b.get("pageIndex") is not None or b.get("pageNumber") is not None
        for b in blocks
    )

    if has_page_index:
        for block in blocks:
            if block.get("pageNumber") is not None:
                pi = int(block.get("pageNumber", 1)) - 1
            else:
                pi = int(block.get("pageIndex", 0))
            pages_map.setdefault(pi, []).append(block)
    else:
        # pageIndex 없으면 전체를 하나의 페이지로
        pages_map[0] = blocks

    result = []
    for pi in sorted(pages_map.keys()):
        page_blocks = pages_map[pi]
        text_parts = []
        tables = []

        for b in page_blocks:
            btype = b.get("type", "")
            content = _block_to_text(b)

            if btype == "table":
                # 표 블록
                html = _markdown_table_to_html(content) if content else ""
                if html:
                    tables.append(html)
                text_parts.append(content)
            else:
                text_parts.append(content)

        result.append({
            "text": "\n".join(text_parts),
            "tables": tables,
            "page_num": pi + 1,
        })

    # kordoc JSON의 blocks 구조가 버전에 따라 비어 있거나 불완전할 수 있다.
    # markdown 전문이 훨씬 풍부하면 markdown 기반 분할을 우선 사용한다.
    block_text_len = sum(len(p.get("text", "")) for p in result)
    if markdown and len(markdown) > max(block_text_len * 2, 3000):
        return _split_markdown_into_pages(markdown)

    return result


def extract_hwp(file_path: str | Path) -> PDFContent:
    """
    HWP/HWPX 파일을 파싱해 PDFContent 구조로 반환.

    PDF와 동일한 PageContent/PDFContent 인터페이스를 사용하여
    기존 파이프라인과 호환됩니다.

    Args:
        file_path: HWP 또는 HWPX 파일 경로

    Returns:
        PDFContent: 추출된 콘텐츠 (PDF와 동일한 구조)
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in (".hwp", ".hwpx"):
        raise ValueError(f"지원하지 않는 파일 형식: {suffix} (.hwp 또는 .hwpx만 지원)")

    print(f"[HWP Reader] kordoc로 {suffix.upper()} 파일 파싱 중...")

    # 1단계: JSON 모드 시도
    page_data_list = []
    try:
        data = _run_kordoc_json(file_path)
        page_data_list = _blocks_to_pages(data)
        print(f"[HWP Reader] JSON 모드 파싱 성공: {len(page_data_list)}개 섹션")
    except Exception as e:
        logger.warning(f"kordoc JSON 모드 실패, Markdown 모드로 재시도: {e}")

        # 2단계: Markdown fallback
        try:
            md = _run_kordoc_markdown(file_path)
            page_data_list = _split_markdown_into_pages(md)
            print(f"[HWP Reader] Markdown 모드 파싱 성공: {len(page_data_list)}개 섹션")
        except Exception as e2:
            raise RuntimeError(
                f"HWP 파일 파싱 실패: {e2}\n"
                f"kordoc가 설치되어 있는지 확인해주세요: npm install -g kordoc"
            )

    # PageContent 리스트 생성
    pages: list[PageContent] = []
    all_texts: list[str] = []

    for pd_item in page_data_list:
        text = pd_item.get("text", "")
        page_num = pd_item.get("page_num", len(pages) + 1)

        # 표가 이미 추출되어 있으면 사용, 아니면 텍스트에서 추출
        tables = pd_item.get("tables", [])
        if not tables:
            tables = _extract_tables_from_text(text)

        all_texts.append(f"[페이지 {page_num}]\n{text}")

        pages.append(PageContent(
            page_number=page_num,
            text=text,
            tables=tables,
            images=[],  # HWP에서는 이미지 추출 미지원 (텍스트 기반 분석)
        ))

    if not pages:
        raise RuntimeError("HWP 파일에서 콘텐츠를 추출하지 못했습니다.")

    full_text = "\n\n".join(all_texts)

    print(f"[HWP Reader] 파싱 완료: {len(pages)}개 페이지/섹션, "
          f"총 {len(full_text):,}자")

    return PDFContent(
        total_pages=len(pages),
        pages=pages,
        full_text=full_text,
    )


def is_hwp_file(file_path: str | Path) -> bool:
    """파일이 HWP/HWPX 형식인지 확인"""
    suffix = Path(file_path).suffix.lower()
    return suffix in (".hwp", ".hwpx")
