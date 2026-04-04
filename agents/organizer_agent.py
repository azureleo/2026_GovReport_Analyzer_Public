"""
에이전트 3: 정리·정제 에이전트

에이전트2(텍스트)와 에이전트2b(이미지)의 원시 추출 결과를 받아
최종 엑셀 출력에 맞게 정제·정리합니다.
"""

import logging
import re
import statistics
from copy import deepcopy
from typing import Any

import config
from utils import llm_client

logger = logging.getLogger(__name__)


ORGANIZER_SYSTEM = """당신은 한국 지자체 탄소중립 계획 데이터를 정제하는 전문 데이터 큐레이터입니다.
반드시 JSON만 반환하세요. 설명 텍스트 없이 JSON만."""


# ── 부문 정규화: IPCC 코드, 복합 표기, 이형어 모두 표준 부문명으로 통일 ──
_SECTOR_MAP = {
    # 기존 표기 변형
    "건물부문": "건물", "수송부문": "수송", "농업부문": "농축산",
    "농업": "농축산", "폐기물부문": "폐기물", "흡수": "흡수원",
    "산업부문": "산업", "에너지전환": "전환", "전환부문": "전환", "수소부문": "수소",
    # 복합·합산 부문 표기 → 표준화
    "상업/공공": "건물", "상업·공공": "건물",
    "가정·상업": "건물", "가정/상업": "건물",
    "도로수송": "수송", "도로": "수송", "비도로수송": "수송", "비도로": "수송",
    "에너지생산": "전환", "에너지 생산": "전환",
    # IPCC 분류 코드 → 표준 부문
    "1A 연료연소": "전환", "1B 탈루배출": "전환",
    "2 산업공정": "산업", "2A 광물산업": "산업", "2B 화학산업": "산업",
    "3A1 장내발효": "농축산", "3A2 가축분뇨": "농축산", "3B 토지이용": "흡수원",
    "4A 폐기물 매립(직접)": "폐기물", "4A 폐기물매립": "폐기물",
    "4B 생물학적처리": "폐기물", "4C 폐수처리": "폐기물", "4D 소각": "폐기물",
}

# ── 배출유형 정규화 ──
_TYPE_MAP = {
    "직접 배출": "직접배출", "직접배출량": "직접배출",
    "간접 배출": "간접배출", "간접배출량": "간접배출",
    "흡수": "흡수원", "흡수량": "흡수원",
}

# ── GHG 데이터 종류 정규화 ──
_GHG_DATA_TYPE_MAP = {
    "현황(실적)": "현황", "실적": "현황",
    "BAU": "전망", "전망(BAU)": "전망",
    "NDC": "목표", "감축목표": "목표", "계획": "목표",
}

# ── 자동차 용도 정규화 ──
_VEHICLE_USAGE_MAP = {
    "승용차": "승용", "승용 차": "승용",
    "화물차": "화물", "화물 차": "화물",
    "이륜차": "이륜", "이륜 차": "이륜",
    "승합차": "승합", "승합 차": "승합",
    "특수차": "특수", "특수 차": "특수",
}

# ── 에너지 합산 용도: 해당 구성 세분 용도가 모두 있으면 합산 행 제거 ──
_ENERGY_AGGREGATE_MAP: dict[str, set[str]] = {
    "가정·상업": {"가정", "상업"},
    "가정/상업": {"가정", "상업"},
    "가정 및 상업": {"가정", "상업"},
    "상업·공공": {"상업", "공공"},
    "공공·기타": {"공공", "기타"},
}


def _normalize(val: str, mapping: dict) -> str:
    if not isinstance(val, str):
        return val
    return mapping.get(val.strip(), val.strip())


def _normalize_sector(val: str) -> str:
    """부문명 정규화: IPCC 코드 prefix 패턴(숫자코드 + 공백)도 제거 후 매핑"""
    if not isinstance(val, str):
        return val
    v = val.strip()
    # 먼저 정확히 매핑되는지 확인
    if v in _SECTOR_MAP:
        return _SECTOR_MAP[v]
    # IPCC 숫자+문자 코드 패턴 제거 후 재시도 (예: "1A2 산업연소" → "산업")
    stripped = re.sub(r'^[\dA-Z]+[A-Za-z\d]*\s+', '', v).strip()
    if stripped in _SECTOR_MAP:
        return _SECTOR_MAP[stripped]
    # 표 제목처럼 긴 텍스트는 빈 문자열로 처리 (30자 초과, 숫자·괄호 포함)
    if len(v) > 30 and re.search(r'[\[\]【】표그림]', v):
        return ""
    return v


def _normalize_strategy_name(name: str) -> str:
    """감축사업명에서 선행 영문코드(B1, M1 등) 제거"""
    if not isinstance(name, str):
        return name
    # 예: "B1 기존 건물 ZEB 전환" → "기존 건물 ZEB 전환"
    cleaned = re.sub(r'^[A-Z]\d+\s+', '', name.strip())
    return cleaned if cleaned else name.strip()


def _is_code_only_name(name: str) -> bool:
    """'B1', 'M1' 처럼 코드만으로 된 사업명 여부"""
    return bool(re.fullmatch(r'[A-Z]\d+', name.strip() if isinstance(name, str) else ""))


def _deduplicate_vehicle(rows: list[dict]) -> list[dict]:
    """자동차 데이터: 용도 정규화 후 용도+차종 기준 중복 제거 (값 합산 아닌 우선순위 병합)"""
    merged: dict[tuple, dict] = {}
    for r in rows:
        # 용도 정규화
        r["용도"] = _VEHICLE_USAGE_MAP.get(r.get("용도", "").strip(), r.get("용도", ""))
        key = (r.get("지자체명", ""), r.get("용도", ""), r.get("차종", ""))
        if key not in merged:
            merged[key] = dict(r)
        else:
            # 기존에 None인 필드를 새 값으로 보완
            for field in ["대수", "주행거리"]:
                if merged[key].get(field) is None and r.get(field) is not None:
                    merged[key][field] = r[field]
    return list(merged.values())


def _filter_energy_aggregates(rows: list[dict]) -> list[dict]:
    """에너지 데이터: 세분 용도가 이미 있으면 합산 용도 행 제거"""
    all_usages = {r.get("용도", "") for r in rows}
    result = []
    for r in rows:
        usage = r.get("용도", "")
        if usage in _ENERGY_AGGREGATE_MAP:
            parts = _ENERGY_AGGREGATE_MAP[usage]
            if parts.issubset(all_usages):
                logger.debug(f"에너지 합산 행 제거: {usage} (세분값 존재)")
                continue
        result.append(r)
    return result


def _filter_ghg_outliers(rows: list[dict]) -> list[dict]:
    """
    GHG 연도별 값에서 이상치 제거.
    행 내 비율 기준: 같은 행의 중앙값 대비 500배 초과이면 제거.
    절대값 기준: 50,000,000(5천만 tCO2eq) 초과이면 제거.
    """
    for row in rows:
        yearly = row.get("연도별")
        if not isinstance(yearly, dict):
            continue
        vals = [v for v in yearly.values() if isinstance(v, (int, float))]
        if len(vals) < 2:
            continue
        try:
            median = statistics.median(vals)
        except statistics.StatisticsError:
            continue
        threshold_ratio = median * 500 if median > 0 else 0
        for year, val in list(yearly.items()):
            if not isinstance(val, (int, float)):
                continue
            if val > 50_000_000:
                logger.warning(f"GHG 이상치 제거(절대값): 부문={row.get('부문')}, {year}={val}")
                yearly[year] = None
            elif threshold_ratio > 0 and val > threshold_ratio:
                logger.warning(f"GHG 이상치 제거(비율): 부문={row.get('부문')}, {year}={val} (중앙값={median:.1f})")
                yearly[year] = None
    return rows


def _filter_strategy_code_duplicates(rows: list[dict]) -> list[dict]:
    """
    감축전략: 코드명('B1')과 코드+한글명('B1 기존 건물 ZEB 전환')이 같은 종류로 중복될 때
    코드명만 있는 행 제거.
    """
    # 코드+한글명 수집 (세부+종류 기준)
    full_names: set[tuple] = set()
    for r in rows:
        name = r.get("감축사업명", "")
        if not _is_code_only_name(name):
            full_names.add((r.get("지자체명", ""), r.get("감축사업명_세부", ""), r.get("종류", "")))

    result = []
    for r in rows:
        name = r.get("감축사업명", "")
        if _is_code_only_name(name):
            key = (r.get("지자체명", ""), r.get("감축사업명_세부", ""), r.get("종류", ""))
            if key in full_names:
                logger.debug(f"코드명 중복 제거: {name}")
                continue
        result.append(r)
    return result


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    # 중첩 dict: {"가정": 13043, "상업": 16251} → 합산
    if isinstance(val, dict):
        try:
            total = sum(
                float(v) for v in val.values()
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(",", "").replace("약 ", "").strip().replace(".", "").lstrip("-").isdigit())
            )
            return total if total != 0 else None
        except Exception:
            return None
    try:
        return float(str(val).replace(",", "").replace("약 ", "").strip())
    except (ValueError, TypeError):
        return None


def _ensure_all_years(yearly: dict) -> dict:
    for year in config.YEARS:
        yearly.setdefault(str(year), None)
    return yearly


def _deduplicate(rows: list[dict], key_fields: list[str]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(f, "") for f in key_fields)
        if key not in merged:
            merged[key] = deepcopy(row)
            if "연도별" in row:
                merged[key]["연도별"] = {}
        if "연도별" in row:
            for year in [str(y) for y in config.YEARS]:
                val = _to_float((row.get("연도별") or {}).get(year))
                if val is not None and merged[key]["연도별"].get(year) is None:
                    merged[key]["연도별"][year] = val
    return list(merged.values())


def _local_clean(raw: dict) -> dict:
    municipality = raw.get("municipality_name", "알 수 없음")

    # ── 자동차: 용도 정규화 + 중복 제거 ──
    vehicle_pre = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "용도": r.get("용도", ""),
            "차종": r.get("차종", ""),
            "대수": _to_float(r.get("대수")),
            "주행거리": _to_float(r.get("주행거리")),
        }
        for r in raw.get("vehicle", []) if isinstance(r, dict)
    ]
    vehicle = _deduplicate_vehicle(vehicle_pre)

    # ── 에너지: 합산 용도 필터링 ──
    energy_pre = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "용도": r.get("용도", ""),
            "석유_에너지유": _to_float(r.get("석유_에너지유")),
            "석유_LPG": _to_float(r.get("석유_LPG")),
            "석유_비에너지유": _to_float(r.get("석유_비에너지유")),
            "가스": _to_float(r.get("가스")),
            "전력": _to_float(r.get("전력")),
            "열": _to_float(r.get("열")),
            "신재생": _to_float(r.get("신재생")),
        }
        for r in raw.get("energy", []) if isinstance(r, dict)
    ]
    energy = _filter_energy_aggregates(energy_pre)

    # ── 온실가스: 부문 정규화 + 이상치 제거 + 중복 제거 ──
    ghg_raw = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "배출유형": _normalize(r.get("배출유형", "직접배출"), _TYPE_MAP),
            "종류": _normalize(r.get("종류", "현황"), _GHG_DATA_TYPE_MAP),
            "부문": _normalize_sector(r.get("부문", "")),
            "연도별": _ensure_all_years({
                str(k): _to_float(v) for k, v in (r.get("연도별") or {}).items()
            }),
        }
        for r in raw.get("ghg", []) if isinstance(r, dict)
        # 부문명이 표 제목처럼 긴 경우 제외 (정규화 후 빈 문자열이 될 것이지만 미리 필터)
    ]
    # 부문이 빈 문자열인 행(표 제목으로 오인된 경우) 제거
    ghg_raw = [r for r in ghg_raw if r.get("부문", "").strip()]
    ghg_raw = _filter_ghg_outliers(ghg_raw)
    ghg = _deduplicate(ghg_raw, ["지자체명", "배출유형", "종류", "부문"])

    # ── 감축전략: 코드명 중복 제거 + 중복 제거 ──
    strategy_raw = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "배출유형": _normalize(r.get("배출유형", "직접배출"), _TYPE_MAP),
            "감축전략_부문": _normalize_sector(r.get("감축전략_부문", "")),
            "감축사업명": r.get("감축사업명", ""),
            "감축사업명_세부": r.get("감축사업명_세부", ""),
            "구분": r.get("구분", "공통"),
            "성과지표": r.get("성과지표", ""),
            "종류": r.get("종류", ""),
            "연도별": _ensure_all_years({
                str(k): _to_float(v) for k, v in (r.get("연도별") or {}).items()
            }),
        }
        for r in raw.get("strategy", []) if isinstance(r, dict)
    ]
    strategy_raw = _filter_strategy_code_duplicates(strategy_raw)
    strategy = _deduplicate(strategy_raw, ["지자체명", "감축사업명", "감축사업명_세부", "종류"])

    seen_items: set[str] = set()
    summary = []
    for r in raw.get("summary", []):
        if not isinstance(r, dict):
            continue
        item = r.get("항목", "")
        if item in seen_items:
            continue
        seen_items.add(item)
        summary.append({
            "지자체명": r.get("지자체명") or municipality,
            "항목": item,
            "내용": r.get("내용", ""),
            "근거": r.get("근거", ""),
        })

    # 누락된 요약카드 항목 채우기
    for item in config.SUMMARY_ITEMS:
        if item not in seen_items:
            summary.append({"지자체명": municipality, "항목": item, "내용": "", "근거": ""})

    return {
        "municipality_name": municipality,
        "vehicle": vehicle, "energy": energy,
        "ghg": ghg, "strategy": strategy, "summary": summary,
    }


def _llm_enhance_summary(cleaned: dict, full_text_excerpt: str) -> dict:
    empty_items = [s for s in cleaned["summary"] if not s.get("내용")]
    if not empty_items:
        return cleaned

    import json
    empty_names = [s["항목"] for s in empty_items]
    ghg_sample = json.dumps(cleaned["ghg"][:3], ensure_ascii=False)

    prompt = f"""지자체명: {cleaned['municipality_name']}

다음은 보고서 텍스트 일부와 GHG 데이터 샘플입니다.
아래 요약카드 항목들의 내용을 작성해 주세요.

필요한 항목: {', '.join(empty_names)}

GHG 데이터:
{ghg_sample}

보고서 텍스트:
{full_text_excerpt[:4000]}

다음 JSON 형식으로 반환하세요:
{{
  "summary_updates": [
    {{"항목": "항목명", "내용": "내용", "근거": "보고서 내 근거"}}
  ]
}}"""

    resp = llm_client.call_text(prompt, system=ORGANIZER_SYSTEM)
    parsed = llm_client.parse_json(resp)
    for update in parsed.get("summary_updates", []):
        for row in cleaned["summary"]:
            if row["항목"] == update.get("항목") and not row["내용"]:
                row["내용"] = update.get("내용", "")
                row["근거"] = update.get("근거", "")
    return cleaned


class OrganizerAgent:
    """에이전트 3: 정리·정제 에이전트"""

    def __init__(self):
        self._final_data: dict = {}

    def organize(self, raw_data: dict, full_text_excerpt: str = "") -> dict:
        print("[에이전트3 정리] 로컬 정제 시작...")
        cleaned = _local_clean(raw_data)
        counts = {k: len(v) for k, v in cleaned.items() if isinstance(v, list)}
        print(f"[에이전트3 정리] 로컬 정제 완료: {counts}")

        empty_summary = [s for s in cleaned["summary"] if not s.get("내용")]
        if empty_summary and full_text_excerpt:
            print(f"[에이전트3 정리] 요약카드 빈 항목 {len(empty_summary)}개 LLM 보완 중...")
            cleaned = _llm_enhance_summary(cleaned, full_text_excerpt)
            print("[에이전트3 정리] 요약카드 보완 완료")

        self._final_data = cleaned
        return cleaned

    def get_excel_ready(self) -> dict:
        d = self._final_data
        return {k: d.get(k, []) for k in ["vehicle", "energy", "ghg", "strategy", "summary"]}

    def report(self) -> str:
        d = self._final_data
        counts = {k: len(v) for k, v in d.items() if isinstance(v, list)}
        return (
            f"[에이전트3 정리] 정제 완료\n"
            f"  - 지자체명: {d.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
