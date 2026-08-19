"""carbon_guideline.md 구조 주입과 HWP 스니펫 fallback 파서."""

from __future__ import annotations

import re
from dataclasses import dataclass

import config


GUIDELINE_CONTEXT_RULES_V2: dict[str, list[str]] = {
    "document_meta": ["기본계획", "계획기간", "기준연도", "목표연도", "수립", "법적 근거"],
    "plan_overview": ["추진체계", "추진절차", "경과", "공청회", "자문", "위원회"],
    "regional_conditions": ["인구", "면적", "GRDP", "에너지", "차량", "전력", "건축물", "지역 여건"],
    "emissions_regional": ["온실가스", "배출량", "직접배출", "간접배출", "GIR", "인벤토리", "LULUCF"],
    "emissions_management": ["관리권한", "건물", "수송", "농축산", "폐기물", "흡수원"],
    "emissions_forecast": ["전망", "BAU", "시계열", "LEAP", "증가율"],
    "reduction_targets": ["감축목표", "감축률", "목표배출량", "NDC", "2030", "2018년 대비"],
    "vision_strategy": ["비전", "전략", "추진방향", "핵심", "슬로건"],
    "mitigation_projects": ["감축사업", "세부사업", "핵심과제", "관리번호", "성과지표", "주관부서"],
    "annual_implementation": ["연차별", "이행계획", "단계별", "목표물량"],
    "quantitative_reductions": ["감축량", "감축원단위", "모니터링", "활동량", "배출계수", "정량사업"],
    "financial_plan": ["재정", "투자", "예산", "국비", "시비", "도비"],
    "foundation_measures": [
        "적응", "공유재산", "국제협력", "교육", "녹색성장", "정의로운 전환",
        "기후감시", "기후 전망", "기후영향", "취약성", "리스크", "위험도",
        "SSP", "RCP", "재난방지",
    ],
    "governance_feedback": ["이행관리", "환류", "점검체계", "탄소중립이행책임관"],
    "monitoring_performance": ["추진상황", "점검", "달성여부", "이행실적"],
    "changes_actions": ["변경과제", "미달성", "조치계획", "변경사유"],
}

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_ANCHOR_RE = re.compile(r"<!--\s*sheets:\s*([^>]+?)\s*-->")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_RULE_LINE_RE = re.compile(r"^\d+\.\s+")


@dataclass(frozen=True, slots=True)
class GuidelineSection:
    """markdown 헤딩 하나에서 시작하는 시트별 주입 블록."""

    title: str
    level: int
    text: str
    priority: int


@dataclass(frozen=True, slots=True)
class HeadingMark:
    """markdown 헤딩 위치."""

    line_index: int
    level: int
    title: str


@dataclass(frozen=True, slots=True)
class ParsedGuideline:
    """구조화 markdown 파싱 결과."""

    sections_by_sheet: dict[str, list[GuidelineSection]]
    mapping_rules_by_sheet: dict[str, list[str]]


def sheet_keys() -> list[str]:
    return list(getattr(config, "EXTRACTION_SHEETS", []))


def empty_parsed_guideline() -> ParsedGuideline:
    return ParsedGuideline(
        sections_by_sheet={sheet_key: [] for sheet_key in sheet_keys()},
        mapping_rules_by_sheet={sheet_key: [] for sheet_key in sheet_keys()},
    )


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _split_guideline_sentences(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw in text.splitlines():
        line = _compact_text(raw)
        if not line:
            continue
        if re.match(r"^(\d+(\.\d+)*|제\s*\d+\s*[장절]|부록|표\s*\d+|그림\s*\d+)", line):
            if current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            chunks.append(line)
            continue
        current.append(line)
        current_len += len(line)
        if current_len >= 550:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
    if current:
        chunks.append(" ".join(current))
    return chunks


def _score_guideline_chunk(chunk: str, keywords: list[str]) -> int:
    score = sum(3 for keyword in keywords if keyword in chunk)
    if any(unit in chunk for unit in ["tCO2", "CO2eq", "천톤", "백만원", "대", "km", "%"]):
        score += 1
    if any(term in chunk for term in ["작성", "산정", "기준", "부문", "양식", "항목", "점검"]):
        score += 1
    return score


def extract_guideline_context(full_text: str, max_chunks: int = 3) -> dict[str, list[str]]:
    """앵커가 없는 HWP 텍스트에서 시트별 관련 스니펫을 추출한다."""
    chunks = _split_guideline_sentences(full_text)
    result: dict[str, list[str]] = {}
    for sheet_key, keywords in GUIDELINE_CONTEXT_RULES_V2.items():
        ranked = sorted(((_score_guideline_chunk(chunk, keywords), chunk) for chunk in chunks), reverse=True)
        selected: list[str] = []
        seen: set[str] = set()
        for score, chunk in ranked:
            if score <= 0:
                break
            compact = _compact_text(chunk)
            if not compact or compact in seen:
                continue
            seen.add(compact)
            selected.append(compact[:500])
            if len(selected) >= max_chunks:
                break
        result[sheet_key] = selected
    return result


def _find_headings(lines: list[str]) -> list[HeadingMark]:
    headings: list[HeadingMark] = []
    for idx, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append(HeadingMark(line_index=idx, level=len(match.group(1)), title=match.group(2).strip()))
    return headings


def _section_end(headings: list[HeadingMark], position: int, line_count: int) -> int:
    current = headings[position]
    for following in headings[position + 1:]:
        if following.level <= current.level:
            return following.line_index
    return line_count


def _leading_anchor_sheets(section_lines: list[str]) -> tuple[str, ...]:
    keys: list[str] = []
    for line in section_lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        match = _ANCHOR_RE.fullmatch(stripped)
        if match is not None:
            keys.extend(key.strip() for key in match.group(1).split(",") if key.strip())
            continue
        break
    return tuple(dict.fromkeys(keys))


def _section_priority(title: str) -> int:
    return 2 if title.startswith("`") else 1


def _section_text_without_anchor(section_lines: list[str]) -> str:
    return "\n".join(line for line in section_lines if _ANCHOR_RE.fullmatch(line.strip()) is None).strip()


def _extract_mapping_rules(markdown_text: str) -> list[str]:
    lines = markdown_text.splitlines()
    headings = _find_headings(lines)
    for position, heading in enumerate(headings):
        if heading.level == 2 and heading.title.startswith("7."):
            start = heading.line_index + 1
            end = _section_end(headings, position, len(lines))
            return [line.strip() for line in lines[start:end] if _RULE_LINE_RE.match(line.strip())]
    return []


def _mapping_rules_by_sheet(rules: list[str]) -> dict[str, list[str]]:
    by_sheet: dict[str, list[str]] = {sheet_key: [] for sheet_key in sheet_keys()}
    for sheet_key, keywords in GUIDELINE_CONTEXT_RULES_V2.items():
        by_sheet[sheet_key] = [rule for rule in rules if _score_guideline_chunk(rule, keywords) > 0]
    return by_sheet


def parse_markdown_guideline(markdown_text: str) -> ParsedGuideline:
    lines = markdown_text.splitlines()
    headings = _find_headings(lines)
    parsed = empty_parsed_guideline()
    for position, heading in enumerate(headings):
        end = _section_end(headings, position, len(lines))
        section_lines = lines[heading.line_index:end]
        anchored_sheets = _leading_anchor_sheets(section_lines)
        if not anchored_sheets:
            continue
        section = GuidelineSection(
            title=heading.title,
            level=heading.level,
            text=_section_text_without_anchor(section_lines),
            priority=_section_priority(heading.title),
        )
        for sheet_key in anchored_sheets:
            if sheet_key in parsed.sections_by_sheet:
                parsed.sections_by_sheet[sheet_key].append(section)
    return ParsedGuideline(parsed.sections_by_sheet, _mapping_rules_by_sheet(_extract_mapping_rules(markdown_text)))


def _has_markdown_table(text: str) -> bool:
    return any(_TABLE_LINE_RE.match(line) for line in text.splitlines())


def _append_block_within_limit(prefix: str, parts: list[str], block: str, max_chars: int) -> None:
    candidate = prefix + "\n\n".join([*parts, block])
    if len(candidate) <= max_chars:
        parts.append(block)


def build_snippet_prompts(guideline_context: dict[str, list[str]]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for sheet_key in sheet_keys():
        snippets = guideline_context.get(sheet_key, [])
        if not snippets:
            prompts[sheet_key] = ""
            continue
        lines = [f"[가이드라인 보조 지침: {sheet_key}]"]
        lines.extend(f"{idx}. {snippet}" for idx, snippet in enumerate(snippets, start=1))
        prompts[sheet_key] = "\n".join(lines)
    return prompts


def build_structured_prompts(parsed: ParsedGuideline, max_chars: int) -> dict[str, str]:
    return {sheet_key: _structured_prompt_for_sheet(parsed, sheet_key, max_chars) for sheet_key in sheet_keys()}


def _structured_prompt_for_sheet(parsed: ParsedGuideline, sheet_key: str, max_chars: int) -> str:
    prefix = f"[가이드라인 보조 지침: {sheet_key}]\n"
    if max_chars <= len(prefix):
        return prefix.strip()
    parts: list[str] = []
    seen: set[str] = set()
    sections = parsed.sections_by_sheet.get(sheet_key, [])
    blocks = [section.text for priority in (1, 2) for section in sections if section.priority == priority]
    rules = parsed.mapping_rules_by_sheet.get(sheet_key, [])
    if rules:
        blocks.append("## §7 매핑 규칙\n" + "\n".join(rules))
    for block in blocks:
        normalized = block.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _has_markdown_table(normalized) and len(prefix + normalized) > max_chars:
            continue
        _append_block_within_limit(prefix, parts, normalized, max_chars)
    return (prefix + "\n\n".join(parts)).strip() if parts else ""
