"""최종 추출 행을 원문과 결정론적으로 대조하고 품질 지표를 계산한다."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz

import config
from utils.document_objects import DocumentObject, build_document_objects
from utils.evidence_merge import normalize_evidence_ids
from utils.object_evidence_match import descriptor, match_object_evidence
from utils.object_routing import is_index_page, prepare_object_inventory
from utils.pdf_reader import PDFContent
from utils.selective_ocr import apply_triage_metadata, evidence_id, merge_document_objects
from utils.semantic_routing import infer_object_semantic_targets


_COMMON_SKIP_VALUES = {
    "", "n", "y", "yes", "no", "reported", "visual_only", "estimated",
    "미검수", "반영", "보류", "표", "그림", "그래프", "차트",
}
_IGNORE_HEADER_KEYWORDS = (
    "출처페이지", "근거페이지", "페이지", "데이터상태", "검수상태",
    "최종반영여부", "지자체명", "출처파일",
)
_SEARCH_IGNORE_HEADER_KEYWORDS = (
    "시각자료ID", "데이터포함여부", "디지타이징필요", "관련시트",
)
_PRIORITY_HEADER_KEYWORDS = (
    "관리번호", "사업명", "항목명", "지표명", "과제명", "캡션", "유형",
    "추출값요약", "목표", "배출량", "예산", "값", "연도", "내용", "문구",
)
_SEARCH_FIELD_PRIORITY_GROUPS = (
    (
        "관리번호", "캡션", "제목", "번호", "계획명", "사업명", "과제명",
        "항목명", "지표명", "전략명", "거버넌스기구",
    ),
    (
        "부문", "세부부문", "관리부문", "지표범주", "지표세부범주",
        "개요유형", "전략수준", "목표수준", "목표범위", "시나리오",
        "재원구분", "직간접구분", "배출유형",
    ),
    (
        "점검연도", "기준연도", "목표연도", "계획시작연도", "계획종료연도",
        "연도", "기간원문", "발간일",
    ),
    (
        "목표배출량", "목표감축량", "예상감축량", "배출량", "전망값",
        "예산액", "감축률", "원문값", "정규화값", "값", "단위",
    ),
    (
        "항목값", "연간계획", "이행실적", "내용", "문구", "역할", "설명",
    ),
    ("추출값요약",),
)
_GENERIC_TOKENS = {
    "자료", "단위", "비고", "합계", "구분", "기타", "관련시트", "참고자료",
    "해외사례", "추정", "후보", "그래프", "계획", "현황", "목표", "부문",
}
_VISUAL_PAGE_ID_RE = re.compile(r"\bV(\d{1,4})[-_]\d+\b", re.IGNORECASE)
_VISUAL_LABEL_RE = re.compile(r"\[(표|그림)\s*([0-9]+-[0-9]+)\]")
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w가-힣])-?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?(?![\w가-힣])"
    r"|(?<![\w가-힣])-?\d+(?:\.\d+)?%?(?![\w가-힣])"
)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣0-9·ㆍ()/+.-]{1,}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HEADER_ALIASES = {"감축률(%)": "감축률"}


# 비어 있으면 그 행의 의미가 성립하기 어려운 최소 필드만 둔다. 선택 필드는 제외한다.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "document_meta": ("지자체명", "계획명", "계획시작연도", "계획종료연도"),
    "plan_overview": ("개요유형", "항목명", "항목값"),
    "regional_conditions": ("지표명", "연도", "값"),
    "emissions_regional": ("배출유형", "부문", "연도", "배출량", "단위"),
    "emissions_management": ("관리부문", "직간접구분", "연도", "배출량", "단위"),
    "emissions_forecast": ("시나리오", "연도", "전망값", "단위"),
    "reduction_targets": ("목표수준", "기준연도", "목표연도"),
    "vision_strategy": ("전략수준", "전략명"),
    "mitigation_projects": ("사업명", "부문"),
    "annual_implementation": ("사업명", "연도", "연간계획"),
    "quantitative_reductions": ("사업명", "연도", "예상감축량", "단위"),
    "financial_plan": ("사업명", "연도", "예산액", "예산단위"),
    "foundation_measures": ("대응기반영역", "과제명"),
    "governance_feedback": ("거버넌스기구", "역할"),
    "monitoring_performance": ("점검연도", "사업명", "달성여부"),
    "changes_actions": ("점검연도", "사업명", "변경사유"),
}
_REQUIRED_ANY_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "reduction_targets": (("목표감축량", "목표배출량", "감축률"),),
    "annual_implementation": (("연간계획", "목표물량"),),
}
_CORE_GROUPS = (
    ("document_meta",),
    ("emissions_regional", "emissions_management"),
    ("reduction_targets",),
    ("mitigation_projects",),
    ("financial_plan",),
)


@dataclass(frozen=True, slots=True)
class SearchTerm:
    text: str
    header: str
    weight: float
    priority: int


@dataclass(slots=True)
class RowVerification:
    sheet_key: str
    sheet_name: str
    excel_row: int
    status: str
    confidence: str
    source_pages: list[int]
    matched_page: int | None
    score: float
    search_terms: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    row_summary: str = ""
    message: str = ""

    def as_excel_row(self, municipality: str) -> dict[str, Any]:
        return {
            "지자체명": municipality,
            "대상시트": self.sheet_name,
            "원본행번호": self.excel_row,
            "상태": self.status,
            "신뢰도": self.confidence,
            "출처페이지": ",".join(str(page) for page in self.source_pages),
            "확인페이지": self.matched_page,
            "점수": round(self.score, 2),
            "검색어": " | ".join(self.search_terms),
            "일치검색어": " | ".join(self.matched_terms),
            "행요약": self.row_summary,
            "검수메시지": self.message,
        }


@dataclass(slots=True)
class SourceVerificationReport:
    rows: list[RowVerification]
    by_sheet: dict[str, dict[str, Any]]
    total_rows: int
    verifiable_rows: int
    confirmed_rows: int
    weak_rows: int
    unconfirmed_rows: int
    skipped_rows: int
    rows_with_provenance: int
    grounding_ratio: float
    provenance_ratio: float

    def excel_rows(self, municipality: str) -> list[dict[str, Any]]:
        return [row.as_excel_row(municipality) for row in self.rows]

    def summary_text(self) -> str:
        return (
            f"전체 {self.total_rows}행, 검증가능 {self.verifiable_rows}행, "
            f"확인 {self.confirmed_rows}행, 약함 {self.weak_rows}행, "
            f"미확인 {self.unconfirmed_rows}행, 근거점수 {self.grounding_ratio:.1%}, "
            f"출처페이지 보유율 {self.provenance_ratio:.1%}"
        )

    def validation_issues(self, municipality: str) -> list[dict[str, str]]:
        severity = "정보" if self.grounding_ratio >= 0.8 else "경고"
        issues = [{
            "지자체명": municipality,
            "심각도": severity,
            "영역": "원문대조",
            "항목": "원문 근거 확인률",
            "문제내용": self.summary_text(),
            "권장조치": "20_원문대조의 약함·미확인 행을 출처페이지와 함께 우선 검토",
        }]
        if self.provenance_ratio < 0.8:
            issues.append({
                "지자체명": municipality,
                "심각도": "경고",
                "영역": "원문대조",
                "항목": "출처페이지 보유율",
                "문제내용": f"전체 데이터 행 중 출처페이지가 있는 행은 {self.provenance_ratio:.1%}입니다.",
                "권장조치": "출처페이지가 없는 행은 자동 병합하지 말고 원문 페이지를 먼저 확정",
            })
        for sheet_key, stats in self.by_sheet.items():
            verifiable = int(stats.get("검증가능", 0))
            if not verifiable:
                continue
            ratio = float(stats.get("근거점수", 0.0))
            if ratio >= 0.75:
                continue
            issues.append({
                "지자체명": municipality,
                "심각도": "경고",
                "영역": "원문대조",
                "항목": f"{stats['시트명']} 근거 확인 부족",
                "문제내용": (
                    f"검증가능 {verifiable}행 중 확인 {stats.get('확인', 0)}행, "
                    f"약함 {stats.get('약함', 0)}행, 미확인 {stats.get('미확인', 0)}행 "
                    f"(근거점수 {ratio:.1%})"
                ),
                "권장조치": "해당 시트의 미확인 행과 원문 표를 우선 대조",
            })
        return issues


@dataclass(slots=True)
class SourceObjectVerification:
    object_id: str
    object_type: str
    page_number: int
    number: str
    caption: str
    section: str
    row_count: int
    column_count: int
    status: str
    completeness_score: float
    linked_sheets: list[str] = field(default_factory=list)
    linked_rows: int = 0
    message: str = ""
    expected_sheet: str = ""
    allowed_sheets: list[str] = field(default_factory=list)
    auxiliary_linked_sheets: list[str] = field(default_factory=list)
    routing_status: str = "판정불가"
    bbox: tuple[float, float, float, float] | None = None
    native_confidence: float | None = None
    triage_action: str = ""
    triage_reasons: list[str] = field(default_factory=list)
    ocr_backend: str = ""
    ocr_status: str = ""
    final_status: str = "needs_review"
    attempt_count: int = 0
    terminal_reason: str = ""
    evidence_id: str = ""
    match_stage: str = ""
    matched_evidence_ids: list[str] = field(default_factory=list)
    evaluation_target: str = "extraction"
    triage_evaluation_status: str = ""
    index_reference_pages: list[int] = field(default_factory=list)
    duplicate_object_ids: list[str] = field(default_factory=list)

    def as_excel_row(self, municipality: str) -> dict[str, Any]:
        type_labels = {
            "table": "표",
            "chart": "차트/그래프",
            "figure": "그림",
            "image": "이미지",
            "text": "텍스트",
        }
        return {
            "지자체명": municipality,
            "객체ID": self.object_id,
            "객체유형": type_labels.get(self.object_type, self.object_type),
            "출처페이지": self.page_number,
            "번호": self.number,
            "캡션": self.caption,
            "섹션": self.section,
            "행수": self.row_count,
            "열수": self.column_count,
            "연결상태": self.status,
            "완전성점수": round(self.completeness_score, 2),
            "연결시트": ", ".join(self.linked_sheets),
            "연결행수": self.linked_rows,
            "검수메시지": self.message,
            "예상시트": self.expected_sheet,
            "시트정합상태": self.routing_status,
            "좌표": json.dumps(list(self.bbox), ensure_ascii=False) if self.bbox else "",
            "원본신뢰도": (
                round(float(self.native_confidence), 3)
                if self.native_confidence is not None else None
            ),
            "Triage판정": self.triage_action,
            "Triage사유": ", ".join(self.triage_reasons),
            "보완백엔드": self.ocr_backend,
            "보완상태": self.ocr_status,
            "최종상태": self.final_status,
            "시도횟수": self.attempt_count,
            "종결사유": self.terminal_reason,
            "근거ID": self.evidence_id,
            "근거매칭단계": self.match_stage,
            "일치근거ID": ", ".join(self.matched_evidence_ids),
            "평가분모상태": self.evaluation_target,
            "Triage평가상태": self.triage_evaluation_status,
            "허용시트": ", ".join(self.allowed_sheets),
            "보조연결시트": ", ".join(self.auxiliary_linked_sheets),
            "목차참조페이지": ",".join(str(page) for page in self.index_reference_pages),
            "중복객체ID": ", ".join(self.duplicate_object_ids),
        }


@dataclass(slots=True)
class SourceObjectInventoryReport:
    rows: list[SourceObjectVerification]
    total_objects: int
    confirmed_objects: int
    partial_objects: int
    unconfirmed_objects: int
    coverage_ratio: float
    routing_evaluable_objects: int = 0
    routing_mismatch_objects: int = 0
    routing_error_rate: float = 0.0
    extraction_denominator_objects: int = 0
    excluded_candidate_objects: int = 0
    review_candidate_objects: int = 0
    exact_match_objects: int = 0
    composite_match_objects: int = 0
    ambiguous_match_objects: int = 0
    triage_evaluable_objects: int = 0
    triage_missed_objects: int = 0
    triage_miss_rate: float = 0.0
    triage_unresolved_objects: int = 0

    def excel_rows(self, municipality: str) -> list[dict[str, Any]]:
        return [row.as_excel_row(municipality) for row in self.rows]

    def summary_text(self) -> str:
        return (
            f"원문 객체 {self.total_objects}개, 확인 {self.confirmed_objects}개, "
            f"부분 {self.partial_objects}개, 미확인 {self.unconfirmed_objects}개, "
            f"완전성 {self.coverage_ratio:.1%}, "
            f"추출분모 {self.extraction_denominator_objects}개, "
            f"제외후보 {self.excluded_candidate_objects}개, "
            f"Triage 누락의심 {self.triage_missed_objects}개 "
            f"({self.triage_miss_rate:.1%}), "
            f"시트의미 판정 {self.routing_evaluable_objects}개, "
            f"오배치 의심 {self.routing_mismatch_objects}개 "
            f"({self.routing_error_rate:.1%})"
        )

    def validation_issues(self, municipality: str) -> list[dict[str, str]]:
        severity = "정보" if self.coverage_ratio >= 0.8 else "경고"
        issues = [{
            "지자체명": municipality,
            "심각도": severity,
            "영역": "원문객체인벤토리",
            "항목": "표·그래프 객체 완전성",
            "문제내용": self.summary_text(),
            "권장조치": "21_원문객체인벤토리의 부분·미확인 객체를 우선 재추출",
        }]
        if self.unconfirmed_objects:
            pages = sorted({
                row.page_number for row in self.rows
                if row.status in {"미확인", "검토필요"}
            })
            page_text = ",".join(f"p{page}" for page in pages[:20])
            if len(pages) > 20:
                page_text += ",…"
            issues.append({
                "지자체명": municipality,
                "심각도": "경고",
                "영역": "원문객체인벤토리",
                "항목": "미연결 표·그래프 객체",
                "문제내용": f"미확인 {self.unconfirmed_objects}개: {page_text or '페이지 미상'}",
                "권장조치": "해당 페이지를 표 객체 우선 복구 또는 비전 재분석",
            })
        if self.routing_mismatch_objects:
            issues.append({
                "지자체명": municipality,
                "심각도": "경고",
                "영역": "원문객체인벤토리",
                "항목": "표·그래프 시트 의미 불일치",
                "문제내용": (
                    f"캡션·섹션으로 판정 가능한 {self.routing_evaluable_objects}개 중 "
                    f"{self.routing_mismatch_objects}개가 다른 시트에 연결되었습니다 "
                    f"({self.routing_error_rate:.1%})."
                ),
                "권장조치": "21_원문객체인벤토리에서 시트정합상태=오배치의심 객체를 우선 확인",
            })
        return issues


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    score: float
    issues: list[str]
    metrics: dict[str, Any]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return re.sub(r"\s+", " ", _canonicalize_json_fragments(str(value))).strip()


def _normalize(value: str) -> str:
    text = html.unescape(_HTML_TAG_RE.sub(" ", value or "")).casefold()
    text = text.replace(",", "")
    return re.sub(r"[\s\[\](){}'\"“”‘’·ㆍ.:：;,_/\\|%]", "", text)


def _field_priority(header: str) -> int:
    canonical = _HEADER_ALIASES.get(str(header or ""), str(header or ""))
    leaf = re.sub(r"\[\d+\]$", "", canonical.rsplit(".", 1)[-1])
    for priority, group in enumerate(_SEARCH_FIELD_PRIORITY_GROUPS):
        if any(keyword == leaf or keyword in leaf for keyword in group):
            return priority
    return len(_SEARCH_FIELD_PRIORITY_GROUPS)


def _json_fragments(text: str) -> list[tuple[int, int, Any]]:
    """문자열 안의 유효한 JSON 객체/배열 위치와 값을 순서대로 반환한다."""
    fragments: list[tuple[int, int, Any]] = []
    cursor = 0
    while cursor < len(text):
        start = next(
            (index for index in range(cursor, len(text)) if text[index] in "{["),
            -1,
        )
        if start < 0:
            break
        stack: list[str] = []
        in_string = False
        escaped = False
        end = -1
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\" and in_string:
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char in "{[":
                stack.append(char)
            elif char in "}]":
                if not stack:
                    break
                expected = "{" if char == "}" else "["
                if stack[-1] != expected:
                    break
                stack.pop()
                if not stack:
                    end = index
                    break
        if end < 0:
            cursor = start + 1
            continue
        try:
            payload = json.loads(text[start:end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = start + 1
            continue
        fragments.append((start, end + 1, payload))
        cursor = end + 1
    return fragments


def _canonicalize_json_fragments(text: str) -> str:
    fragments = _json_fragments(text)
    if not fragments:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, payload in fragments:
        pieces.append(text[cursor:start])
        pieces.append(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _parse_pages(value: Any, page_count: int) -> list[int]:
    pages: list[int] = []
    for token in re.findall(r"(?:p|P)?\s*(\d{1,4})", _stringify(value)):
        page = int(token)
        if 1 <= page <= page_count and page not in pages:
            pages.append(page)
    return pages


def _flatten_json(value: Any, key_path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key in sorted(
            value,
            key=lambda item: (_field_priority(str(item)), _normalize(str(item)), str(item)),
        ):
            child = value[key]
            next_path = f"{key_path}.{key}" if key_path else str(key)
            yield from _flatten_json(child, next_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten_json(child, f"{key_path}[{index}]")
    else:
        text = _stringify(value)
        if text:
            yield key_path, text


def _source_pages(row: dict[str, Any], page_count: int) -> list[int]:
    pages: list[int] = []

    def add(values: Iterable[int]) -> None:
        for page in values:
            if page not in pages:
                pages.append(page)

    for key, value in row.items():
        key_text = _stringify(key)
        value_text = _stringify(value)
        if any(marker in key_text for marker in ("출처페이지", "근거페이지")):
            add(_parse_pages(value, page_count))
        if "시각자료ID" in key_text:
            add(int(match.group(1)) for match in _VISUAL_PAGE_ID_RE.finditer(value_text))
        if value_text.startswith(("{", "[")):
            try:
                parsed = json.loads(value_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for path, child in _flatten_json(parsed):
                if any(marker in path.casefold() for marker in ("페이지", "page", "근거", "출처")):
                    add(_parse_pages(child, page_count))
    return sorted(page for page in pages if 1 <= page <= page_count)


def _numeric_variants(text: str) -> list[str]:
    compact = text.replace(",", "").replace("%", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", compact):
        return [text]
    # set 순회 순서는 프로세스 해시 시드에 따라 달라진다. 검색어 상한과 마킹 시
    # 앞의 항목만 사용하므로 원문 표기 → 무구분 표기 → 표시 변형 순서를 고정한다.
    variants = [text, compact]
    try:
        value = float(compact)
    except ValueError:
        return list(dict.fromkeys(variants))
    if value.is_integer():
        integer = int(value)
        variants.extend((str(integer), f"{integer:,}"))
    else:
        variants.extend((f"{value:g}", f"{value:,.1f}", f"{value:,.2f}"))
    return [variant for variant in dict.fromkeys(variants) if variant]


def _split_text(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= 58:
        return [text]
    candidates = [
        part.strip() for part in re.split(r"[.;。:：,，/]| - |ㆍ", text)
        if 8 <= len(part.strip()) <= 58
    ]
    words = text.split()
    if len(words) >= 4:
        candidates.append(" ".join(words[: min(6, len(words))]))
    candidates.extend(text[:width].strip() for width in (36, 46, 56) if len(text[:width].strip()) >= 8)
    return list(dict.fromkeys(candidates))


def _atomic_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for token in _WORD_TOKEN_RE.findall(text):
        token = token.strip(".,;:()[]{}")
        if len(token) >= 2 and token.casefold() not in _COMMON_SKIP_VALUES and token not in _GENERIC_TOKENS:
            candidates.append(token)
    for token in _NUMBER_TOKEN_RE.findall(text):
        if token not in {"0", "1"}:
            candidates.append(token)
    parts = text.split()
    for left, right in zip(parts, parts[1:]):
        pair = f"{left.strip('.,;:()[]{}')} {right.strip('.,;:()[]{}')}"
        if 4 <= len(pair) <= 38 and not _VISUAL_PAGE_ID_RE.search(pair):
            candidates.append(pair)
    return candidates


def _value_candidates(header: str, text: str) -> list[tuple[str, str]]:
    fragments = _json_fragments(text)
    if fragments:
        values: list[tuple[str, str]] = []
        cursor = 0
        plain_parts: list[str] = []
        for start, end, parsed in fragments:
            plain_parts.append(text[cursor:start])
            cursor = end
            for path, child in _flatten_json(parsed):
                source_header = re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1]) or header
                values.extend((candidate, source_header) for candidate in _split_text(child))
                values.extend((candidate, source_header) for candidate in _atomic_candidates(child))
                if any(keyword in path for keyword in _PRIORITY_HEADER_KEYWORDS):
                    values.append((child, source_header))
        plain_parts.append(text[cursor:])
        plain_text = " ".join(part for part in plain_parts if part.strip())
        if plain_text:
            if "캡션" in header:
                values.extend(
                    (match.group(0), header)
                    for match in _VISUAL_LABEL_RE.finditer(plain_text)
                )
                values.extend((candidate, header) for candidate in _split_text(plain_text))
            elif "추출값요약" in header:
                values.extend((candidate, header) for candidate in _atomic_candidates(plain_text))
            else:
                values.extend((candidate, header) for candidate in _split_text(plain_text))
        return values
    if "캡션" in header:
        labels = [match.group(0) for match in _VISUAL_LABEL_RE.finditer(text)]
        return [(candidate, header) for candidate in [*labels, *_split_text(text)]]
    if "추출값요약" in header:
        return [(candidate, header) for candidate in _atomic_candidates(text)]
    return [(candidate, header) for candidate in _split_text(text)]


def _term_weight(text: str, header: str) -> float:
    weight = 1.0
    if any(keyword in header for keyword in _PRIORITY_HEADER_KEYWORDS):
        weight += 1.0
    if "캡션" in header and _VISUAL_LABEL_RE.search(text):
        weight += 3.0
    if re.fullmatch(r"[A-Z가-힣]?\d+-\d+(?:-\d+)?", text):
        weight += 2.0
    if len(text) >= 8:
        weight += 1.0
    if len(text) >= 18:
        weight += 1.0
    if re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?%?", text):
        weight += 0.1
    return max(weight, 0.5)


def _build_terms(headers: list[str], row: dict[str, Any], max_terms: int) -> list[SearchTerm]:
    raw: list[SearchTerm] = []
    generic = {_normalize(token) for token in _GENERIC_TOKENS}
    ordered_headers = sorted(
        headers,
        key=lambda header: (_field_priority(header), _normalize(header), header),
    )
    for header in ordered_headers:
        if any(keyword in header for keyword in (*_IGNORE_HEADER_KEYWORDS, *_SEARCH_IGNORE_HEADER_KEYWORDS)):
            continue
        key = _HEADER_ALIASES.get(header, header)
        text = _stringify(row.get(key))
        if len(text) < 2 or text.casefold() in _COMMON_SKIP_VALUES:
            continue
        for candidate, source_header in _value_candidates(header, text):
            for variant in _numeric_variants(candidate):
                if len(variant) < 2 or _normalize(variant) in generic:
                    continue
                raw.append(SearchTerm(
                    variant,
                    source_header,
                    _term_weight(variant, source_header),
                    _field_priority(source_header),
                ))
    dedup: dict[str, SearchTerm] = {}
    for term in raw:
        key = _normalize(term.text)
        previous = dedup.get(key)
        if key and (
            previous is None
            or term.priority < previous.priority
            or (term.priority == previous.priority and term.weight > previous.weight)
        ):
            dedup[key] = term
    return sorted(
        dedup.values(),
        key=lambda term: (
            term.priority,
            -term.weight,
            -len(term.text),
            _normalize(term.text),
            term.header,
            term.text,
        ),
    )[:max_terms]


def _row_summary(headers: list[str], row: dict[str, Any]) -> str:
    parts = []
    for header in headers:
        if any(keyword in header for keyword in _IGNORE_HEADER_KEYWORDS):
            continue
        value = _stringify(row.get(_HEADER_ALIASES.get(header, header)))
        if value:
            parts.append(f"{header}={value}")
        if len(parts) >= 6:
            break
    return " | ".join(parts)[:700]


def _page_window(source_pages: list[int], page_count: int, radius: int) -> list[int]:
    pages: set[int] = set()
    for source in source_pages:
        pages.update(range(max(1, source - radius), min(page_count, source + radius) + 1))
    return sorted(pages)


def _is_identity_term(term: str) -> bool:
    normalized = _normalize(term)
    if not normalized or re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", normalized):
        return False
    return any(char.isalpha() or "가" <= char <= "힣" for char in normalized)


def _confidence(score: float, matched: list[str]) -> tuple[str, str]:
    has_identity = any(_is_identity_term(term) for term in matched)
    if has_identity and (score >= 5 or len(matched) >= 3):
        return "확인", "high"
    if has_identity and (score >= 2.5 or any(len(term) >= 8 for term in matched)):
        return "확인", "medium"
    if matched:
        return "약함", "low"
    return "미확인", "none"


def _search_pages(
    page_numbers: Iterable[int],
    terms: list[SearchTerm],
    normalized_pages: dict[int, str],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    normalized_terms = [(term, _normalize(term.text)) for term in terms]
    for page_number in page_numbers:
        body = normalized_pages.get(page_number, "")
        if not body:
            continue
        matched = [term for term, key in normalized_terms if key and key in body]
        if not matched:
            continue
        score = sum(term.weight for term in matched)
        if best is None or score > best["score"]:
            best = {
                "page": page_number,
                "score": score,
                "matched_terms": [term.text for term in matched],
            }
    return best


def verify_final_data(
    final_data: dict[str, Any],
    document: PDFContent,
    *,
    page_radius: int = 1,
    max_terms: int = 12,
    global_search: bool = True,
) -> SourceVerificationReport:
    """00~16 데이터 행을 원문 텍스트·파싱 표와 대조한다. LLM 호출은 없다."""
    page_count = document.total_pages
    normalized_pages = {
        page.page_number: _normalize("\n".join([page.text or "", *(page.tables or [])]))
        for page in document.pages
    }
    all_pages = sorted(normalized_pages)
    results: list[RowVerification] = []

    sheet_keys = [*config.EXTRACTION_SHEETS, "visual_inventory"]
    for sheet_key in sheet_keys:
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
        headers = list(config.EXCEL_HEADERS.get(sheet_name, []))
        for index, source_row in enumerate(rows, start=2):
            if not isinstance(source_row, dict):
                continue
            row = dict(source_row)
            source_pages = _source_pages(row, page_count)
            terms = _build_terms(headers, row, max_terms)
            summary = _row_summary(headers, row)
            if not terms:
                results.append(RowVerification(
                    sheet_key, sheet_name, index, "건너뜀", "none", source_pages, None, 0.0,
                    row_summary=summary, message="검색할 식별값이 없습니다.",
                ))
                continue

            preferred = _page_window(source_pages, page_count, max(0, page_radius))
            best = _search_pages(preferred, terms, normalized_pages) if preferred else None
            if best is None and global_search:
                preferred_set = set(preferred)
                best = _search_pages((page for page in all_pages if page not in preferred_set), terms, normalized_pages)

            if best is None:
                results.append(RowVerification(
                    sheet_key, sheet_name, index, "미확인", "none", source_pages, None, 0.0,
                    search_terms=[term.text for term in terms], row_summary=summary,
                    message="PDF/HWP 파싱 텍스트와 표에서 식별값을 찾지 못했습니다.",
                ))
                continue

            status, confidence = _confidence(best["score"], best["matched_terms"])
            message = "출처페이지 주변에서 확인했습니다."
            if source_pages and best["page"] not in preferred:
                message = "기재된 출처페이지에서는 찾지 못했지만 문서의 다른 페이지에서 확인했습니다."
            elif not source_pages:
                message = "출처페이지가 없어 문서 전체 검색으로 확인했습니다."
            results.append(RowVerification(
                sheet_key, sheet_name, index, status, confidence, source_pages, best["page"], best["score"],
                search_terms=[term.text for term in terms], matched_terms=best["matched_terms"],
                row_summary=summary, message=message,
            ))

    weights = {"high": 1.0, "medium": 0.75, "low": 0.25, "none": 0.0}
    verifiable = [row for row in results if row.status != "건너뜀"]
    grounding = sum(weights.get(row.confidence, 0.0) for row in verifiable) / len(verifiable) if verifiable else 0.0
    provenance = sum(bool(row.source_pages) for row in results) / len(results) if results else 0.0
    by_sheet: dict[str, dict[str, Any]] = {}
    for sheet_key in sheet_keys:
        sheet_rows = [row for row in results if row.sheet_key == sheet_key]
        if not sheet_rows:
            continue
        checkable = [row for row in sheet_rows if row.status != "건너뜀"]
        sheet_grounding = (
            sum(weights.get(row.confidence, 0.0) for row in checkable) / len(checkable)
            if checkable else 0.0
        )
        by_sheet[sheet_key] = {
            "시트명": config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key),
            "전체": len(sheet_rows),
            "검증가능": len(checkable),
            "확인": sum(row.status == "확인" for row in checkable),
            "약함": sum(row.status == "약함" for row in checkable),
            "미확인": sum(row.status == "미확인" for row in checkable),
            "근거점수": sheet_grounding,
        }

    return SourceVerificationReport(
        rows=results,
        by_sheet=by_sheet,
        total_rows=len(results),
        verifiable_rows=len(verifiable),
        confirmed_rows=sum(row.status == "확인" for row in verifiable),
        weak_rows=sum(row.status == "약함" for row in verifiable),
        unconfirmed_rows=sum(row.status == "미확인" for row in verifiable),
        skipped_rows=sum(row.status == "건너뜀" for row in results),
        rows_with_provenance=sum(bool(row.source_pages) for row in results),
        grounding_ratio=grounding,
        provenance_ratio=provenance,
    )


def _object_inventory_terms(obj: DocumentObject) -> tuple[list[str], list[str]]:
    """객체 식별어와 표 셀 대조어를 분리한다."""
    identity: list[str] = []
    for value in (obj.number, obj.caption):
        normalized = _normalize(value)
        if len(normalized) >= 3 and normalized not in identity:
            identity.append(normalized)

    cell_terms: list[str] = []
    for row in obj.rows[:12]:
        for cell in row[:12]:
            for candidate in _atomic_candidates(str(cell or "")):
                normalized = _normalize(candidate)
                if (
                    len(normalized) >= 2
                    and normalized not in _GENERIC_TOKENS
                    and normalized not in cell_terms
                ):
                    cell_terms.append(normalized)
                if len(cell_terms) >= 30:
                    return identity, cell_terms
    return identity, cell_terms


def _is_data_object(obj: DocumentObject, page_text: str) -> bool:
    if obj.object_type not in {"table", "chart", "figure", "image"}:
        return False
    if obj.metadata.get("render_proxy") or obj.metadata.get("triage_action") == "transport_only":
        return False
    if obj.metadata.get("is_index_reference") or is_index_page(page_text):
        return False
    strong_signals = [
        str(value)
        for value in [
            *(obj.metadata.get("triage_reasons") or []),
            *(obj.metadata.get("negative_revalidation_signals") or []),
        ]
        if str(value).startswith("strong:")
    ]
    if obj.object_type == "table":
        if not obj.rows or len(obj.rows) < 2:
            return bool(
                obj.metadata.get("triage_action") == "ocr_required"
                and (obj.number or obj.caption)
            )
        cells = " ".join(cell for row in obj.rows[:12] for cell in row[:12])
        has_data_signal = bool(_NUMBER_TOKEN_RE.search(cells)) or any(
            marker in cells
            for marker in ("연도", "배출량", "예산", "목표", "사업명", "지표", "단위", "실적")
        )
        return bool(obj.number or obj.caption or has_data_signal)
    if obj.object_type == "chart":
        return bool(obj.number or obj.caption or obj.rows)
    if obj.object_type in {"figure", "image"}:
        return bool(
            obj.rows
            or strong_signals
            or obj.metadata.get("triage_action") == "ocr_required"
            or obj.metadata.get("ocr_status") in {
                "parsed", "loaded", "added_missing_object", "enriched_native",
                "replaced_low_confidence_native",
            }
        )
    return bool(obj.number or obj.caption)


def _object_evaluation_target(obj: DocumentObject, page_text: str) -> str:
    """객체를 삭제하지 않고 추출·음성·검토 분모 중 하나로 배치한다."""
    if obj.object_type not in {"table", "chart", "figure", "image"}:
        return ""
    if obj.metadata.get("render_proxy") or obj.metadata.get("triage_action") == "transport_only":
        return ""
    if obj.metadata.get("is_index_reference") or is_index_page(page_text):
        return ""
    if _is_data_object(obj, page_text):
        return "extraction"
    if obj.metadata.get("triage_action") == "skip_non_data":
        return "triage_negative"
    if (
        obj.metadata.get("final_status") == "needs_review"
        or obj.number
        or obj.caption
    ):
        return "review"
    return ""


def _triage_evaluation_status(obj: DocumentObject, evaluation_target: str) -> str:
    action = str(obj.metadata.get("triage_action") or "")
    final_status = str(obj.metadata.get("final_status") or "needs_review")
    if evaluation_target == "extraction":
        if action == "skip_non_data":
            return "false_negative_candidate"
        if final_status == "needs_review":
            return "unresolved"
        return "selected"
    if evaluation_target == "triage_negative":
        return "negative_selected" if action == "skip_non_data" else "over_selected_candidate"
    return "unresolved"


def _inventory_document_objects(
    final_data: dict[str, Any],
    document: PDFContent,
    native_objects: Sequence[DocumentObject] | None = None,
) -> list[DocumentObject]:
    """파이프라인 객체 원장을 우선 사용하고 구버전 결과는 재구성한다."""
    serialized = final_data.get("document_objects")
    if isinstance(serialized, list) and serialized:
        objects: list[DocumentObject] = []
        for row in serialized:
            if not isinstance(row, dict):
                continue
            try:
                objects.append(DocumentObject.from_dict(row))
            except (TypeError, ValueError):
                continue
        if objects:
            # 초기 A/B 스냅샷은 용량 절감을 위해 rows/text를 생략했다. 같은 PDF에서
            # 재구성한 native 객체로 누락 payload만 복원해 평가 분모를 보존한다.
            native = list(native_objects) if native_objects is not None else build_document_objects(document.pages)
            native_by_id = {obj.object_id: obj for obj in native}
            for obj in objects:
                base = native_by_id.pop(obj.object_id, None)
                if base is None:
                    continue
                if not obj.rows:
                    obj.rows = base.rows
                if not obj.text:
                    obj.text = base.text
                if not obj.nearby_text:
                    obj.nearby_text = base.nearby_text
                if not obj.caption:
                    obj.caption = base.caption
                if not obj.number:
                    obj.number = base.number
                if not obj.section:
                    obj.section = base.section
                if obj.bbox is None:
                    obj.bbox = base.bbox
            # 스냅샷에서 제외된 native 객체는 병합 단계에서 중복·비대상으로 정리된
            # 객체일 수 있으므로 평가 분모에 새로 추가하지 않는다.
            objects.sort(
                key=lambda obj: (
                    obj.page_number,
                    obj.sequence,
                    obj.object_type,
                    obj.object_id,
                )
            )
            return objects

    native = list(native_objects) if native_objects is not None else build_document_objects(document.pages)
    triage = final_data.get("object_triage")
    if isinstance(triage, list):
        apply_triage_metadata(native, triage)
    ocr_objects: list[DocumentObject] = []
    serialized_ocr = final_data.get("ocr_document_objects")
    if isinstance(serialized_ocr, list):
        for row in serialized_ocr:
            if not isinstance(row, dict):
                continue
            try:
                ocr_objects.append(DocumentObject.from_dict(row))
            except (TypeError, ValueError):
                continue
    return merge_document_objects(native, ocr_objects) if ocr_objects else native


def build_source_object_inventory(
    final_data: dict[str, Any],
    document: PDFContent,
    *,
    document_objects: Sequence[DocumentObject] | None = None,
) -> SourceObjectInventoryReport:
    """원문의 데이터 표·그래프를 결과 행에 역방향으로 연결한다. LLM은 사용하지 않는다."""
    page_count = document.total_pages
    page_text = {page.page_number: page.text or "" for page in document.pages}
    prepared = prepare_object_inventory(
        _inventory_document_objects(final_data, document, document_objects),
        page_text,
    )
    object_targets = {
        obj.object_id: _object_evaluation_target(
            obj, page_text.get(obj.page_number, "")
        )
        for obj in prepared.objects
    }
    objects = [obj for obj in prepared.objects if object_targets.get(obj.object_id)]

    indexed_rows: dict[str, tuple[str, str, set[int], dict[str, Any]]] = {}
    result_descriptors = []
    for sheet_key in [*getattr(config, "EXTRACTION_SHEETS", []), "visual_inventory"]:
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            normalized = _normalize(json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ))
            pages = set(_source_pages(row, page_count))
            row_evidence_ids = normalize_evidence_ids(
                row.get("근거ID"), row.get("근거ID목록")
            )
            row_object_ids = [
                value for value in (
                    row.get("객체ID"), row.get("근거객체ID"),
                ) if value
            ]
            descriptor_pages = sorted(pages) or [None]
            for descriptor_page in descriptor_pages:
                key = f"{sheet_key}:{row_index}:p{descriptor_page or 0}"
                indexed_rows[key] = (sheet_key, normalized, pages, row)
                result_descriptors.append(descriptor(
                    key=key,
                    page=descriptor_page,
                    evidence_ids=row_evidence_ids,
                    object_ids=row_object_ids,
                    number=row.get("번호"),
                    caption=row.get("캡션") or row.get("제목"),
                    content=json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
                ))

    evidence_object_ids: dict[str, set[str]] = {}
    for obj in objects:
        obj_evidence_id = str(obj.metadata.get("evidence_id") or evidence_id(obj))
        for value in normalize_evidence_ids(obj_evidence_id):
            evidence_object_ids.setdefault(value, set()).add(obj.object_id)
    ambiguous_evidence_ids = {
        value for value, object_ids in evidence_object_ids.items() if len(object_ids) > 1
    }

    partial_weight = max(
        0.0,
        min(1.0, float(getattr(config, "SOURCE_OBJECT_PARTIAL_WEIGHT", 0.5))),
    )
    results: list[SourceObjectVerification] = []
    for obj in objects:
        evaluation_target = object_targets[obj.object_id]
        semantic_targets = infer_object_semantic_targets(obj)
        obj_evidence_id = str(obj.metadata.get("evidence_id") or evidence_id(obj))
        match = match_object_evidence(
            descriptor(
                key=obj.object_id,
                page=obj.page_number,
                evidence_ids=[obj_evidence_id],
                object_ids=[
                    obj.object_id,
                    *(obj.metadata.get("duplicate_object_ids") or []),
                ],
                number=obj.number,
                caption=obj.caption,
                content=" ".join(
                    str(cell or "") for row in obj.rows[:20] for cell in row[:20]
                ),
            ),
            result_descriptors,
            ambiguous_evidence_ids=ambiguous_evidence_ids,
        )
        matched_entries = [
            indexed_rows[key] for key in match.matched_keys if key in indexed_rows
        ]
        linked = matched_entries
        linked_sheets = sorted({
            config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            for sheet_key, _row_text, _pages, _row in linked
        })
        confirmed_sheet_keys = (
            {sheet_key for sheet_key, _row_text, _pages, _row in matched_entries}
            if match.confirmed else set()
        )
        body_confirmed_keys = confirmed_sheet_keys.intersection(
            set(getattr(config, "EXTRACTION_SHEETS", []))
        )
        auxiliary_confirmed_keys = confirmed_sheet_keys - body_confirmed_keys
        body_confirmed_sheets = {
            config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            for sheet_key in body_confirmed_keys
        }
        auxiliary_linked_sheets = sorted(
            config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            for sheet_key in auxiliary_confirmed_keys
        )
        allowed_sheet_keys = [target.sheet_key for target in semantic_targets]
        allowed_sheets = [
            config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            for sheet_key in allowed_sheet_keys
        ]
        expected_sheet = (
            allowed_sheets[0]
            if allowed_sheets
            else ""
        )
        if not semantic_targets or not match.confirmed:
            routing_status = "판정불가"
        elif any(sheet_key in body_confirmed_keys for sheet_key in allowed_sheet_keys):
            routing_status = "일치"
        elif body_confirmed_sheets:
            routing_status = "오배치의심"
        elif auxiliary_confirmed_keys:
            routing_status = "본문미연결"
        else:
            routing_status = "판정불가"
        if evaluation_target == "triage_negative":
            status = "제외후보"
            score = 0.0
            message = "비데이터로 분류된 객체를 Triage 평가 분모에 보존했습니다."
        elif match.confirmed:
            status = "확인"
            score = 1.0
            message = match.reason
        elif match.status == "partial":
            status = "부분"
            score = partial_weight
            message = match.reason
        elif match.status == "ambiguous":
            status = "검토필요"
            score = 0.0
            message = match.reason
        else:
            status = "미확인"
            score = 0.0
            message = match.reason
        results.append(SourceObjectVerification(
            object_id=obj.object_id,
            object_type=obj.object_type,
            page_number=obj.page_number,
            number=obj.number,
            caption=obj.caption,
            section=obj.section,
            row_count=int(obj.metadata.get("row_count", len(obj.rows)) or 0),
            column_count=int(obj.metadata.get("column_count", 0) or 0),
            status=status,
            completeness_score=score,
            linked_sheets=linked_sheets,
            linked_rows=len(linked),
            message=message,
            expected_sheet=expected_sheet,
            allowed_sheets=allowed_sheets,
            auxiliary_linked_sheets=auxiliary_linked_sheets,
            routing_status=routing_status,
            bbox=obj.bbox,
            native_confidence=(
                float(obj.metadata["native_confidence"])
                if obj.metadata.get("native_confidence") is not None else None
            ),
            triage_action=str(obj.metadata.get("triage_action") or ""),
            triage_reasons=[str(value) for value in (obj.metadata.get("triage_reasons") or [])],
            ocr_backend=str(obj.metadata.get("ocr_backend") or obj.metadata.get("engine") or ""),
            ocr_status=str(obj.metadata.get("ocr_status") or ""),
            final_status=str(obj.metadata.get("final_status") or "needs_review"),
            attempt_count=int(obj.metadata.get("attempt_count") or 0),
            terminal_reason=str(obj.metadata.get("terminal_reason") or ""),
            evidence_id=obj_evidence_id,
            match_stage=match.status,
            matched_evidence_ids=list(match.matched_evidence_ids),
            evaluation_target=evaluation_target,
            triage_evaluation_status=_triage_evaluation_status(obj, evaluation_target),
            index_reference_pages=sorted({
                int(page)
                for page in (obj.metadata.get("index_reference_pages") or [])
                if str(page).isdigit()
            }),
            duplicate_object_ids=[
                str(value)
                for value in (obj.metadata.get("duplicate_object_ids") or [])
                if value
            ],
        ))

    total = len(results)
    extraction_rows = [
        row for row in results if row.evaluation_target in {"extraction", "review"}
    ]
    coverage_denominator = len(extraction_rows)
    coverage = (
        sum(row.completeness_score for row in extraction_rows) / coverage_denominator
        if coverage_denominator else 1.0
    )
    routing_statuses = {"일치", "오배치의심", "본문미연결"}
    routing_evaluable = sum(row.routing_status in routing_statuses for row in results)
    routing_mismatches = sum(
        row.routing_status in {"오배치의심", "본문미연결"} for row in results
    )
    return SourceObjectInventoryReport(
        rows=results,
        total_objects=total,
        confirmed_objects=sum(row.status == "확인" for row in extraction_rows),
        partial_objects=sum(row.status == "부분" for row in extraction_rows),
        unconfirmed_objects=sum(
            row.status in {"미확인", "검토필요"} for row in extraction_rows
        ),
        coverage_ratio=coverage,
        routing_evaluable_objects=routing_evaluable,
        routing_mismatch_objects=routing_mismatches,
        routing_error_rate=(
            routing_mismatches / routing_evaluable if routing_evaluable else 0.0
        ),
        extraction_denominator_objects=coverage_denominator,
        excluded_candidate_objects=sum(
            row.evaluation_target == "triage_negative" for row in results
        ),
        review_candidate_objects=sum(
            row.evaluation_target == "review" for row in results
        ),
        exact_match_objects=sum(
            row.match_stage in {"exact_id", "exact_object_id"} for row in extraction_rows
        ),
        composite_match_objects=sum(row.match_stage == "composite" for row in extraction_rows),
        ambiguous_match_objects=sum(row.match_stage == "ambiguous" for row in extraction_rows),
        triage_evaluable_objects=len(results),
        triage_missed_objects=sum(
            row.triage_evaluation_status == "false_negative_candidate" for row in results
        ),
        triage_miss_rate=(
            sum(
                row.triage_evaluation_status == "false_negative_candidate"
                for row in results
            ) / coverage_denominator
            if coverage_denominator else 0.0
        ),
        triage_unresolved_objects=sum(
            row.triage_evaluation_status == "unresolved" for row in results
        ),
    )


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip().casefold() in {"", "null", "none", "n/a", "nan"}:
        return False
    return True


def _validation_issue_type(row: dict[str, Any]) -> tuple[str, str]:
    """행 번호·연도·관리번호가 달라도 같은 원인의 경고는 한 유형으로 센다."""
    area = str(row.get("영역") or "")
    item = str(row.get("항목") or "")
    detail = str(row.get("문제내용") or "")
    combined = f"{item} {detail}"
    known_types = (
        ("기존계획 평가 장", "기존계획 문맥 혼입"),
        ("원장 호출실패", "추출 원장 호출실패"),
        ("원장 파싱실패", "추출 원장 파싱실패"),
        ("합계≠부분합", "재정 합계-부분합 불일치"),
        ("중복 키 값 충돌", "중복 키 값 충돌"),
        ("관리번호", "관리번호 정합성"),
    )
    for marker, issue_type in known_types:
        if marker in combined:
            return area, issue_type
    normalized = re.sub(r"\([^)]*\)", "", item)
    normalized = re.sub(r"\b(?:행\s*)?\d+(?:~\d+)?\b", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return area, normalized or "미분류"


def assess_quality(
    final_data: dict[str, Any],
    verification: SourceVerificationReport | None = None,
    source_inventory: SourceObjectInventoryReport | None = None,
) -> QualityAssessment:
    """행 근거와 원문 객체 완전성을 분리해 결정론적 품질을 계산한다."""
    required_total = 0
    required_filled = 0
    for sheet_key, required_fields in _REQUIRED_FIELDS.items():
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            required_total += len(required_fields)
            required_filled += sum(_filled(row.get(field)) for field in required_fields)
            for alternatives in _REQUIRED_ANY_FIELDS.get(sheet_key, ()):
                required_total += 1
                required_filled += any(_filled(row.get(field)) for field in alternatives)
    fill_ratio = required_filled / required_total if required_total else 0.0

    core_present = sum(
        any(isinstance(final_data.get(key), list) and final_data.get(key) for key in group)
        for group in _CORE_GROUPS
    )
    core_ratio = core_present / len(_CORE_GROUPS)

    validation_rows = final_data.get("validation_report", [])
    if not isinstance(validation_rows, list):
        validation_rows = []
    base_validation = [
        row for row in validation_rows
        if isinstance(row, dict)
        and row.get("영역") not in {"원문대조", "원문객체인벤토리"}
    ]
    errors = sum(row.get("심각도") == "오류" for row in base_validation)
    warnings = sum(row.get("심각도") == "경고" for row in base_validation)
    error_types = {_validation_issue_type(row) for row in base_validation if row.get("심각도") == "오류"}
    warning_types = {_validation_issue_type(row) for row in base_validation if row.get("심각도") == "경고"}
    integrity_ratio = max(0.0, 1.0 - min(1.0, len(error_types) * 0.2 + len(warning_types) * 0.035))

    grounding_ratio = verification.grounding_ratio if verification is not None else 0.0
    provenance_ratio = verification.provenance_ratio if verification is not None else 0.0
    object_ratio = source_inventory.coverage_ratio if source_inventory is not None else 0.0
    pipeline_metrics = final_data.get("pipeline_metrics", {})
    if not isinstance(pipeline_metrics, dict):
        pipeline_metrics = {}
    extraction_total = int(pipeline_metrics.get("extraction_batches_total", 0) or 0)
    extraction_ok = int(pipeline_metrics.get("extraction_batches_ok", 0) or 0)
    extraction_ratio = extraction_ok / extraction_total if extraction_total else 1.0
    row_routing_error = pipeline_metrics.get("routing_error_rate")
    object_routing_error = (
        source_inventory.routing_error_rate if source_inventory is not None else None
    )
    routing_error_rate = max(
        float(row_routing_error or 0.0),
        float(object_routing_error or 0.0),
    )
    if source_inventory is not None:
        components = {
            "원문근거": grounding_ratio * 25.0,
            "원문객체": object_ratio * 5.0,
            "필수필드": fill_ratio * 20.0,
            "출처페이지": provenance_ratio * 10.0,
            "정합성": integrity_ratio * 15.0,
            "핵심시트": core_ratio * 10.0,
            "추출성공": extraction_ratio * 15.0,
        }
    else:
        components = {
            "원문근거": grounding_ratio * 30.0,
            "필수필드": fill_ratio * 20.0,
            "출처페이지": provenance_ratio * 10.0,
            "정합성": integrity_ratio * 15.0,
            "핵심시트": core_ratio * 10.0,
            "추출성공": extraction_ratio * 15.0,
        }
    score = sum(components.values())
    if source_inventory is None:
        score = min(score, float(getattr(config, "QUALITY_MAX_WITHOUT_SOURCE_INVENTORY", 95.0)))

    issues: list[str] = []
    if verification is None:
        issues.append("원문 대조 비활성: 원문 근거 점수를 계산할 수 없음")
    elif grounding_ratio < 0.8:
        issues.append(f"원문 근거 점수 낮음 ({grounding_ratio:.0%})")
    if source_inventory is not None and object_ratio < 0.8:
        issues.append(f"원문 표·그래프 객체 완전성 낮음 ({object_ratio:.0%})")
    if fill_ratio < 0.8:
        issues.append(f"필수 필드 채움률 낮음 ({fill_ratio:.0%})")
    if provenance_ratio < 0.8:
        issues.append(f"출처페이지 보유율 낮음 ({provenance_ratio:.0%})")
    if extraction_ratio < 0.95:
        failures = max(0, extraction_total - extraction_ok)
        issues.append(
            f"추출 배치 성공률 낮음 ({extraction_ratio:.0%}, 실패 {failures}/{extraction_total})"
        )
    if core_ratio < 1.0:
        missing = [
            "/".join(config.SHEET_KEY_TO_NAME.get(key, key) for key in group)
            for group in _CORE_GROUPS
            if not any(isinstance(final_data.get(key), list) and final_data.get(key) for key in group)
        ]
        issues.append("핵심 시트 데이터 없음: " + ", ".join(missing))
    if errors or warnings:
        issues.append(f"결정론적 정합성 이슈: 오류 {errors}건, 경고 {warnings}건")
    if source_inventory is None:
        issues.append(
            f"원문 전체 객체 인벤토리 미연결로 점수 상한 "
            f"{getattr(config, 'QUALITY_MAX_WITHOUT_SOURCE_INVENTORY', 95.0):g}점 적용"
        )
    if routing_error_rate > 0.05:
        issues.append(f"시트 의미 라우팅 오류율 높음 ({routing_error_rate:.1%})")

    metrics = {
        "score": round(score, 2),
        "grounding_ratio": round(grounding_ratio, 4),
        "source_object_coverage_ratio": round(object_ratio, 4),
        "required_field_fill_ratio": round(fill_ratio, 4),
        "provenance_ratio": round(provenance_ratio, 4),
        "integrity_ratio": round(integrity_ratio, 4),
        "core_sheet_ratio": round(core_ratio, 4),
        "extraction_success_ratio": round(extraction_ratio, 4),
        "extraction_batches_total": extraction_total,
        "extraction_batches_ok": extraction_ok,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "validation_error_types": len(error_types),
        "validation_warning_types": len(warning_types),
        "components": {key: round(value, 2) for key, value in components.items()},
        "source_inventory_connected": source_inventory is not None,
        "source_objects_total": source_inventory.total_objects if source_inventory is not None else 0,
        "source_objects_confirmed": source_inventory.confirmed_objects if source_inventory is not None else 0,
        "source_objects_partial": source_inventory.partial_objects if source_inventory is not None else 0,
        "source_objects_unconfirmed": source_inventory.unconfirmed_objects if source_inventory is not None else 0,
        "source_object_extraction_denominator": (
            source_inventory.extraction_denominator_objects if source_inventory is not None else 0
        ),
        "source_object_excluded_candidates": (
            source_inventory.excluded_candidate_objects if source_inventory is not None else 0
        ),
        "source_object_review_candidates": (
            source_inventory.review_candidate_objects if source_inventory is not None else 0
        ),
        "source_object_exact_matches": (
            source_inventory.exact_match_objects if source_inventory is not None else 0
        ),
        "source_object_composite_matches": (
            source_inventory.composite_match_objects if source_inventory is not None else 0
        ),
        "source_object_ambiguous_matches": (
            source_inventory.ambiguous_match_objects if source_inventory is not None else 0
        ),
        "triage_evaluable_objects": (
            source_inventory.triage_evaluable_objects if source_inventory is not None else 0
        ),
        "triage_missed_objects": (
            source_inventory.triage_missed_objects if source_inventory is not None else 0
        ),
        "triage_miss_rate": round(
            source_inventory.triage_miss_rate if source_inventory is not None else 0.0,
            4,
        ),
        "triage_unresolved_objects": (
            source_inventory.triage_unresolved_objects if source_inventory is not None else 0
        ),
        # 아래 세 지표는 총점과 분리해 보고한다. 셀 정확도는 고정 골든셋
        # 평가를 명시적으로 실행한 경우에만 외부 평가 리포트에서 채워진다.
        "cell_accuracy": None,
        "object_recall": None,
        "automatic_source_object_coverage": round(object_ratio, 4),
        "routing_error_rate": round(routing_error_rate, 4),
        "row_routing_error_rate": round(float(row_routing_error or 0.0), 4),
        "object_routing_error_rate": round(float(object_routing_error or 0.0), 4),
        "object_routing_evaluable": (
            source_inventory.routing_evaluable_objects if source_inventory is not None else 0
        ),
        "object_routing_mismatches": (
            source_inventory.routing_mismatch_objects if source_inventory is not None else 0
        ),
    }
    return QualityAssessment(round(max(0.0, score), 2), issues, metrics)


def create_marked_pdf(
    source_path: str | Path,
    report: SourceVerificationReport,
    output_path: str | Path,
    *,
    max_marks: int = 10000,
) -> tuple[Path | None, int]:
    """확인된 검색어 좌표를 원본 PDF 사본에 표시한다."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    if source_path.suffix.casefold() != ".pdf" or not source_path.exists() or max_marks <= 0:
        return None, 0

    document = fitz.open(str(source_path))
    count = 0
    seen: dict[int, set[tuple[float, float, float, float]]] = {}
    try:
        ordered_rows = sorted(
            report.rows,
            key=lambda row: (
                row.matched_page if row.matched_page is not None else 10**9,
                row.sheet_key,
                row.excel_row,
            ),
        )
        for row in ordered_rows:
            if row.matched_page is None or not row.matched_terms or count >= max_marks:
                continue
            page_index = row.matched_page - 1
            if not 0 <= page_index < len(document):
                continue
            page = document[page_index]
            for term in row.matched_terms[:3]:
                if count >= max_marks or not 2 <= len(term) <= 80:
                    break
                rectangles = sorted(
                    page.search_for(term),
                    key=lambda rect: (rect.y0, rect.x0, rect.y1, rect.x1),
                )
                for rect in rectangles[:2]:
                    key = tuple(round(value, 1) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
                    if key in seen.setdefault(page_index, set()):
                        continue
                    seen[page_index].add(key)
                    annotation = page.add_rect_annot(rect)
                    annotation.set_colors(stroke=(1.0, 0.0, 0.0))
                    annotation.set_opacity(0.65)
                    annotation.update()
                    count += 1
        if count == 0:
            return None, 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path), garbage=3, deflate=True)
        return output_path, count
    finally:
        document.close()
