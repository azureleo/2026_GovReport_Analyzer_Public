"""
에이전트 3-c: 하이브리드 보조 검수 에이전트

기본 추출본(OpenAI/GPT-Mini 등)을 유지한 채, Gemini 같은 보조 모델로
고위험 시트의 일부 원문 구간만 다시 읽어 누락 후보와 값 충돌 후보를 찾습니다.
검수 결과는 자동 병합하지 않고 별도 Excel 시트에 기록합니다.
"""

# noqa: SIZE_OK — 보조검수 후보 생성·판정 계약을 보존하는 기존 단일 파일.

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import config
from utils import llm_client
from utils.pdf_reader import PageContent
from agents.extractor_agent import _build_page_text, _route_pages_by_sheet
from agents.organizer_agent import OrganizerAgent, _dedup_key_text

logger = logging.getLogger(__name__)


HYBRID_REVIEW_SYSTEM = """당신은 지자체 탄소중립 계획 추출 결과를 검수하는 보조 모델입니다.
반드시 JSON만 반환하세요.
기본 추출본에 바로 병합하지 않고, 원본 문서에서 확인되는 누락 후보와 값 충돌 후보만 제안하세요."""


HYBRID_ADJUDICATION_SYSTEM = """당신은 지자체 탄소중립 계획 추출 결과의 후보 병합 여부를 판정하는 검수자입니다.
반드시 JSON만 반환하세요.
후보가 원문 페이지에서 명확히 확인되는지, 지자체 자체 데이터인지, 기존 기본본에 병합해도 되는지 판정하세요.
원문에 없는 값은 절대 새로 만들지 마세요."""


_REFERENCE_CONTEXT_KEYWORDS = [
    "국가 NDC", "우리나라 NDC", "국가 온실가스 감축목표", "국가 감축목표",
    "주요국", "세계도시", "해외", "국내외", "COP", "IPCC", "EU", "OECD",
    "사례", "동향", "중앙정부",
]


_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


_PROVIDER_ALIASES = {
    "gemini-api": "gemini",
    "openai-api": "openai",
    "gpt": "openai",
    "local": "codex",
    "local-agent": "codex",
    "claude-code": "claude",
}


_TABLE_REF_RE = re.compile(r"(표|그림)\s*([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)\s*[-–]\s*([0-9]+)")


_SHEET_NAME_TO_KEY = {name: key for key, name in config.SHEET_KEY_TO_NAME.items()}


_SIGNATURE_FIELDS = {
    "emissions_management": ["지자체명", "관리부문", "세부부문", "직간접구분", "연도"],
    "emissions_forecast": ["지자체명", "시나리오", "부문", "세부부문", "연도"],
    "reduction_targets": ["지자체명", "목표수준", "목표범위", "부문", "기준연도", "목표연도"],
    "mitigation_projects": ["지자체명", "관리번호", "부문", "사업명"],
    "annual_implementation": ["지자체명", "관리번호", "사업명", "연도"],
    "quantitative_reductions": ["지자체명", "관리번호", "사업명", "연도", "모니터링인자"],
    "financial_plan": ["지자체명", "계획구분", "부문", "사업명", "재원구분", "연도"],
}


def _normalise_provider(provider: str) -> str:
    provider = str(provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(provider, provider)


def _command_available(command: str) -> bool:
    return bool(llm_client._command_exists(command))


def _backend_available(provider: str) -> tuple[bool, str]:
    provider = _normalise_provider(provider)
    if provider == "gemini":
        if getattr(config, "GEMINI_API_KEY", ""):
            return True, ""
        return False, "GEMINI_API_KEY 미설정"
    if provider == "openai":
        if getattr(config, "OPENAI_API_KEY", ""):
            return True, ""
        return False, "OPENAI_API_KEY 미설정"
    if provider == "codex":
        command = getattr(config, "CODEX_COMMAND", "codex")
        if _command_available(command):
            return True, ""
        return False, f"Codex CLI를 찾을 수 없음: CODEX_COMMAND={command}"
    if provider == "claude":
        command = getattr(config, "CLAUDE_COMMAND", "claude")
        if _command_available(command):
            return True, ""
        return False, f"Claude Code CLI를 찾을 수 없음: CLAUDE_COMMAND={command}"
    if provider == "auto":
        for candidate in ("codex", "claude", "gemini", "openai"):
            ok, _ = _backend_available(candidate)
            if ok:
                return True, ""
        return False, "auto 백엔드에서 사용 가능한 provider 없음"
    return False, f"지원하지 않는 보조 검수 백엔드: {provider}"


def _effective_backend_model(provider: str, model: str) -> str:
    provider = _normalise_provider(provider)
    model = str(model or "").strip()
    looks_like_gemini_default = model.startswith("gemini-") or model.startswith("models/gemini")
    if provider == "openai" and looks_like_gemini_default:
        return getattr(config, "OPENAI_MODEL", "")
    if provider in {"codex", "claude", "auto"} and looks_like_gemini_default:
        return getattr(config, "LOCAL_AGENT_MODEL", "")
    return model


def _default_model_for_provider(provider: str) -> str:
    provider = _normalise_provider(provider)
    if provider == "gemini":
        return getattr(config, "MODEL", "")
    if provider == "openai":
        return getattr(config, "OPENAI_MODEL", "")
    if provider in {"codex", "claude", "auto"}:
        return getattr(config, "LOCAL_AGENT_MODEL", "")
    return ""


def _review_stage_provider(default_provider: str) -> str:
    stage_providers = getattr(config, "STAGE_PROVIDERS", {}) or {}
    if isinstance(stage_providers, dict):
        provider = str(stage_providers.get("review", "") or "").strip().lower()
        if provider:
            return provider
    return default_provider


def _review_stage_backend(default_provider: str, default_model: str) -> tuple[str, str]:
    stage_provider = _review_stage_provider(default_provider)
    stage_models = getattr(config, "STAGE_MODELS", {}) or {}
    stage_model = ""
    if isinstance(stage_models, dict):
        stage_model = str(stage_models.get("review", "") or "").strip()
    if stage_model:
        model = stage_model
    elif _normalise_provider(stage_provider) != _normalise_provider(default_provider):
        model = _default_model_for_provider(stage_provider)
    else:
        model = default_model
    return stage_provider, _effective_backend_model(stage_provider, model)


@contextmanager
def _temporary_backend(provider: str, model: str | None = None) -> Iterator[None]:
    saved = {
        "LLM_PROVIDER": getattr(config, "LLM_PROVIDER", ""),
        "MODEL": getattr(config, "MODEL", ""),
        "OPENAI_MODEL": getattr(config, "OPENAI_MODEL", ""),
        "LOCAL_AGENT_MODEL": getattr(config, "LOCAL_AGENT_MODEL", ""),
    }
    normalised_provider = _normalise_provider(provider)
    try:
        config.LLM_PROVIDER = normalised_provider
        if model:
            if normalised_provider == "gemini":
                config.MODEL = model
            elif normalised_provider == "openai":
                config.OPENAI_MODEL = model
            else:
                config.LOCAL_AGENT_MODEL = model
        yield
    finally:
        for name, value in saved.items():
            setattr(config, name, value)


def _compact(text: str, limit: int = 22000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _sample_evenly(rows: list[dict], limit: int) -> list[dict]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[0]]
    indexes = sorted({round(i * (len(rows) - 1) / (limit - 1)) for i in range(limit)})
    return [rows[i] for i in indexes]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def _select_evenly(chunks: list[list[PageContent]], limit: int) -> list[list[PageContent]]:
    if limit <= 0 or len(chunks) <= limit:
        return chunks
    if limit == 1:
        return [chunks[0]]
    indexes = sorted({round(i * (len(chunks) - 1) / (limit - 1)) for i in range(limit)})
    return [chunks[i] for i in indexes]


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _parse_json_cell(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = llm_client.parse_json(value)
    return parsed if isinstance(parsed, dict) else {}


def _sheet_key_from_name(sheet_name: str) -> str | None:
    if sheet_name in config.SHEET_KEY_TO_NAME:
        return sheet_name
    if sheet_name in _SHEET_NAME_TO_KEY:
        return _SHEET_NAME_TO_KEY[sheet_name]
    normalized = re.sub(r"^\d+_", "", sheet_name or "")
    for name, key in _SHEET_NAME_TO_KEY.items():
        if normalized and normalized in name:
            return key
    return None


def _extract_page_numbers(page_ref: str) -> list[int]:
    text = page_ref or ""
    numbers: set[int] = set()
    for start, end in re.findall(r"p?\s*(\d+)\s*[~\-]\s*p?\s*(\d+)", text, flags=re.IGNORECASE):
        a, b = int(start), int(end)
        if a > b:
            a, b = b, a
        for page_num in range(a, min(b, a + 14) + 1):
            numbers.add(page_num)
    for single in re.findall(r"p\s*\.?\s*(\d+)|페이지\s*(\d+)", text, flags=re.IGNORECASE):
        raw = single[0] or single[1]
        if raw:
            numbers.add(int(raw))
    if not numbers:
        for raw in re.findall(r"\b\d{1,4}\b", text):
            value = int(raw)
            if 1 <= value <= 2000:
                numbers.add(value)
    return sorted(numbers)


def _normalize_table_ref(ref: str) -> str:
    match = _TABLE_REF_RE.search(ref or "")
    if not match:
        return ""
    kind, chapter, number = match.groups()
    return f"{kind} {chapter}-{number}"


def _extract_table_refs(text: str) -> list[str]:
    refs = []
    seen = set()
    for match in _TABLE_REF_RE.finditer(text or ""):
        ref = _normalize_table_ref(match.group(0))
        if ref and ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def _extract_table_title_hints(text: str, refs: list[str]) -> list[str]:
    hints: list[str] = []
    source = text or ""
    for ref in refs:
        escaped = re.escape(ref).replace("\\ ", r"\s*")
        patterns = [
            rf"{escaped}\s*['\"‘’“”「『]([^'\"‘’“”」』\n]{{4,80}})['\"‘’“”」』]",
            rf"{escaped}\s*[:\]\)]?\s*([^\n]{{4,80}})",
        ]
        for pattern in patterns:
            match = re.search(pattern, source)
            if not match:
                continue
            title = re.sub(r"\s+", " ", match.group(1)).strip(" -:[]()")
            if title and title not in hints:
                hints.append(title)
    return hints[:3]


def _looks_like_toc_page(page: PageContent) -> bool:
    text = re.sub(r"\s+", " ", page.text or "")
    head = text[:1200]
    if any(marker in head for marker in ("목차", "표 목차", "그림 목차", "Contents")):
        return True
    table_ref_count = len(_TABLE_REF_RE.findall(text))
    return page.page_number <= 20 and table_ref_count >= 8


def _build_table_page_index(pages: list[PageContent]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for page in pages:
        combined = "\n".join([page.text or "", *(page.tables or [])])
        for ref in _extract_table_refs(combined):
            index.setdefault(ref, []).append(page.page_number)
    return index


def _score_table_page(
    *,
    page: PageContent,
    ref: str,
    title_hints: list[str],
    original_pages: set[int],
) -> int:
    combined = re.sub(r"\s+", " ", "\n".join([page.text or "", *(page.tables or [])]))
    score = 0
    if ref in combined:
        score += 10
    if not _looks_like_toc_page(page):
        score += 20
    else:
        score -= 30
    if page.page_number in original_pages:
        score += 2
    for title in title_hints:
        title_tokens = [token for token in re.split(r"\s+", title) if len(token) >= 2]
        matched = sum(1 for token in title_tokens if token in combined)
        score += min(matched, 6)
    # 표 본문은 보통 행/열 수치가 함께 있어 목차보다 숫자 밀도가 높다.
    if len(re.findall(r"\d", combined)) >= 40:
        score += 3
    return score


def _correct_candidate_page_ref(
    *,
    candidate: dict,
    pages_by_num: dict[int, PageContent],
    table_index: dict[str, list[int]],
) -> tuple[str, str]:
    original_ref = candidate.get("근거페이지", "") or ""
    evidence_text = " ".join([
        original_ref,
        candidate.get("검수사유", "") or "",
        candidate.get("후보행JSON", "") or "",
    ])
    refs = _extract_table_refs(evidence_text)
    if not refs:
        return original_ref, ""

    original_pages = set(_extract_page_numbers(original_ref))
    title_hints = _extract_table_title_hints(evidence_text, refs)
    best_page: int | None = None
    best_ref = ""
    best_score = -10**9
    for ref in refs:
        for page_num in table_index.get(ref, []):
            page = pages_by_num.get(page_num)
            if not page:
                continue
            score = _score_table_page(
                page=page,
                ref=ref,
                title_hints=title_hints,
                original_pages=original_pages,
            )
            if score > best_score:
                best_score = score
                best_page = page_num
                best_ref = ref

    if best_page is None:
        return original_ref, ""
    corrected = f"p{best_page}"
    if corrected == original_ref:
        return original_ref, ""
    original_page_text = ", ".join(f"p{num}" for num in sorted(original_pages)) or original_ref
    return corrected, f"근거페이지보정: {original_page_text} -> {corrected} ({best_ref})"


def _page_context(pages_by_num: dict[int, PageContent], page_ref: str, limit: int = 28000) -> str:
    page_numbers = _extract_page_numbers(page_ref)
    if not page_numbers:
        return ""
    parts = []
    for page_num in page_numbers:
        page = pages_by_num.get(page_num)
        if not page:
            continue
        table_text = "\n".join(page.tables or [])
        parts.append(f"[p{page.page_number}]\n{page.text}\n{table_text}")
    return _compact("\n\n".join(parts), limit)


def _reference_flags(text: str) -> list[str]:
    flags = []
    for keyword in _REFERENCE_CONTEXT_KEYWORDS:
        if keyword and keyword.casefold() in (text or "").casefold():
            flags.append(keyword)
    return flags


def _as_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value.strip() if isinstance(value, str) else value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _clean_candidate_row(candidate_row: dict, sheet_key: str, municipality: str) -> dict | None:
    sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key)
    if not sheet_name:
        return None
    headers = config.EXCEL_HEADERS.get(sheet_name, [])
    if not headers or not isinstance(candidate_row, dict):
        return None
    cleaned = {header: _as_scalar(candidate_row.get(header)) for header in headers if header in candidate_row}
    if "지자체명" in headers:
        cleaned["지자체명"] = cleaned.get("지자체명") or municipality
    meaningful = [
        value for key, value in cleaned.items()
        if key != "지자체명" and value not in (None, "")
    ]
    if not meaningful:
        return None
    rows = OrganizerAgent().organize_sheet(sheet_key, [cleaned], municipality)
    return rows[0] if rows else None


def _row_signature(row: dict, sheet_key: str) -> tuple:
    fields = _SIGNATURE_FIELDS.get(sheet_key)
    if not fields:
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, "")
        fields = config.EXCEL_HEADERS.get(sheet_name, [])[:5]
    return tuple(_dedup_key_text(row.get(field)) for field in fields)


def _is_missing_candidate(candidate: dict) -> bool:
    return "누락" in str(candidate.get("후보유형", "") or "")


def _candidate_duplicates_existing(candidate: dict, final_data: dict) -> bool:
    if not _is_missing_candidate(candidate):
        return False
    sheet_key = _sheet_key_from_name(candidate.get("대상시트", ""))
    if not sheet_key:
        return False
    municipality = final_data.get("municipality_name", "알 수 없음")
    candidate_row = _clean_candidate_row(_parse_json_cell(candidate.get("후보행JSON")), sheet_key, municipality)
    if not candidate_row:
        return False
    existing_rows = final_data.get(sheet_key, []) if isinstance(final_data.get(sheet_key), list) else []
    signatures = {_row_signature(row, sheet_key) for row in existing_rows if isinstance(row, dict)}
    return _row_signature(candidate_row, sheet_key) in signatures


def _row_contains_log_values(existing: dict, normalized_row: dict, sheet_key: str) -> bool:
    if _row_signature(existing, sheet_key) != _row_signature(normalized_row, sheet_key):
        return False
    for key, value in normalized_row.items():
        if value in (None, ""):
            continue
        if _dedup_key_text(existing.get(key)) != _dedup_key_text(value):
            return False
    return True


def reconcile_reflected_merge_log(final_data: dict, merge_log: list[dict]) -> list[dict]:
    for row in merge_log:
        if not isinstance(row, dict) or row.get("최종반영여부") != "반영":
            continue
        sheet_key = _sheet_key_from_name(row.get("대상시트", ""))
        normalized_row = _parse_json_cell(row.get("정규화행JSON"))
        if not sheet_key or not normalized_row:
            continue
        existing_rows = final_data.get(sheet_key, []) if isinstance(final_data.get(sheet_key), list) else []
        if any(
            _row_contains_log_values(item, normalized_row, sheet_key)
            for item in existing_rows
            if isinstance(item, dict)
        ):
            continue
        row["최종반영여부"] = "보류(병합후중복제거)"
        blocker = str(row.get("병합차단사유", "") or "").strip()
        row["병합차단사유"] = ", ".join(part for part in [blocker, "병합후중복제거"] if part)
    return merge_log


def _confidence_at_least(value: str, minimum: str) -> bool:
    return _CONFIDENCE_RANK.get((value or "").strip().lower(), 0) >= _CONFIDENCE_RANK.get(minimum, 3)


def _default_adjudication(reason: str, flag: str = "판정실패") -> dict:
    return {
        "decision": "needs_human",
        "confidence": "low",
        "reason": reason,
        "risk_flags": [flag],
    }


def _risk_flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,|/]", value) if item.strip()]
    return []


def _sheet_key_from_issue_area(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    direct = _sheet_key_from_name(text)
    if direct:
        return direct
    for sheet_key, sheet_name in config.SHEET_KEY_TO_NAME.items():
        label = re.sub(r"^\d+_", "", sheet_name)
        if text == sheet_key or text in sheet_name or label in text or text in label:
            return sheet_key
    return None


def _issue_row_index(issue: dict) -> int | None:
    direct = issue.get("대상행번호")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return None
    text = " ".join(str(issue.get(field, "") or "") for field in ("항목", "문제내용"))
    match = re.search(r"행\s*(\d+)", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _target_pages_from_row(row: dict) -> list[int]:
    return _extract_page_numbers(str(row.get("출처페이지", "") or ""))


def _target_key(sheet_key: str, row_index: int, reason: str) -> tuple[str, int, str]:
    return sheet_key, row_index, reason[:120]


def _is_ledger_failure_issue(issue: dict) -> bool:
    item = str(issue.get("항목", "") or "")
    return "원장 호출실패" in item or "원장 파싱실패" in item


def select_targeted_review_rows(final_data: dict) -> list[dict]:
    """검증리포트 경고와 conflicting 행에서 타깃 검수 대상을 결정론적으로 고른다."""
    targets: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    max_rows = max(1, int(getattr(config, "HYBRID_REVIEW_TARGET_MAX_ROWS", 50)))

    def add_target(sheet_key: str, row_index: int, row: dict, reason: str, issue: dict | None = None) -> None:
        if len(targets) >= max_rows:
            return
        key = _target_key(sheet_key, row_index, reason)
        if key in seen:
            return
        seen.add(key)
        targets.append({
            "sheet_key": sheet_key,
            "sheet_name": config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key),
            "row_index": row_index,
            "row": dict(row),
            "reason": reason,
            "issue": dict(issue or {}),
            "pages": _target_pages_from_row(row),
        })

    def add_failed_page_targets() -> None:
        if not getattr(config, "HYBRID_REVIEW_INCLUDE_FAILED_PAGES", False):
            return
        for issue in final_data.get("validation_report", []):
            if not isinstance(issue, dict):
                continue
            if not _is_ledger_failure_issue(issue):
                continue
            sheet_key = _sheet_key_from_issue_area(issue.get("영역"))
            if not sheet_key:
                continue
            failed_pages = set(_extract_page_numbers(str(issue.get("문제내용", "") or "")))
            if not failed_pages:
                continue
            rows = final_data.get(sheet_key, [])
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    continue
                if failed_pages & set(_target_pages_from_row(row)):
                    add_target(sheet_key, index, row, "원장 실패 페이지 인접 행", issue)

    for issue in final_data.get("validation_report", []):
        if not isinstance(issue, dict) or issue.get("심각도") != "경고":
            continue
        if _is_ledger_failure_issue(issue):
            continue
        sheet_key = str(issue.get("대상시트키") or "").strip()
        if sheet_key not in config.SHEET_KEY_TO_NAME:
            sheet_key = _sheet_key_from_issue_area(issue.get("영역")) or _sheet_key_from_issue_area(issue.get("항목"))
        if not sheet_key:
            continue
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list) or not rows:
            continue
        row_index = _issue_row_index(issue)
        if row_index is None and len(rows) == 1:
            row_index = 1
        if row_index is None or row_index < 1 or row_index > len(rows):
            continue
        row = rows[row_index - 1]
        if isinstance(row, dict):
            add_target(sheet_key, row_index, row, issue.get("문제내용") or issue.get("항목") or "검증리포트 경고", issue)

    add_failed_page_targets()

    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows, start=1):
            if isinstance(row, dict) and row.get("데이터상태") == "conflicting":
                add_target(sheet_key, index, row, "데이터상태=conflicting", None)

    return targets


class HybridReviewAgent:
    """Gemini 등 보조 모델을 이용한 타깃 검수."""

    def __init__(self):
        self._candidates: list[dict] = []
        self._stats: dict[str, Any] = {
            "enabled": bool(getattr(config, "HYBRID_REVIEW_ENABLED", False)),
            "provider": getattr(config, "HYBRID_REVIEW_PROVIDER", "gemini"),
            "model": getattr(config, "HYBRID_REVIEW_MODEL", ""),
            "adjudication_provider": getattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini"),
            "adjudication_model": getattr(config, "HYBRID_ADJUDICATION_MODEL", ""),
            "sheets": {},
            "adjudication": {
                "checked": 0,
                "accepted": 0,
                "fixed": 0,
                "merged": 0,
                "rejected": 0,
                "needs_human": 0,
                "page_corrected": 0,
            },
            "skipped": "",
        }

    def _provider_available(self) -> bool:
        provider = _review_stage_provider(getattr(config, "HYBRID_REVIEW_PROVIDER", "gemini"))
        ok, message = _backend_available(provider)
        if not ok:
            self._stats["skipped"] = message
        return ok

    def _base_rows_text(self, final_data: dict, sheet_key: str) -> str:
        rows = final_data.get(sheet_key, [])
        if not isinstance(rows, list):
            rows = []
        sample = _sample_evenly([row for row in rows if isinstance(row, dict)], config.HYBRID_REVIEW_BASE_ROWS_PER_SHEET)
        return json.dumps(sample, ensure_ascii=False)

    def _prompt_for_batch(
        self,
        *,
        municipality: str,
        sheet_key: str,
        batch: list[PageContent],
        base_rows_json: str,
        guideline_prompt: str,
    ) -> str:
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
        page_nums = [page.page_number for page in batch]
        page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
        guideline_block = f"\n[가이드라인 보조 지침]\n{_compact(guideline_prompt, 4000)}\n" if guideline_prompt else ""
        return f"""지자체명: {municipality}
검수 대상 시트: {sheet_name} ({sheet_key})
검수 페이지: {page_range}
{guideline_block}
[기본 추출본 일부]
{_compact(base_rows_json, 16000)}

[원문]
{_compact(_build_page_text(batch), 24000)}

아래 JSON 형식으로만 반환하세요:
{{
  "hybrid_review_candidates": [
    {{
      "대상시트": "{sheet_name}",
      "후보유형": "누락후보|값충돌|기본본오염의심",
      "신뢰도": "low|medium|high",
      "근거페이지": "{page_range}",
      "candidate_row": {{}},
      "base_similar_row": null,
      "reason": "검수 사유",
      "merge_recommendation": "검토후병합|병합보류"
    }}
  ]
}}

판정 규칙:
- 기본 추출본에 이미 같은 행이 있으면 반환하지 마세요.
- 원문 수치와 기본 추출본 수치가 같은 식별자에서 다를 때만 후보유형을 "값충돌"로 반환하세요.
- 기본 추출본에 국가/해외/참고자료성 값이 지자체 자체 값처럼 들어간 것으로 보이면 "기본본오염의심"으로 반환하세요.
- 다음 문맥은 지자체 자체 데이터로 병합하지 마세요: {", ".join(_REFERENCE_CONTEXT_KEYWORDS)}
- 원문 페이지와 표/문장 근거가 명확하지 않으면 반환하지 마세요.
- 후보는 많아도 가장 중요한 것만 10개 이하로 제한하세요.
- 반환한 후보는 자동 병합되지 않고 사람이 검토할 후보입니다."""

    def _review_batch(
        self,
        *,
        municipality: str,
        sheet_key: str,
        batch: list[PageContent],
        final_data: dict,
        extraction_prompts: dict[str, str],
    ) -> list[dict]:
        base_rows_json = self._base_rows_text(final_data, sheet_key)
        prompt = self._prompt_for_batch(
            municipality=municipality,
            sheet_key=sheet_key,
            batch=batch,
            base_rows_json=base_rows_json,
            guideline_prompt=extraction_prompts.get(sheet_key, ""),
        )
        provider, model = _review_stage_backend(
            getattr(config, "HYBRID_REVIEW_PROVIDER", "gemini"),
            getattr(config, "HYBRID_REVIEW_MODEL", ""),
        )
        try:
            with _temporary_backend(provider, model or None):
                resp = llm_client.call_text(prompt, system=HYBRID_REVIEW_SYSTEM, stage="review")
        except llm_client.LLMQuotaExceededError as exc:
            self._stats["skipped"] = f"quota/한도 초과: {exc}"
            raise
        except (llm_client.LLMCallError, RuntimeError) as exc:
            logger.warning("하이브리드 검수 호출 실패(%s): %s", sheet_key, exc)
            return []

        parsed = llm_client.parse_json(resp)
        if not isinstance(parsed, dict):
            return []
        rows = parsed.get("hybrid_review_candidates", [])
        return rows if isinstance(rows, list) else []

    def _target_page_context(self, target: dict, pages_by_num: dict[int, PageContent], limit: int = 12000) -> str:
        page_nums = set(target.get("pages") or [])
        expanded = sorted({num + delta for num in page_nums for delta in (-1, 0, 1) if num + delta in pages_by_num})
        if not expanded:
            return ""
        return _compact(_build_page_text([pages_by_num[num] for num in expanded]), limit)

    def _targeted_prompt(self, *, municipality: str, target: dict, page_context: str) -> str:
        return f"""지자체명: {municipality}
대상시트: {target['sheet_name']} ({target['sheet_key']})
대상행번호: {target['row_index']}
검수사유: {target['reason']}

[현재 행]
{json.dumps(target['row'], ensure_ascii=False)}

[검증리포트 항목]
{json.dumps(target.get('issue', {}), ensure_ascii=False)}

[원문 발췌]
{page_context}

기존 계획 평가 장에 실린 과제번호·사업목록은 이전 계획의 것으로, 본계획 관리번호와 다른 사업을 가리킬 수 있습니다.
본계획 사업 식별 기준은 본계획의 사업목록 표와 사업카드입니다.

아래 JSON 형식으로만 반환하세요:
{{
  "hybrid_review_candidates": [
    {{
      "candidate_type": "유지|수정안|판단불가",
      "confidence": "low|medium|high",
      "candidate_row": {{}},
      "base_similar_row": {{}},
      "reason": "원문 근거 기반 판단 사유",
      "merge_recommendation": "자동병합금지"
    }}
  ]
}}

규칙:
- 원문 발췌에서 직접 확인되지 않는 값은 만들지 마세요.
- 자동 병합을 전제로 답하지 말고, 후보 시트에 남길 검수 결과만 제안하세요.
- 판단이 어려우면 candidate_type은 판단불가로 두고 candidate_row는 현재 행을 그대로 반환하세요."""

    def _review_target(self, *, municipality: str, target: dict, pages_by_num: dict[int, PageContent]) -> list[dict]:
        page_context = self._target_page_context(target, pages_by_num)
        prompt = self._targeted_prompt(municipality=municipality, target=target, page_context=page_context)
        provider, model = _review_stage_backend(
            getattr(config, "HYBRID_REVIEW_PROVIDER", "gemini"),
            getattr(config, "HYBRID_REVIEW_MODEL", ""),
        )
        try:
            with _temporary_backend(provider, model or None):
                parsed, parse_ok = llm_client.call_text_json(prompt, system=HYBRID_REVIEW_SYSTEM, stage="review")
        except llm_client.LLMQuotaExceededError:
            raise
        except (llm_client.LLMCallError, RuntimeError) as exc:
            logger.warning("타깃 보조검수 호출 실패(%s 행 %s): %s", target["sheet_key"], target["row_index"], exc)
            return []
        if not parse_ok or not isinstance(parsed, dict):
            return []
        candidates = parsed.get("hybrid_review_candidates", [])
        return candidates if isinstance(candidates, list) else []

    def review_targeted(
        self,
        *,
        pages: list[PageContent],
        final_data: dict,
        extraction_prompts: dict[str, str] | None = None,
    ) -> list[dict]:
        if not getattr(config, "HYBRID_REVIEW_ENABLED", False):
            self._stats["skipped"] = "비활성"
            return []
        if not self._provider_available():
            return []
        municipality = final_data.get("municipality_name", "알 수 없음")
        pages_by_num = {page.page_number: page for page in pages}
        targets = select_targeted_review_rows(final_data)
        self._stats["targeted"] = {"targets": len(targets), "candidates": 0}
        seen: set[tuple[str, str, str]] = set()
        for target in targets:
            page_range = ",".join(f"p{num}" for num in target.get("pages", [])) or "p?"
            for item in self._review_target(municipality=municipality, target=target, pages_by_num=pages_by_num):
                if not isinstance(item, dict):
                    continue
                row = self._normalize_candidate(item, municipality, target["sheet_key"], page_range)
                row["검수상태"] = "타깃검수"
                if not row.get("검수사유"):
                    row["검수사유"] = target["reason"]
                if _candidate_duplicates_existing(row, final_data):
                    continue
                dedup_key = (row["대상시트"], row["후보유형"], row["후보행JSON"])
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                self._candidates.append(row)
        self._stats["targeted"]["candidates"] = len(self._candidates)
        return self._candidates

    def _normalize_candidate(self, item: dict, municipality: str, sheet_key: str, page_range: str) -> dict:
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
        candidate_row = item.get("candidate_row") or item.get("후보행") or {}
        base_row = item.get("base_similar_row") or item.get("기본본유사행")
        return {
            "지자체명": municipality,
            "대상시트": item.get("대상시트") or sheet_name,
            "후보유형": item.get("후보유형") or item.get("candidate_type") or "누락후보",
            "신뢰도": item.get("신뢰도") or item.get("confidence") or "medium",
            "근거페이지": item.get("근거페이지") or item.get("page_range") or page_range,
            "후보행JSON": _json_cell(candidate_row),
            "기본본유사행JSON": _json_cell(base_row),
            "검수사유": item.get("reason") or item.get("검수사유") or "",
            "병합권장": item.get("merge_recommendation") or item.get("병합권장") or "검토후병합",
            "검수상태": "미검수",
        }

    def review(
        self,
        *,
        pages: list[PageContent],
        final_data: dict,
        extraction_prompts: dict[str, str],
    ) -> list[dict]:
        if not getattr(config, "HYBRID_REVIEW_ENABLED", False):
            self._stats["skipped"] = "비활성"
            return []
        if getattr(config, "HYBRID_REVIEW_TARGETED", True):
            return self.review_targeted(pages=pages, final_data=final_data, extraction_prompts=extraction_prompts)
        if not self._provider_available():
            return []

        municipality = final_data.get("municipality_name", "알 수 없음")
        routed = _route_pages_by_sheet(pages)
        target_sheets = [
            sheet for sheet in getattr(config, "HYBRID_REVIEW_SHEETS", [])
            if sheet in config.SHEET_KEY_TO_NAME
        ]
        batch_size = getattr(config, "HYBRID_REVIEW_BATCH_SIZE", 8)
        max_batches = getattr(config, "HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 2)

        seen: set[tuple[str, str, str]] = set()
        for sheet_key in target_sheets:
            sheet_pages = routed.get(sheet_key, [])
            if not sheet_pages:
                self._stats["sheets"][sheet_key] = {"batches": 0, "candidates": 0}
                continue

            batches = _select_evenly(_chunks(sheet_pages, batch_size), max_batches)
            sheet_count = 0
            for batch in batches:
                page_nums = [page.page_number for page in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                for item in self._review_batch(
                    municipality=municipality,
                    sheet_key=sheet_key,
                    batch=batch,
                    final_data=final_data,
                    extraction_prompts=extraction_prompts,
                ):
                    if not isinstance(item, dict):
                        continue
                    row = self._normalize_candidate(item, municipality, sheet_key, page_range)
                    if _candidate_duplicates_existing(row, final_data):
                        continue
                    dedup_key = (row["대상시트"], row["후보유형"], row["후보행JSON"])
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    self._candidates.append(row)
                    sheet_count += 1
            self._stats["sheets"][sheet_key] = {"batches": len(batches), "candidates": sheet_count}

        return self._candidates

    def _adjudication_provider_available(self) -> bool:
        provider = _review_stage_provider(getattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini"))
        ok, message = _backend_available(provider)
        if not ok:
            self._stats["adjudication"]["skipped"] = message
        return ok

    def _adjudication_prompt(
        self,
        *,
        municipality: str,
        candidate: dict,
        sheet_key: str,
        page_context: str,
        existing_rows: list[dict],
    ) -> str:
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
        candidate_row = _parse_json_cell(candidate.get("후보행JSON"))
        similar_row = _parse_json_cell(candidate.get("기본본유사행JSON"))
        sampled_existing = _sample_evenly([row for row in existing_rows if isinstance(row, dict)], 30)
        return f"""지자체명: {municipality}
판정 대상 시트: {sheet_name} ({sheet_key})
후보유형: {candidate.get("후보유형", "")}
근거페이지: {candidate.get("근거페이지", "")}
후보 사유: {candidate.get("검수사유", "")}

[후보 행]
{json.dumps(candidate_row, ensure_ascii=False)}

[기본본 유사 행]
{json.dumps(similar_row, ensure_ascii=False)}

[기본본 일부]
{json.dumps(sampled_existing, ensure_ascii=False)}

[원문 문맥]
{page_context}

아래 JSON 형식으로만 반환하세요:
{{
  "decision": "accept|fix_then_merge|reject|needs_human",
  "confidence": "low|medium|high",
  "evidence_page": "p123",
  "evidence_text": "원문 근거 문구를 짧게 인용 또는 요약",
  "normalized_row": {{}},
  "reason": "판정 사유",
  "risk_flags": ["국가자료혼입|해외사례|참고자료|단위불명확|중복의심|근거부족"]
}}

판정 기준:
- accept: 후보 행의 핵심 값이 원문 문맥에서 직접 확인되고, 지자체 자체 데이터이며, 기본본에 없는 누락 항목일 때만 선택하세요.
- fix_then_merge: 후보의 핵심 값은 맞지만 시트명, 부문명, 직간접구분, 단위 표기처럼 정규화가 필요한 경우 선택하세요. 이때 normalized_row에 수정된 행을 넣으세요.
- reject: 원문 근거가 없거나, 국가/해외/참고자료/목차/일반 설명을 지자체 데이터로 오해한 후보라면 선택하세요.
- needs_human: 원문 근거는 있으나 단위, 부문, 연도, 사업명 매칭이 모호하면 선택하세요.
- normalized_row는 {sheet_name} 시트 헤더에 맞춘 최종 후보 행입니다. 원문에 없는 값은 채우지 마세요.
- 기본본오염의심 후보는 자동 병합 대상이 아니므로 accept보다 needs_human/reject를 우선 고려하세요.
- evidence_text는 반드시 원문 문맥에서 확인 가능한 근거여야 합니다."""

    def _adjudication_batch_prompt(
        self,
        *,
        municipality: str,
        sheet_key: str,
        prepared_candidates: list[dict],
        final_data: dict,
    ) -> str:
        sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
        existing_rows = final_data.get(sheet_key, []) if isinstance(final_data.get(sheet_key), list) else []
        sampled_existing = _sample_evenly([row for row in existing_rows if isinstance(row, dict)], 30)
        candidate_payload = []
        for prepared in prepared_candidates:
            candidate = prepared["candidate_for_judgment"]
            candidate_payload.append({
                "candidate_id": prepared["candidate_id"],
                "후보유형": candidate.get("후보유형", ""),
                "근거페이지": candidate.get("근거페이지", ""),
                "후보사유": candidate.get("검수사유", ""),
                "후보행": _parse_json_cell(candidate.get("후보행JSON")),
                "기본본유사행": _parse_json_cell(candidate.get("기본본유사행JSON")),
                "원문문맥": prepared.get("page_context", ""),
            })

        return f"""지자체명: {municipality}
판정 대상 시트: {sheet_name} ({sheet_key})

[기본본 일부]
{json.dumps(sampled_existing, ensure_ascii=False)}

[판정 후보 목록]
{json.dumps(candidate_payload, ensure_ascii=False)}

아래 JSON 형식으로만 반환하세요:
{{
  "adjudications": [
    {{
      "candidate_id": "후보목록의 candidate_id 값을 그대로 복사",
      "decision": "accept|fix_then_merge|reject|needs_human",
      "confidence": "low|medium|high",
      "evidence_page": "p123",
      "evidence_text": "원문 근거 문구를 짧게 인용 또는 요약",
      "normalized_row": {{}},
      "reason": "판정 사유",
      "risk_flags": ["국가자료혼입", "해외사례", "참고자료", "단위불명확", "중복의심", "근거부족"]
    }}
  ]
}}

판정 기준:
- 모든 candidate_id를 빠짐없이 한 번씩 판정하고, candidate_id는 [판정 후보 목록]의 값을 그대로 복사하세요.
- accept: 후보 행의 핵심 값이 원문 문맥에서 직접 확인되고, 지자체 자체 데이터이며, 기본본에 없는 누락 항목일 때만 선택하세요.
- fix_then_merge: 후보의 핵심 값은 맞지만 시트명, 부문명, 직간접구분, 단위 표기처럼 정규화가 필요한 경우 선택하세요. 이때 normalized_row에 수정된 행을 넣으세요.
- reject: 원문 근거가 없거나, 국가/해외/참고자료/목차/일반 설명을 지자체 데이터로 오해한 후보라면 선택하세요.
- needs_human: 원문 근거는 있으나 단위, 부문, 연도, 사업명 매칭이 모호하면 선택하세요.
- normalized_row는 {sheet_name} 시트 헤더에 맞춘 최종 후보 행입니다. 원문에 없는 값은 채우지 마세요.
- 기본본오염의심 후보는 자동 병합 대상이 아니므로 accept보다 needs_human/reject를 우선 고려하세요.
- evidence_text는 반드시 각 후보의 원문 문맥에서 확인 가능한 근거여야 합니다."""

    def _normalise_batch_adjudications(self, parsed: Any, candidate_ids: list[str]) -> dict[str, dict]:
        if isinstance(parsed, dict):
            rows = parsed.get("adjudications") or parsed.get("results") or parsed.get("decisions") or []
        elif isinstance(parsed, list):
            rows = parsed
        else:
            rows = []

        if not isinstance(rows, list):
            rows = []
        position_fallback = len(rows) == len(candidate_ids) and all(
            isinstance(item, dict)
            and not (item.get("candidate_id") or item.get("후보ID") or item.get("id"))
            for item in rows
        )
        result: dict[str, dict] = {}
        for idx, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            raw_candidate_id = item.get("candidate_id") or item.get("후보ID") or item.get("id")
            if raw_candidate_id:
                candidate_id = str(raw_candidate_id)
                if candidate_id in candidate_ids:
                    result[candidate_id] = item
                continue
            if position_fallback and idx < len(candidate_ids):
                result[candidate_ids[idx]] = item
        return result

    def _prepare_candidate_for_adjudication(
        self,
        *,
        candidate: dict,
        candidate_id: str,
        pages_by_num: dict[int, PageContent],
        table_index: dict[str, list[int]],
    ) -> dict | None:
        sheet_key = _sheet_key_from_name(candidate.get("대상시트", ""))
        if not sheet_key or sheet_key not in config.SHEET_KEY_TO_NAME:
            return None
        corrected_page_ref, correction_note = _correct_candidate_page_ref(
            candidate=candidate,
            pages_by_num=pages_by_num,
            table_index=table_index,
        )
        if correction_note:
            self._stats["adjudication"]["page_corrected"] += 1
        candidate_for_judgment = dict(candidate)
        if corrected_page_ref:
            candidate_for_judgment["근거페이지"] = corrected_page_ref
        if correction_note:
            candidate_for_judgment["검수사유"] = (
                f"{candidate_for_judgment.get('검수사유', '')}\n{correction_note}"
            ).strip()
        context_limit = int(getattr(config, "HYBRID_ADJUDICATION_CONTEXT_CHARS", 8000))
        page_context = _page_context(
            pages_by_num,
            corrected_page_ref or candidate.get("근거페이지", ""),
            limit=context_limit,
        )
        return {
            "candidate_id": candidate_id,
            "sheet_key": sheet_key,
            "candidate": candidate,
            "candidate_for_judgment": candidate_for_judgment,
            "corrected_page_ref": corrected_page_ref,
            "correction_note": correction_note,
            "page_context": page_context,
            "raw_candidate_row": _parse_json_cell(candidate.get("후보행JSON")),
        }

    def _record_adjudication_stats(self, adjudication: dict, reflected: str) -> None:
        decision = adjudication.get("decision", "needs_human")
        if decision == "accept":
            self._stats["adjudication"]["accepted"] += 1
        elif decision == "fix_then_merge":
            self._stats["adjudication"]["fixed"] += 1
        elif decision == "reject":
            self._stats["adjudication"]["rejected"] += 1
        elif decision == "needs_human":
            self._stats["adjudication"]["needs_human"] += 1
        self._stats["adjudication"]["checked"] += 1
        if reflected == "반영":
            self._stats["adjudication"]["merged"] += 1

    def _apply_adjudication(
        self,
        *,
        municipality: str,
        prepared: dict,
        adjudication: dict,
        final_data: dict,
    ) -> dict:
        candidate = prepared["candidate"]
        sheet_key = prepared["sheet_key"]
        normalized_source = adjudication.get("normalized_row")
        if not isinstance(normalized_source, dict) or not normalized_source:
            normalized_source = prepared.get("raw_candidate_row", {})
        normalized_row = _clean_candidate_row(normalized_source, sheet_key, municipality)
        blockers = self._merge_blockers(
            candidate=candidate,
            adjudication=adjudication,
            sheet_key=sheet_key,
            normalized_row=normalized_row,
            page_context=prepared.get("page_context", ""),
            final_data=final_data,
        )

        reflected = "보류"
        if not blockers and normalized_row:
            final_data.setdefault(sheet_key, []).append(normalized_row)
            reflected = "반영"

        self._record_adjudication_stats(adjudication, reflected)

        reason = adjudication.get("reason", "")
        correction_note = prepared.get("correction_note", "")
        if correction_note:
            reason = f"{reason} / {correction_note}".strip(" /")

        return {
            "지자체명": municipality,
            "대상시트": config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key),
            "판정": adjudication.get("decision", "needs_human"),
            "신뢰도": adjudication.get("confidence", ""),
            "최종반영여부": reflected,
            "근거페이지": (
                adjudication.get("evidence_page")
                or prepared.get("corrected_page_ref")
                or candidate.get("근거페이지", "")
            ),
            "근거문구": adjudication.get("evidence_text", ""),
            "위험플래그": ", ".join(_risk_flags(adjudication.get("risk_flags"))),
            "후보행JSON": candidate.get("후보행JSON", ""),
            "정규화행JSON": _json_cell(normalized_row),
            "판정사유": reason,
            "병합차단사유": ", ".join(blockers),
        }

    def _adjudicate_candidate(
        self,
        *,
        municipality: str,
        candidate: dict,
        sheet_key: str,
        page_context: str,
        final_data: dict,
    ) -> dict:
        prompt = self._adjudication_prompt(
            municipality=municipality,
            candidate=candidate,
            sheet_key=sheet_key,
            page_context=page_context,
            existing_rows=final_data.get(sheet_key, []) if isinstance(final_data.get(sheet_key), list) else [],
        )
        provider, model = _review_stage_backend(
            getattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini"),
            getattr(config, "HYBRID_ADJUDICATION_MODEL", ""),
        )
        try:
            with _temporary_backend(provider, model or None):
                resp = llm_client.call_text(prompt, system=HYBRID_ADJUDICATION_SYSTEM, stage="review")
        except llm_client.LLMQuotaExceededError:
            raise
        except (llm_client.LLMCallError, RuntimeError) as exc:
            logger.warning("하이브리드 후보 판정 실패(%s): %s", sheet_key, exc)
            return {
                "decision": "needs_human",
                "confidence": "low",
                "reason": f"판정 호출 실패: {exc}",
                "risk_flags": ["판정호출실패"],
            }
        parsed = llm_client.parse_json(resp)
        return parsed if isinstance(parsed, dict) else {
            "decision": "needs_human",
            "confidence": "low",
            "reason": "판정 응답 JSON 파싱 실패",
            "risk_flags": ["판정파싱실패"],
        }

    def _adjudicate_candidate_batch(
        self,
        *,
        municipality: str,
        sheet_key: str,
        prepared_candidates: list[dict],
        final_data: dict,
    ) -> dict[str, dict]:
        if not prepared_candidates:
            return {}
        prompt = self._adjudication_batch_prompt(
            municipality=municipality,
            sheet_key=sheet_key,
            prepared_candidates=prepared_candidates,
            final_data=final_data,
        )
        provider, model = _review_stage_backend(
            getattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini"),
            getattr(config, "HYBRID_ADJUDICATION_MODEL", ""),
        )
        candidate_ids = [str(item["candidate_id"]) for item in prepared_candidates]
        try:
            with _temporary_backend(provider, model or None):
                resp = llm_client.call_text(prompt, system=HYBRID_ADJUDICATION_SYSTEM, stage="review")
        except llm_client.LLMQuotaExceededError:
            raise
        except (llm_client.LLMCallError, RuntimeError) as exc:
            logger.warning("하이브리드 후보 묶음 판정 실패(%s): %s", sheet_key, exc)
            return {
                candidate_id: _default_adjudication(f"묶음 판정 호출 실패: {exc}", "판정호출실패")
                for candidate_id in candidate_ids
            }
        return self._normalise_batch_adjudications(llm_client.parse_json(resp), candidate_ids)

    def _merge_blockers(
        self,
        *,
        candidate: dict,
        adjudication: dict,
        sheet_key: str,
        normalized_row: dict | None,
        page_context: str,
        final_data: dict,
    ) -> list[str]:
        blockers: list[str] = []
        if not getattr(config, "HYBRID_AUTO_MERGE_ENABLED", False):
            blockers.append("자동병합비활성")
        if candidate.get("후보유형") == "기본본오염의심":
            blockers.append("기본본오염의심은로그전용")
        if adjudication.get("decision") not in {"accept", "fix_then_merge"}:
            blockers.append(f"판정={adjudication.get('decision', 'unknown')}")
        minimum = getattr(config, "HYBRID_AUTO_MERGE_MIN_CONFIDENCE", "high")
        if not _confidence_at_least(adjudication.get("confidence", ""), minimum):
            blockers.append(f"신뢰도<{minimum}")
        if not adjudication.get("evidence_page") or not adjudication.get("evidence_text"):
            blockers.append("근거부족")
        risk_flags = _risk_flags(adjudication.get("risk_flags"))
        risky = {"국가자료혼입", "해외사례", "참고자료", "단위불명확", "중복의심", "근거부족"}
        if any(flag in risky for flag in risk_flags):
            blockers.append("위험플래그")
        if _reference_flags(page_context):
            blockers.append("참고자료문맥")
        if not normalized_row:
            blockers.append("정규화행없음")
        else:
            existing_rows = final_data.get(sheet_key, []) if isinstance(final_data.get(sheet_key), list) else []
            signatures = {_row_signature(row, sheet_key) for row in existing_rows if isinstance(row, dict)}
            if _row_signature(normalized_row, sheet_key) in signatures:
                blockers.append("기본본중복")
        return blockers

    def adjudicate_and_merge(
        self,
        *,
        candidates: list[dict],
        final_data: dict,
        pages: list[PageContent],
    ) -> tuple[dict, list[dict]]:
        if not candidates or not getattr(config, "HYBRID_ADJUDICATION_ENABLED", True):
            return final_data, []
        if not self._adjudication_provider_available():
            return final_data, []

        municipality = final_data.get("municipality_name", "알 수 없음")
        pages_by_num = {page.page_number: page for page in pages}
        table_index = _build_table_page_index(pages)
        max_candidates = max(0, getattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0))
        target_candidates = candidates[:max_candidates] if max_candidates else candidates
        merge_log: list[dict] = []

        for candidate in target_candidates:
            if not isinstance(candidate, dict):
                continue
            prepared = self._prepare_candidate_for_adjudication(
                candidate=candidate,
                candidate_id=f"c{self._stats['adjudication']['checked'] + 1}",
                pages_by_num=pages_by_num,
                table_index=table_index,
            )
            if not prepared:
                continue
            adjudication = self._adjudicate_candidate(
                municipality=municipality,
                candidate=prepared["candidate_for_judgment"],
                sheet_key=prepared["sheet_key"],
                page_context=prepared["page_context"],
                final_data=final_data,
            )
            merge_log.append(self._apply_adjudication(
                municipality=municipality,
                prepared=prepared,
                adjudication=adjudication,
                final_data=final_data,
            ))

        return final_data, merge_log

    def review_and_adjudicate_by_sheet(
        self,
        *,
        pages: list[PageContent],
        final_data: dict,
        extraction_prompts: dict[str, str],
        progress: Callable[[str], None] | None = None,
        target_sheets: list[str] | None = None,
        routed_pages: dict[str, list[PageContent]] | None = None,
    ) -> tuple[dict, list[dict], list[dict]]:
        def emit(message: str) -> None:
            if progress and getattr(config, "HYBRID_PROGRESS_LOG_ENABLED", True):
                progress(message)

        if not getattr(config, "HYBRID_REVIEW_ENABLED", False):
            self._stats["skipped"] = "비활성"
            return final_data, [], []
        if not self._provider_available():
            return final_data, [], []

        municipality = final_data.get("municipality_name", "알 수 없음")
        routed = routed_pages or _route_pages_by_sheet(pages)
        configured_sheets = target_sheets or getattr(config, "HYBRID_REVIEW_SHEETS", [])
        target_sheets = [
            sheet for sheet in configured_sheets
            if sheet in config.SHEET_KEY_TO_NAME
        ]
        batch_size = getattr(config, "HYBRID_REVIEW_BATCH_SIZE", 8)
        max_batches = getattr(config, "HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 2)
        adjudication_enabled = getattr(config, "HYBRID_ADJUDICATION_ENABLED", True)
        adjudication_available = self._adjudication_provider_available() if adjudication_enabled else False
        adjudication_batch_size = max(1, int(getattr(config, "HYBRID_ADJUDICATION_BATCH_SIZE", 6)))
        max_candidates = max(0, getattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0))

        pages_by_num = {page.page_number: page for page in pages}
        table_index = _build_table_page_index(pages)
        review_candidates: list[dict] = []
        merge_log: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        adjudicated_total = 0

        emit(
            "[에이전트3c 보조검수] 시트 단위 검수 시작 "
            f"({len(target_sheets)}개 시트, 판정묶음 {adjudication_batch_size}건)"
        )

        for sheet_index, sheet_key in enumerate(target_sheets, start=1):
            sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            sheet_pages = routed.get(sheet_key, [])
            if not sheet_pages:
                self._stats["sheets"][sheet_key] = {"batches": 0, "candidates": 0, "adjudicated": 0, "merged": 0}
                emit(f"[에이전트3c 보조검수] ({sheet_index}/{len(target_sheets)}) {sheet_name}: 후보 페이지 없음")
                continue

            batches = _select_evenly(_chunks(sheet_pages, batch_size), max_batches)
            emit(
                f"[에이전트3c 보조검수] ({sheet_index}/{len(target_sheets)}) {sheet_name}: "
                f"{len(sheet_pages)}페이지, {len(batches)}배치 후보탐색"
            )

            sheet_candidates: list[dict] = []
            for batch_num, batch in enumerate(batches, start=1):
                page_nums = [page.page_number for page in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                batch_items = self._review_batch(
                    municipality=municipality,
                    sheet_key=sheet_key,
                    batch=batch,
                    final_data=final_data,
                    extraction_prompts=extraction_prompts,
                )
                added = 0
                for item in batch_items:
                    if not isinstance(item, dict):
                        continue
                    row = self._normalize_candidate(item, municipality, sheet_key, page_range)
                    if _candidate_duplicates_existing(row, final_data):
                        continue
                    dedup_key = (row["대상시트"], row["후보유형"], row["후보행JSON"])
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    self._candidates.append(row)
                    review_candidates.append(row)
                    sheet_candidates.append(row)
                    added += 1
                emit(
                    f"  - {sheet_name} 후보탐색 {batch_num}/{len(batches)} "
                    f"({page_range}): 신규 {added}건"
                )

            sheet_merged_before = self._stats["adjudication"]["merged"]
            sheet_checked_before = self._stats["adjudication"]["checked"]
            self._stats["sheets"][sheet_key] = {
                "batches": len(batches),
                "candidates": len(sheet_candidates),
                "adjudicated": 0,
                "merged": 0,
            }

            if not sheet_candidates:
                emit(f"[에이전트3d 후보판정] {sheet_name}: 후보 없음")
                continue
            if not adjudication_enabled:
                emit(f"[에이전트3d 후보판정] {sheet_name}: 후보판정 비활성, {len(sheet_candidates)}건 로그만 기록")
                continue
            if not adjudication_available:
                skipped = self._stats["adjudication"].get("skipped", "판정 백엔드 사용 불가")
                emit(f"[에이전트3d 후보판정] {sheet_name}: {skipped}")
                continue

            remaining_allowed = None if max_candidates <= 0 else max_candidates - adjudicated_total
            if remaining_allowed is not None and remaining_allowed <= 0:
                emit(f"[에이전트3d 후보판정] {sheet_name}: 전역 판정 상한 {max_candidates}건 도달")
                continue
            target_candidates_for_sheet = (
                sheet_candidates
                if remaining_allowed is None
                else sheet_candidates[:remaining_allowed]
            )
            prepared_candidates = [
                prepared for idx, candidate in enumerate(target_candidates_for_sheet, start=1)
                if (prepared := self._prepare_candidate_for_adjudication(
                    candidate=candidate,
                    candidate_id=f"{sheet_key}-{idx}",
                    pages_by_num=pages_by_num,
                    table_index=table_index,
                ))
            ]

            adjudication_chunks = _chunks(prepared_candidates, adjudication_batch_size)
            emit(
                f"[에이전트3d 후보판정] {sheet_name}: 후보 {len(prepared_candidates)}건, "
                f"{len(adjudication_chunks)}묶음 판정"
            )
            for chunk_num, prepared_chunk in enumerate(adjudication_chunks, start=1):
                before = dict(self._stats["adjudication"])
                adjudications = self._adjudicate_candidate_batch(
                    municipality=municipality,
                    sheet_key=sheet_key,
                    prepared_candidates=prepared_chunk,
                    final_data=final_data,
                )
                for prepared in prepared_chunk:
                    candidate_id = str(prepared["candidate_id"])
                    adjudication = adjudications.get(candidate_id) or _default_adjudication(
                        "묶음 판정 결과에서 candidate_id를 찾지 못했습니다.",
                        "판정누락",
                    )
                    merge_log.append(self._apply_adjudication(
                        municipality=municipality,
                        prepared=prepared,
                        adjudication=adjudication,
                        final_data=final_data,
                    ))
                adjudicated_total += len(prepared_chunk)
                after = self._stats["adjudication"]
                emit(
                    f"  - {sheet_name} 후보판정 {chunk_num}/{len(adjudication_chunks)}: "
                    f"checked +{after['checked'] - before.get('checked', 0)}, "
                    f"accept +{after['accepted'] - before.get('accepted', 0)}, "
                    f"fix +{after['fixed'] - before.get('fixed', 0)}, "
                    f"reject +{after['rejected'] - before.get('rejected', 0)}, "
                    f"needs_human +{after['needs_human'] - before.get('needs_human', 0)}, "
                    f"merge +{after['merged'] - before.get('merged', 0)}"
                )

            self._stats["sheets"][sheet_key]["adjudicated"] = (
                self._stats["adjudication"]["checked"] - sheet_checked_before
            )
            self._stats["sheets"][sheet_key]["merged"] = (
                self._stats["adjudication"]["merged"] - sheet_merged_before
            )
            emit(
                f"[에이전트3c 보조검수] {sheet_name} 완료: "
                f"후보 {len(sheet_candidates)}건, "
                f"판정 {self._stats['sheets'][sheet_key]['adjudicated']}건, "
                f"병합 {self._stats['sheets'][sheet_key]['merged']}건"
            )

        return final_data, review_candidates, merge_log

    def report(self) -> str:
        if self._stats.get("skipped"):
            return f"[에이전트3c 보조검수] 건너뜀: {self._stats['skipped']}"
        sheet_bits = []
        for sheet_key, stat in self._stats.get("sheets", {}).items():
            extra = ""
            if "adjudicated" in stat or "merged" in stat:
                extra = f"/{stat.get('adjudicated', 0)}판정/{stat.get('merged', 0)}병합"
            sheet_bits.append(
                f"{sheet_key}: {stat.get('batches', 0)}배치/{stat.get('candidates', 0)}후보{extra}"
            )
        return (
            "[에이전트3c 보조검수] 완료\n"
            f"  - 백엔드: {self._stats.get('provider')} ({self._stats.get('model') or '기본 모델'})\n"
            f"  - 후보: {len(self._candidates)}건\n"
            f"  - 시트별: {', '.join(sheet_bits) or '없음'}"
        )

    def adjudication_report(self) -> str:
        stat = self._stats.get("adjudication", {})
        if stat.get("skipped"):
            return f"[에이전트3d 후보판정] 건너뜀: {stat['skipped']}"
        return (
            "[에이전트3d 후보판정] 완료\n"
            f"  - 백엔드: {self._stats.get('adjudication_provider')} "
            f"({self._stats.get('adjudication_model') or '기본 모델'})\n"
            f"  - 판정: {stat.get('checked', 0)}건 "
            f"(accept {stat.get('accepted', 0)}, "
            f"fix_then_merge {stat.get('fixed', 0)}, "
            f"reject {stat.get('rejected', 0)}, "
            f"needs_human {stat.get('needs_human', 0)})\n"
            f"  - 근거페이지 보정: {stat.get('page_corrected', 0)}건\n"
            f"  - 자동 병합: {stat.get('merged', 0)}건 "
            f"({'활성' if getattr(config, 'HYBRID_AUTO_MERGE_ENABLED', False) else '비활성'})"
        )
