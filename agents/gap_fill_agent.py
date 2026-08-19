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
from utils.parallel import parallel_map_collect
from utils.pdf_reader import PageContent
from agents.extractor_agent import (
    _SHEET_CONFIGS,
    _score_page_for_sheet,
    _build_page_text,
    _build_semantic_batches,
    _page_nums_from_batch_text,
    _attach_row_context,
    _PROVENANCE_INSTRUCTION,
    _ROUTE_CONFIGS,
    BatchRecord,
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
        self.ledger: list[BatchRecord] = []

    def _rows_for(self, sheet_key: str, cleaned: dict | list[dict]) -> list[dict]:
        if isinstance(cleaned, list):
            return cleaned
        rows = cleaned.get(sheet_key, [])
        return rows if isinstance(rows, list) else []

    def _needs_backfill(self, sheet_key: str, cleaned: dict | list[dict]) -> tuple[bool, str]:
        rows = self._rows_for(sheet_key, cleaned)
        if not rows:
            return True, "빈 시트"
        thresholds = getattr(config, "GAP_FILL_COVERAGE_THRESHOLDS", {})

        if sheet_key == "emissions_regional":
            sectors = {r.get("부문") for r in rows if isinstance(r, dict) and r.get("부문")}
            years = {r.get("연도") for r in rows if isinstance(r, dict) and r.get("연도")}
            min_sectors = int(thresholds.get("emissions_regional_min_sectors", 4))
            min_years = int(thresholds.get("emissions_regional_min_years", 2))
            reasons = []
            if len(sectors) < min_sectors:
                reasons.append(f"부문 {len(sectors)}/{min_sectors}")
            if len(years) < min_years:
                reasons.append(f"연도 {len(years)}/{min_years}")
            return (bool(reasons), ", ".join(reasons))

        if sheet_key == "emissions_management":
            sectors = {r.get("관리부문") for r in rows if isinstance(r, dict) and r.get("관리부문")}
            min_sectors = int(thresholds.get("emissions_management_min_sectors", 2))
            if len(sectors) < min_sectors:
                return True, f"관리부문 {len(sectors)}/{min_sectors}"
            return False, ""

        if sheet_key == "emissions_forecast":
            years = {r.get("연도") for r in rows if isinstance(r, dict) and r.get("연도")}
            min_years = int(thresholds.get("emissions_forecast_min_years", 2))
            if len(years) < min_years:
                return True, f"연도 {len(years)}/{min_years}"
            return False, ""

        if sheet_key == "reduction_targets":
            years = {r.get("목표연도") for r in rows if isinstance(r, dict) and r.get("목표연도")}
            # 2050은 장기 비전 탐색 대상이며 정량 목표 행을 강제하지 않는다.
            missing = [year for year in (2030,) if year not in years]
            if missing:
                return True, "목표연도 누락: " + ", ".join(str(year) for year in missing)
            return False, ""

        if isinstance(cleaned, dict) and sheet_key in {"quantitative_reductions", "annual_implementation"}:
            project_rows = cleaned.get("mitigation_projects", [])
            project_count = len(project_rows) if isinstance(project_rows, list) else 0
            ratio = float(thresholds.get("project_ratio", 0.3))
            if project_count and len(rows) < project_count * ratio:
                return True, f"사업 대비 행수 {len(rows)}/{project_count} < {ratio:.0%}"
            return False, ""

        if sheet_key == "financial_plan":
            years = {r.get("연도") for r in rows if isinstance(r, dict) and r.get("연도")}
            sectors = {r.get("부문") for r in rows if isinstance(r, dict) and r.get("부문")}
            min_years = int(thresholds.get("financial_plan_min_years", 2))
            min_sectors = int(thresholds.get("financial_plan_min_sectors", 2))
            reasons = []
            if len(years) < min_years:
                reasons.append(f"연도 {len(years)}/{min_years}")
            if len(sectors) < min_sectors:
                reasons.append(f"부문 {len(sectors)}/{min_sectors}")
            return (bool(reasons), ", ".join(reasons))

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

    def _has_strong_keyword(self, page: PageContent, sheet_key: str) -> bool:
        cfg = _ROUTE_CONFIGS.get(sheet_key, {})
        combined = f"{page.text or ''}\n" + "\n".join(page.tables or [])
        return any(keyword in combined for keyword in cfg.get("strong", []))

    def _relevant_pages(
        self,
        pages: list[PageContent],
        sheet_key: str,
        min_score: int,
        max_pages: int,
        exclude_nums: set[int] | None = None,
    ) -> list[PageContent]:
        exclude_nums = exclude_nums or set()
        scored = [
            (_score_page_for_sheet(page, sheet_key), page)
            for page in pages
            if page.page_number not in exclude_nums and self._has_strong_keyword(page, sheet_key)
        ]
        scored = [(score, page) for score, page in scored if score >= min_score]
        scored.sort(key=lambda item: (item[0], -item[1].page_number), reverse=True)
        return [page for _, page in scored[:max_pages]]

    def _reextract(self, sheet_key: str, batch_text: str, municipality: str) -> list[dict]:
        cfg = _SHEET_CONFIGS.get(sheet_key)
        if not cfg:
            return []
        page_nums = _page_nums_from_batch_text(batch_text)
        prompt = (
            f"지자체명: {municipality}\n\n"
            "아래 배치 텍스트는 1차 추출에서 값이 누락되었을 가능성이 큰 구간입니다.\n"
            f"[배치 텍스트]\n{batch_text}\n\n"
            f"{cfg['prompt']}\n\n{_PROVENANCE_INSTRUCTION}"
        )
        parsed, parse_ok = llm_client.call_text_json(prompt, system=GAP_FILL_SYSTEM, stage="gap_fill")
        if not parse_ok or not isinstance(parsed, dict):
            self.ledger.append(BatchRecord(sheet_key, page_nums, "parse_fail", 0, "JSON 파싱 실패"))
            return []
        items = parsed.get(sheet_key, [])
        if not isinstance(items, list):
            self.ledger.append(BatchRecord(sheet_key, page_nums, "parse_fail", 0, "스키마 불일치"))
            return []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = _attach_row_context(item, municipality, page_nums)
            row["보완출처"] = "gap_fill"
            out.append(row)
        self.ledger.append(BatchRecord(sheet_key, page_nums, "ok", len(out)))
        return out

    def enhance(
        self,
        raw_data: dict,
        cleaned: dict,
        pages: list[PageContent],
        batch_size: int | None = None,
        extracted_page_nums: dict[str, set[int]] | None = None,
        routed_page_nums: dict[str, set[int]] | None = None,
    ) -> dict:
        """
        Args:
            raw_data: 1차 추출(+이미지 병합) 원시 결과. 보완 행이 여기에 append 된다.
            cleaned: organize() 1차 결과(시트별 정제 데이터). 채움률 판단 기준.
            pages: 문서 전체 페이지.
            extracted_page_nums: 1차 추출 원장에서 status==ok인 성공 페이지번호.
                GAP_FILL_SKIP_ALREADY_ROUTED=True이면 이 페이지만 재추출 후보에서 제외한다.

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
        skip_routed = getattr(config, "GAP_FILL_SKIP_ALREADY_ROUTED", True)
        extracted_page_nums = extracted_page_nums or routed_page_nums or {}
        self._total_added = 0
        self.ledger = []

        # 보완 대상 시트별로 재추출 배치를 모두 모은 뒤 동시에 호출한다(배치 간 독립).
        sheet_batches: list[dict] = []
        print("[에이전트3b 빈칸보완] 채움률 점검 및 보완 재추출 시작...")
        for sheet_key in _GAP_FILL_SHEETS:
            rows = self._rows_for(sheet_key, cleaned)
            needs, reason = self._needs_backfill(sheet_key, cleaned)
            self._stats[sheet_key] = {
                "before": len(rows), "needs": needs, "reason": reason, "added": 0,
            }
            if not needs:
                continue

            exclude_nums = extracted_page_nums.get(sheet_key, set()) if skip_routed else set()
            relevant = self._relevant_pages(
                pages, sheet_key, min_score, max_pages, exclude_nums=exclude_nums
            )
            if not relevant:
                note = " / 관련 페이지 없음"
                if skip_routed and exclude_nums:
                    note = " / 1차 미전송 신규 페이지 없음(이미 라우팅됨)"
                self._stats[sheet_key]["reason"] = reason + note
                continue

            # 점수순으로 뽑힌 페이지를 페이지번호 순으로 정렬한 뒤 의미 단위 배치로 묶는다.
            ordered = sorted(relevant, key=lambda p: p.page_number)
            for batch in _build_semantic_batches(ordered, batch_size):
                page_nums = [page.page_number for page in batch]
                sheet_batches.append({
                    "sheet_key": sheet_key,
                    "batch": batch,
                    "reason": reason,
                    "relevant_n": len(relevant),
                    "page_nums": page_nums,
                })

        results = parallel_map_collect(
            lambda sb: self._reextract(sb["sheet_key"], _build_page_text(sb["batch"]), municipality),
            sheet_batches,
            workers=getattr(config, "TEXT_WORKERS", 4),
            stats_label="gap_fill",
        )

        added_by_sheet: dict[str, list[dict]] = {}
        info_by_sheet: dict[str, tuple[str, int]] = {}
        failed_batches = 0
        for task, (added_rows, err) in zip(sheet_batches, results):
            sheet_key = task["sheet_key"]
            if err is not None:
                failed_batches += 1
                self.ledger.append(BatchRecord(
                    sheet_key, list(task["page_nums"]), "call_fail", 0,
                    f"{type(err).__name__}: {str(err)[:200]}",
                ))
                print(f"  [{sheet_key}] 보완 배치 호출 실패({type(err).__name__}) — 원장 기록")
                continue
            added_by_sheet.setdefault(sheet_key, []).extend(added_rows or [])
            info_by_sheet[sheet_key] = (task["reason"], task["relevant_n"])

        for sheet_key, added_rows in added_by_sheet.items():
            if not added_rows:
                continue
            updated[sheet_key] = list(updated.get(sheet_key, [])) + added_rows
            self._total_added += len(added_rows)
            self._stats[sheet_key]["added"] = len(added_rows)
            reason, relevant_n = info_by_sheet[sheet_key]
            print(
                f"  [{sheet_key}] {reason} → 관련 {relevant_n}p 재추출, "
                f"보완 후보 {len(added_rows)}건"
            )

        if failed_batches:
            print(f"[에이전트3b 빈칸보완] 호출 실패 배치 {failed_batches}건 원장 기록")
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
