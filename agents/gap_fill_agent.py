"""
에이전트 3-b: 빈칸 보완 에이전트 (가이드라인 기반 16개 시트)

1차 추출·정제 결과에서 비어 있거나 값 채움률이 낮은 시트를 찾아, 해당 시트와
관련성이 높은 페이지만 다시 좁게 모아 재추출하여 raw_data에 보완 후보를 추가한다.

설계 포인트:
- 추출/라우팅 1차 패스가 놓친 페이지를 다시 잡기 위해, 재추출 시 라우팅 점수
  임계값을 1차(config.DOCUMENT_ROUTE_MIN_SCORE)보다 낮게 둔다(recall 우선).
- 추가된 행은 그대로 OrganizerAgent.organize()를 다시 거쳐 정규화·중복 제거되므로
  중복으로 인한 오염 위험은 dedup이 흡수한다.
- 추정값을 만들지 않도록 프롬프트에서 '원문에 명확히 보이는 값만' 추출을 강제한다.
"""

import logging

import config
from utils import llm_client
from utils.pdf_reader import PageContent
from agents.extractor_agent import (
    _SHEET_CONFIGS,
    _score_page_for_sheet,
    _build_page_text,
    _build_semantic_batches,
    EXTRACTION_SYSTEM,
)

logger = logging.getLogger(__name__)


GAP_FILL_SYSTEM = (
    EXTRACTION_SYSTEM
    + "\n\n추가 규칙: 이것은 1차 추출에서 값이 누락된 시트를 보완하는 재추출입니다. "
    "원문(배치 텍스트)에 명확히 보이는 값만 추출하고, 추정·보간·일반상식 채움은 금지합니다."
)


# 시트별로 '값이 채워진 의미 있는 행'인지 판단하는 핵심 필드.
# 이 필드 중 하나라도 값이 있으면 채워진 행으로 본다.
_VALUE_FIELDS = {
    "regional_conditions": ["값"],
    "emissions_regional": ["배출량"],
    "emissions_management": ["배출량"],
    "emissions_forecast": ["전망값"],
    "reduction_targets": ["기준배출량", "목표배출량", "목표감축량", "감축률"],
    "quantitative_reductions": ["예상감축량", "활동량"],
    "financial_plan": ["예산액"],
    "annual_implementation": ["목표물량", "연간계획"],
}

# 빈칸보완 대상 시트(수치/표 중심으로 누락이 잦은 시트). 위 _VALUE_FIELDS 키와 일치.
_GAP_FILL_SHEETS = list(_VALUE_FIELDS.keys())


class GapFillAgent:
    """비어 있거나 채움률이 낮은 시트를 재추출로 보완."""

    def __init__(self):
        self._stats: dict[str, dict] = {}
        self._total_added = 0

    def _needs_backfill(self, sheet_key: str, rows: list[dict]) -> tuple[bool, str]:
        if not rows:
            return True, "빈 시트"
        value_fields = _VALUE_FIELDS.get(sheet_key, [])
        if not value_fields:
            return False, ""
        filled = sum(
            1 for r in rows
            if isinstance(r, dict) and any(
                r.get(f) is not None and str(r.get(f, "")).strip() for f in value_fields
            )
        )
        ratio = filled / len(rows) if rows else 0.0
        min_ratio = getattr(config, "GAP_FILL_MIN_FILL_RATIO", 0.6)
        if ratio < min_ratio:
            return True, f"채움률 {ratio:.0%} < {min_ratio:.0%}"
        return False, ""

    def _relevant_pages(
        self,
        pages: list[PageContent],
        sheet_key: str,
        min_score: int,
        max_pages: int,
    ) -> list[PageContent]:
        scored = [
            (_score_page_for_sheet(page, sheet_key), page)
            for page in pages
        ]
        scored = [(score, page) for score, page in scored if score >= min_score]
        scored.sort(key=lambda item: (item[0], -item[1].page_number), reverse=True)
        return [page for _, page in scored[:max_pages]]

    def _reextract(self, sheet_key: str, batch_text: str, municipality: str) -> list[dict]:
        cfg = _SHEET_CONFIGS.get(sheet_key)
        if not cfg:
            return []
        prompt = (
            f"지자체명: {municipality}\n\n"
            "아래 배치 텍스트는 1차 추출에서 값이 누락되었을 가능성이 큰 구간입니다.\n"
            f"[배치 텍스트]\n{batch_text}\n\n"
            f"{cfg['prompt']}"
        )
        resp = llm_client.call_text(prompt, system=GAP_FILL_SYSTEM)
        parsed = llm_client.parse_json(resp)
        if not isinstance(parsed, dict):
            return []
        items = parsed.get(sheet_key, [])
        if not isinstance(items, list):
            return []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("지자체명"):
                item["지자체명"] = municipality
            out.append(item)
        return out

    def enhance(
        self,
        raw_data: dict,
        cleaned: dict,
        pages: list[PageContent],
        batch_size: int | None = None,
    ) -> dict:
        """
        Args:
            raw_data: 1차 추출(+이미지 병합) 원시 결과. 보완 행이 여기에 append 된다.
            cleaned: organize() 1차 결과(시트별 정제 데이터). 채움률 판단 기준.
            pages: 문서 전체 페이지.

        Returns:
            보완 행이 추가된 raw_data(추가가 전혀 없으면 원본 raw_data 그대로).
        """
        municipality = (
            cleaned.get("municipality_name")
            or raw_data.get("municipality_name", "알 수 없음")
        )
        updated = dict(raw_data)
        batch_size = batch_size or config.BATCH_SIZE
        min_score = getattr(config, "GAP_FILL_REEXTRACT_MIN_SCORE", 2)
        max_pages = getattr(config, "GAP_FILL_REEXTRACT_MAX_PAGES", 24)
        self._total_added = 0

        print("[에이전트3b 빈칸보완] 채움률 점검 및 보완 재추출 시작...")
        for sheet_key in _GAP_FILL_SHEETS:
            rows = cleaned.get(sheet_key, [])
            if not isinstance(rows, list):
                rows = []
            needs, reason = self._needs_backfill(sheet_key, rows)
            self._stats[sheet_key] = {
                "before": len(rows), "needs": needs, "reason": reason, "added": 0,
            }
            if not needs:
                continue

            relevant = self._relevant_pages(pages, sheet_key, min_score, max_pages)
            if not relevant:
                self._stats[sheet_key]["reason"] = reason + " / 관련 페이지 없음"
                continue

            added_rows: list[dict] = []
            # 점수순으로 뽑힌 페이지를 페이지번호 순으로 정렬한 뒤 의미 단위 배치로 묶는다.
            ordered = sorted(relevant, key=lambda p: p.page_number)
            batches = _build_semantic_batches(ordered, batch_size)
            for batch in batches:
                added_rows.extend(
                    self._reextract(sheet_key, _build_page_text(batch), municipality)
                )

            if added_rows:
                updated[sheet_key] = list(updated.get(sheet_key, [])) + added_rows
                self._total_added += len(added_rows)
                self._stats[sheet_key]["added"] = len(added_rows)
                print(
                    f"  [{sheet_key}] {reason} → 관련 {len(relevant)}p 재추출, "
                    f"보완 후보 {len(added_rows)}건"
                )

        print(f"[에이전트3b 빈칸보완] 완료. 보완 후보 총 {self._total_added}건 추가")
        return updated if self._total_added > 0 else raw_data

    def report(self) -> str:
        needy = {k: v for k, v in self._stats.items() if v.get("needs")}
        lines = ["[에이전트3b 빈칸보완] 완료", f"  - 보완 후보 총 {self._total_added}건 추가"]
        for sheet_key, info in needy.items():
            lines.append(
                f"  - {sheet_key}: 기존 {info['before']}건 "
                f"({info['reason']}) → 추가 {info['added']}건"
            )
        if not needy:
            lines.append("  - 보완 필요 시트 없음")
        return "\n".join(lines)
