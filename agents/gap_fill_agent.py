"""
에이전트 3-b: 빈칸 보완 에이전트

1차 정제 결과에서 값이 많이 비어 있는 행을 찾아, 관련 원문 페이지만 좁게
다시 조회해 raw_data에 보완 후보를 추가합니다.
"""

import json
import re

import config
from utils import llm_client
from utils.pdf_reader import PageContent


GAP_FILL_SYSTEM = """당신은 한국 지자체 탄소중립 계획 보고서의 누락값을 보완하는 데이터 검수자입니다.
반드시 JSON만 반환하세요.
원문에 명확히 있는 값만 채우고, 추정이 필요한 값은 null로 두세요."""


def _yearly_count(row: dict) -> int:
    yearly = row.get("연도별") or {}
    return sum(1 for year in config.YEARS if yearly.get(str(year)) is not None)


def _compact(text: str, limit: int = 9000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _row_terms(row: dict, fields: list[str]) -> list[str]:
    terms = []
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return list(dict.fromkeys(terms))


def _chunks(rows: list[dict], size: int) -> list[list[dict]]:
    size = max(size, 1)
    return [rows[i:i + size] for i in range(0, len(rows), size)]


_FOCUSED_CONFIGS = {
    "vehicle": {
        "strong": [
            "자동차 등록대수", "자동차 등록 대수", "등록 자동차", "등록대수",
            "연료별 자동차", "차종별 자동차", "차종별 주행거리",
            "연료별 자동차 등록 대수", "주행거리",
        ],
        "weak": [
            "자동차", "차량", "승용차", "화물차", "승합차", "특수차",
            "휘발유", "경유", "LPG", "전기차", "수소차", "하이브리드",
        ],
        "negative": ["전기차 보급", "충전기", "투자계획", "감축효과", "예산", "설문"],
    },
    "energy": {
        "strong": [
            "최종에너지 원별 소비량", "부문별 최종에너지 소비량",
            "최종에너지 소비량", "에너지원별 소비량", "용도별 전력 소비량",
            "서울시 최종에너지", "전력소비량",
        ],
        "weak": [
            "에너지", "소비량", "석유", "도시가스", "전력", "열에너지",
            "신재생", "가정·상업", "공공·기타", "산업", "수송", "toe", "TOE",
        ],
        "negative": ["투자계획", "감축효과", "예산", "설문", "교육"],
    },
}


def _score_focused_page(page: PageContent, sheet_key: str) -> int:
    cfg = _FOCUSED_CONFIGS[sheet_key]
    body = (page.text or "") + "\n" + "\n".join(page.tables or [])
    score = 0
    score += sum(4 for kw in cfg["strong"] if kw in body)
    score += sum(1 for kw in cfg["weak"] if kw in body)
    score -= sum(3 for kw in cfg["negative"] if kw in body)
    if page.tables:
        score += 2
    if page.images:
        score += 1
    if re.search(r"\b20(0[5-9]|1[0-9]|2[0-9]|3[0-4])\b", body):
        score += 1
    return score


class GapFillAgent:
    """빈칸이 큰 행만 보완 재추출."""

    def __init__(self):
        self._stats = {"targets": {}, "updates": {}}

    def _page_text(self, page: PageContent) -> str:
        text = page.text or ""
        if page.tables:
            text += "\n\n[표 데이터]\n" + "\n\n".join(page.tables)
        return text

    def _find_context(self, pages: list[PageContent], terms: list[str], fallback_terms: list[str]) -> str:
        scored = []
        for page in pages:
            body = self._page_text(page)
            score = 0
            for term in terms:
                if term and term in body:
                    score += 5
            for term in fallback_terms:
                if term and term in body:
                    score += 1
            if score > 0:
                scored.append((score, page.page_number, body))

        scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = scored[: config.GAP_FILL_CONTEXT_PAGES]
        return "\n\n".join(
            f"=== 페이지 {page_num} ===\n{body[:2500]}"
            for _, page_num, body in selected
        )

    def _focused_context_batches(self, pages: list[PageContent], sheet_key: str) -> list[str]:
        """자동차/에너지 전용으로 관련 가능성이 높은 구간만 다시 묶는다."""
        if not getattr(config, "FOCUSED_GAP_FILL_ENABLED", True):
            return []

        min_score = config.FOCUSED_GAP_FILL_MIN_SCORE.get(sheet_key, 5)
        max_anchors = config.FOCUSED_GAP_FILL_MAX_ANCHORS.get(sheet_key, 10)
        context_radius = getattr(config, "FOCUSED_GAP_FILL_CONTEXT_PAGES", 2)
        batch_pages = getattr(config, "FOCUSED_GAP_FILL_BATCH_PAGES", 6)

        scored = []
        by_num = {page.page_number: page for page in pages}
        for page in pages:
            score = _score_focused_page(page, sheet_key)
            if score >= min_score:
                scored.append((score, page.page_number))

        scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        anchors = [page_num for _, page_num in scored[:max_anchors]]
        selected_nums: set[int] = set()
        for page_num in anchors:
            for n in range(page_num - context_radius, page_num + context_radius + 1):
                if n in by_num:
                    selected_nums.add(n)

        selected = [by_num[n] for n in sorted(selected_nums)]
        chunks = [selected[i:i + batch_pages] for i in range(0, len(selected), batch_pages)]
        contexts = []
        for chunk in chunks:
            parts = []
            for page in chunk:
                body = self._page_text(page)
                parts.append(f"=== 페이지 {page.page_number} ===\n{body[:3200]}")
            contexts.append("\n\n".join(parts))
        return contexts

    def _call_gap_fill(self, sheet_key: str, targets: list[dict], context: str, municipality: str) -> list[dict]:
        if not targets or not context.strip():
            return []

        sample = json.dumps(targets, ensure_ascii=False)
        schemas = {
            "vehicle": (
                '{"vehicle": [{"지자체명": "...", "용도": "전체|승용|화물|버스|이륜|특수|승합", '
                '"차종": "경유|휘발유|LPG|전기|수소|하이브리드", "대수": 숫자or null, '
                '"주행거리": 숫자or null, "보완상태": "보완|원문미기재"}]}'
            ),
            "energy": (
                '{"energy": [{"지자체명": "...", "용도": "가정|상업|공공|산업|수송 등", '
                '"석유_에너지유": 숫자or null, "석유_LPG": 숫자or null, "석유_비에너지유": 숫자or null, '
                '"가스": 숫자or null, "전력": 숫자or null, "열": 숫자or null, "신재생": 숫자or null, '
                '"보완상태": "보완|원문미기재"}]}'
            ),
            "ghg": (
                '{"ghg": [{"지자체명": "...", "배출유형": "직접배출|간접배출|흡수원", '
                '"종류": "현황|전망|목표", "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계|기타", '
                '"연도별": {"2018": 숫자, "2030": 숫자}, "보완상태": "보완|원문미기재"}]}'
            ),
            "strategy": (
                '{"strategy": [{"지자체명": "...", "배출유형": "직접배출|간접배출", '
                '"감축전략_부문": "...", "감축사업명": "...", "감축사업명_세부": "...", '
                '"구분": "공통|특화", "성과지표": "...", '
                '"종류": "계획(지표)|계획(감축량)|계획(예산)|실적(지표)|실적(감축량)|실적(예산)", '
                '"연도별": {"2024": 숫자}, "보완상태": "보완|원문미기재|정성사업"}]}'
            ),
        }
        prompt = f"""지자체명: {municipality}

다음은 1차 추출 결과에서 값이 비어 있는 {sheet_key} 행입니다.
원문 문맥을 보고 누락값을 보완하세요.

[보완 대상]
{sample}

[관련 원문]
{_compact(context)}

반환 형식:
{schemas[sheet_key]}

규칙:
- 원문에 명확한 숫자가 있는 값만 채우세요.
- 원문에 숫자 없이 정성 설명만 있으면 보완상태를 "정성사업" 또는 "원문미기재"로 두세요.
- 대상 행에 대응하는 원문을 찾지 못하면 값은 null로 두고 보완상태만 표시하세요.
- 기존 행 식별에 필요한 사업명/부문/용도/차종은 반드시 유지하세요."""

        resp = llm_client.call_text(prompt, system=GAP_FILL_SYSTEM)
        parsed = llm_client.parse_json(resp)
        if not isinstance(parsed, dict):
            return []
        rows = parsed.get(sheet_key, [])
        return rows if isinstance(rows, list) else []

    def _extract_vehicle_table(self, context: str, municipality: str) -> list[dict]:
        if not context.strip():
            return []
        prompt = f"""지자체명: {municipality}

다음 원문에서 자동차 현황 데이터를 다시 추출하세요.
특히 등록대수(대)를 우선 채우세요. 주행거리가 원문에 없으면 null로 둡니다.

[관련 원문]
{_compact(context, 12000)}

반환 형식:
{{
  "vehicle": [
    {{"지자체명": "...", "용도": "전체|승용|화물|버스|이륜|특수|승합", "차종": "경유|휘발유|LPG|전기|수소|하이브리드|CNG|기타", "대수": 숫자or null, "주행거리": 숫자or null}}
  ]
}}

규칙:
- 대수와 주행거리를 혼동하지 마세요.
- 연료별 전체 자동차 등록대수만 있으면 용도는 "전체"로 기록하세요.
- 차종별 등록대수만 있으면 차종은 "전체" 또는 "기타"로 두고 용도에 승용/화물/승합/특수 등을 기록하세요.
- 차량 대수·주행거리와 무관한 표이면 빈 배열을 반환하세요.
- 전기차 성장률, 보급 목표, 추진계획, 투자계획, 감축효과 표의 숫자는 자동차 현황 대수로 사용하지 마세요.
- 표 제목, 비율, 증감률, 예산, 배출량 숫자는 대수로 사용하지 마세요."""
        resp = llm_client.call_text(prompt, system=GAP_FILL_SYSTEM)
        parsed = llm_client.parse_json(resp)
        if not isinstance(parsed, dict):
            return []
        rows = parsed.get("vehicle", [])
        return rows if isinstance(rows, list) else []

    def _extract_energy_table(self, context: str, municipality: str) -> list[dict]:
        if not context.strip():
            return []
        prompt = f"""지자체명: {municipality}

다음 원문에서 에너지 소비 현황 데이터를 다시 추출하세요.
원문에 명확한 수치가 있는 셀만 채우고, 교차표가 없으면 억지로 추정하지 마세요.

[관련 원문]
{_compact(context, 14000)}

반환 형식:
{{
  "energy": [
    {{"지자체명": "...", "용도": "전체|가정|상업|공공|기타|산업|수송|가정·상업|공공·기타", "석유_에너지유": 숫자or null, "석유_LPG": 숫자or null, "석유_비에너지유": 숫자or null, "가스": 숫자or null, "전력": 숫자or null, "열": 숫자or null, "신재생": 숫자or null}}
  ]
}}

규칙:
- [표]에 에너지원별 총량만 있으면 용도는 "전체"로 기록하세요.
- [표]에 부문별 총량만 있으면 해당 부문의 합계 성격 값을 석유/전력/가스 등에 억지 배분하지 말고, 에너지원별 수치가 확인되는 경우에만 채우세요.
- 에너지원별 총량을 가정/상업/공공 등 특정 용도에 임의 배정하지 마세요.
- 서술문에 비중(%)만 있는 경우 실제 소비량으로 환산하지 마세요.
- 단위가 천TOE, TOE, TJ, GWh 등으로 명시된 수치만 사용하세요.
- 변화율, 비율, 예산, 온실가스 배출량은 에너지 소비량으로 사용하지 마세요."""
        resp = llm_client.call_text(prompt, system=GAP_FILL_SYSTEM)
        parsed = llm_client.parse_json(resp)
        if not isinstance(parsed, dict):
            return []
        rows = parsed.get("energy", [])
        return rows if isinstance(rows, list) else []

    def _focused_extract_vehicle_energy(
        self,
        raw_data: dict,
        cleaned: dict,
        pages: list[PageContent],
        municipality: str,
    ) -> tuple[dict, int]:
        updated = dict(raw_data)
        total = 0

        vehicle_rows = cleaned.get("vehicle", [])
        vehicle_count_filled = sum(1 for row in vehicle_rows if row.get("대수") is not None)
        vehicle_needs_focus = not vehicle_rows or (
            vehicle_count_filled / len(vehicle_rows) < 0.7
            or all(row.get("주행거리") is None for row in vehicle_rows)
        )

        energy_cols = ["석유_에너지유", "석유_LPG", "석유_비에너지유", "가스", "전력", "열", "신재생"]
        energy_rows = cleaned.get("energy", [])
        energy_filled_cells = sum(1 for row in energy_rows for col in energy_cols if row.get(col) is not None)
        energy_possible_cells = len(energy_rows) * len(energy_cols)
        energy_needs_focus = not energy_rows or (
            energy_possible_cells > 0 and energy_filled_cells / energy_possible_cells < 0.35
        )

        if vehicle_needs_focus:
            contexts = self._focused_context_batches(pages, "vehicle")
            focused_rows: list[dict] = []
            for context in contexts:
                focused_rows.extend(self._extract_vehicle_table(context, municipality))
            if focused_rows:
                updated["vehicle"] = list(updated.get("vehicle", [])) + focused_rows
                total += len(focused_rows)
                self._stats["updates"]["vehicle_focused"] = len(focused_rows)
            else:
                self._stats["updates"]["vehicle_focused"] = 0

        if energy_needs_focus:
            contexts = self._focused_context_batches(pages, "energy")
            focused_rows = []
            for context in contexts:
                focused_rows.extend(self._extract_energy_table(context, municipality))
            if focused_rows:
                updated["energy"] = list(updated.get("energy", [])) + focused_rows
                total += len(focused_rows)
                self._stats["updates"]["energy_focused"] = len(focused_rows)
            else:
                self._stats["updates"]["energy_focused"] = 0

        return updated, total

    def _vehicle_targets(self, cleaned: dict) -> list[dict]:
        rows = []
        for row in cleaned.get("vehicle", []):
            if row.get("대수") is None or row.get("주행거리") is None:
                rows.append(row)
        return rows[: config.GAP_FILL_MAX_TARGETS.get("vehicle", 20)]

    def _energy_targets(self, cleaned: dict) -> list[dict]:
        cols = ["석유_에너지유", "석유_LPG", "석유_비에너지유", "가스", "전력", "열", "신재생"]
        rows = []
        for row in cleaned.get("energy", []):
            filled = sum(1 for col in cols if row.get(col) is not None)
            if filled < 3:
                rows.append(row)
        return rows[: config.GAP_FILL_MAX_TARGETS.get("energy", 20)]

    def _ghg_targets(self, cleaned: dict) -> list[dict]:
        rows = [row for row in cleaned.get("ghg", []) if _yearly_count(row) <= 1]
        return rows[: config.GAP_FILL_MAX_TARGETS.get("ghg", 30)]

    def _strategy_targets(self, cleaned: dict) -> list[dict]:
        rows = [row for row in cleaned.get("strategy", []) if _yearly_count(row) == 0]
        return rows[: config.GAP_FILL_MAX_TARGETS.get("strategy", 60)]

    def enhance(self, raw_data: dict, cleaned: dict, pages: list[PageContent]) -> dict:
        municipality = cleaned.get("municipality_name") or raw_data.get("municipality_name", "알 수 없음")
        updated = dict(raw_data)
        total_updates = 0

        plans = [
            ("vehicle", self._vehicle_targets(cleaned), ["자동차", "차량", "등록대수", "주행거리"]),
            ("energy", self._energy_targets(cleaned), ["에너지", "전력", "도시가스", "석유", "신재생", "소비량"]),
            ("ghg", self._ghg_targets(cleaned), ["온실가스", "배출량", "전망", "목표", "tCO2", "CO2eq"]),
            ("strategy", self._strategy_targets(cleaned), ["감축", "사업", "성과지표", "예산", "감축량", "실적"]),
        ]

        for sheet_key, targets, fallback_terms in plans:
            self._stats["targets"][sheet_key] = len(targets)
            if not targets:
                self._stats["updates"][sheet_key] = 0
                continue

            terms = []
            if sheet_key == "vehicle":
                for row in targets:
                    terms.extend(_row_terms(row, ["용도", "차종"]))
            elif sheet_key == "energy":
                for row in targets:
                    terms.extend(_row_terms(row, ["용도"]))
            elif sheet_key == "ghg":
                for row in targets:
                    terms.extend(_row_terms(row, ["종류", "부문"]))
            else:
                for row in targets:
                    terms.extend(_row_terms(row, ["감축사업명", "감축사업명_세부", "성과지표"]))

            updates = []
            for batch in _chunks(targets, config.GAP_FILL_TARGET_BATCH_SIZE):
                batch_terms = []
                if sheet_key == "vehicle":
                    for row in batch:
                        batch_terms.extend(_row_terms(row, ["용도", "차종"]))
                elif sheet_key == "energy":
                    for row in batch:
                        batch_terms.extend(_row_terms(row, ["용도"]))
                elif sheet_key == "ghg":
                    for row in batch:
                        batch_terms.extend(_row_terms(row, ["종류", "부문"]))
                else:
                    for row in batch:
                        batch_terms.extend(_row_terms(row, ["감축사업명", "감축사업명_세부", "성과지표"]))
                context = self._find_context(pages, batch_terms or terms, fallback_terms)
                updates.extend(self._call_gap_fill(sheet_key, batch, context, municipality))
            self._stats["updates"][sheet_key] = len(updates)
            if updates:
                total_updates += len(updates)
                updated.setdefault(sheet_key, [])
                updated[sheet_key] = list(updated.get(sheet_key, [])) + updates

            if sheet_key == "vehicle":
                vehicle_rows = cleaned.get("vehicle", [])
                filled = sum(1 for row in vehicle_rows if row.get("대수") is not None)
                fill_rate = filled / len(vehicle_rows) if vehicle_rows else 0
                if fill_rate < 0.5:
                    context = self._find_context(
                        pages,
                        ["자동차", "차량", "등록대수", "차종별", "용도별"],
                        ["자동차", "차량", "등록대수", "주행거리"],
                    )
                    extra = self._extract_vehicle_table(context, municipality)
                    if extra:
                        total_updates += len(extra)
                        self._stats["updates"][sheet_key] += len(extra)
                        updated.setdefault("vehicle", [])
                        updated["vehicle"] = list(updated.get("vehicle", [])) + extra

        focused_updated, focused_count = self._focused_extract_vehicle_energy(
            raw_data=updated,
            cleaned=cleaned,
            pages=pages,
            municipality=municipality,
        )
        if focused_count:
            updated = focused_updated
            total_updates += focused_count

        # 연도값이 끝내 없는 감축전략은 삭제하지 않고 상태 표시용 후보를 추가한다.
        for row in self._strategy_targets(cleaned):
            marker = dict(row)
            marker["보완상태"] = "정성사업/원문미기재"
            updated.setdefault("strategy_gap_notes", []).append(marker)

        return updated if total_updates > 0 else raw_data

    def report(self) -> str:
        targets = ", ".join(f"{k}: {v}건" for k, v in self._stats.get("targets", {}).items())
        updates = ", ".join(f"{k}: {v}건" for k, v in self._stats.get("updates", {}).items())
        return (
            "[에이전트3b 빈칸보완] 완료\n"
            f"  - 보완 대상: {targets or '없음'}\n"
            f"  - 보완 후보 추가: {updates or '없음'}"
        )
