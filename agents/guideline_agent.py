"""
에이전트 1: 가이드라인 에이전트

carbon_guideline.md를 우선 사용해 시트별 보조 지침을 반환한다. markdown이 없고
--guideline HWP/HWPX 파일이 제공된 경우에는 키워드 스니펫 fallback을 사용한다.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config
from agents.guideline_parser import (
    ParsedGuideline,
    build_snippet_prompts,
    build_structured_prompts,
    empty_parsed_guideline,
    extract_guideline_context,
    parse_markdown_guideline,
    sheet_keys,
)
from utils.hwp_reader import extract_hwp, is_hwp_file

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MARKDOWN_GUIDELINE_PATH = _PROJECT_ROOT / "carbon_guideline.md"

logger = logging.getLogger(__name__)


class GuidelineAgent:
    """가이드라인을 시트별 추출 보조 프롬프트로 변환한다."""

    def __init__(self, hwp_path: str | None = None):
        self.hwp_path = hwp_path
        self.guideline_text = ""
        self.guideline_context: dict[str, list[str]] = {}
        self.parsed_guideline: ParsedGuideline = empty_parsed_guideline()
        self.guideline_loaded = False
        self.guideline_error = ""
        self.guideline_source = ""
        self.guideline_mode = "none"
        self.prompt_char_counts: dict[str, int] = {}

        if _MARKDOWN_GUIDELINE_PATH.exists():
            self._load_markdown_guideline()
        elif hwp_path:
            self._load_hwp_guideline(hwp_path)

    def _load_markdown_guideline(self) -> None:
        """사전 구조화된 carbon_guideline.md를 로드하고 앵커 섹션을 파싱한다."""
        try:
            text = _MARKDOWN_GUIDELINE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            self.guideline_error = f"carbon_guideline.md 로드 실패: {exc}"
            logger.warning(self.guideline_error)
            return
        self.guideline_text = text
        self.parsed_guideline = parse_markdown_guideline(text)
        self.guideline_context = extract_guideline_context(text)
        self.guideline_loaded = bool(text.strip())
        self.guideline_source = _MARKDOWN_GUIDELINE_PATH.name
        self.guideline_mode = "markdown"

    def _load_hwp_guideline(self, hwp_path: str) -> None:
        """HWP/HWPX fallback은 앵커가 없으므로 키워드 스니펫만 만든다."""
        path = Path(hwp_path)
        if not path.exists():
            self.guideline_error = f"파일 없음: {path}"
            logger.warning(self.guideline_error)
            return
        if not is_hwp_file(path):
            self.guideline_error = f"지원하지 않는 가이드라인 형식: {path.suffix}"
            logger.warning(self.guideline_error)
            return
        try:
            content = extract_hwp(path)
        except (OSError, RuntimeError) as exc:
            self.guideline_error = str(exc)
            logger.warning("가이드라인 HWP 파싱 실패: %s", exc)
            return
        self.guideline_text = content.full_text
        self.guideline_context = extract_guideline_context(content.full_text)
        self.guideline_loaded = bool(self.guideline_text.strip())
        self.guideline_source = path.name
        self.guideline_mode = "hwp_snippet"

    def get_schema(self) -> dict[str, dict[str, str] | str]:
        """호환용 스키마 요약 반환. 실제 추출 스키마는 ExtractorAgent가 소유한다."""
        return {
            "description": "carbon_guideline.md 기반 16개 시트 보조 지침",
            "sheets": {sheet_key: config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key) for sheet_key in sheet_keys()},
        }

    def get_all_prompts(self) -> dict[str, str]:
        """모든 16개 시트의 추출 보조 프롬프트를 시트키 기준으로 반환한다."""
        if self.guideline_mode == "markdown" and getattr(config, "GUIDELINE_STRUCTURED_INJECTION", True):
            prompts = build_structured_prompts(
                self.parsed_guideline,
                max(0, getattr(config, "GUIDELINE_PROMPT_MAX_CHARS", 3000)),
            )
        else:
            prompts = build_snippet_prompts(self.guideline_context)
        self.prompt_char_counts = {sheet_key: len(prompt) for sheet_key, prompt in prompts.items()}
        return prompts

    def report(self) -> str:
        """에이전트 상태 보고."""
        if not self.prompt_char_counts:
            self.get_all_prompts()
        sheets = [config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key) for sheet_key in sheet_keys()]
        return (
            "[에이전트1 가이드라인] 로드 완료\n"
            f"  - 추출 대상 시트: {len(sheets)}개\n"
            f"  - 시트 목록: {', '.join(sheets)}\n"
            f"  - 가이드라인 출처: {self._guideline_status()}\n"
            f"  - 시트별 주입 문자 수: {self._prompt_char_summary()}"
        )

    def _guideline_status(self) -> str:
        if self.guideline_loaded:
            if self.guideline_mode == "markdown" and getattr(config, "GUIDELINE_STRUCTURED_INJECTION", True):
                section_count = sum(len(sections) for sections in self.parsed_guideline.sections_by_sheet.values())
                return f"로드 완료 ({self.guideline_source}, 구조 주입 {section_count}개 섹션)"
            snippet_count = sum(len(snippets) for snippets in self.guideline_context.values())
            return f"로드 완료 ({self.guideline_source}, 스니펫 fallback {snippet_count}개)"
        if self.guideline_error:
            return f"로드 실패 ({self.guideline_error})"
        return "없음 (내장 추출 스키마만 사용)"

    def _prompt_char_summary(self) -> str:
        if not self.prompt_char_counts:
            return "없음"
        return ", ".join(f"{sheet_key}={self.prompt_char_counts.get(sheet_key, 0)}" for sheet_key in sheet_keys())
