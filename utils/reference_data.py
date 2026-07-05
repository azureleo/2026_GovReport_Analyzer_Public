"""가이드라인 부록 참조 CSV 로더와 결정론적 매칭 유틸리티."""

from __future__ import annotations

import csv
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import config

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APPENDIX4_PATH = _PROJECT_ROOT / "data" / "appendix4_projects.csv"
_APPENDIX3_PATH = _PROJECT_ROOT / "data" / "appendix3_reduction_units.csv"

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


_CODEBOOK_DEFINITIONS = {
    "달성여부": [
        ("달성", "목표 또는 계획을 완료한 상태"),
        ("정상추진", "일정과 목표 대비 정상적으로 추진 중인 상태"),
        ("지연", "일부 일정 또는 성과가 지연된 상태"),
        ("미달성", "목표 달성에 실패했거나 중단된 상태"),
    ],
    "사업유형": [
        ("기존", "전년도 또는 이전 계획부터 계속 추진하는 사업"),
        ("변경", "내용·일정·예산 등이 변경된 사업"),
        ("신규", "해당 계획에서 새로 추진하는 사업"),
    ],
    "전망방법": [
        ("stat_time_series", "시계열 분석"),
        ("stat_regression", "회귀분석"),
        ("stat_growth_rate", "증가율 분석"),
        ("bottom_up_accounting_LEAP", "LEAP 상향식 회계 모형"),
        ("bottom_up_optimization_MARKAL", "MARKAL 상향식 최적화 모형"),
        ("bottom_up_hybrid_MARKAL_MACRO", "MARKAL-MACRO 하이브리드 모형"),
        ("bottom_up_simulation_ENPEP", "ENPEP 상향식 시뮬레이션 모형"),
    ],
    "표준부문": [
        ("건물", "건물 부문"),
        ("수송", "수송 부문"),
        ("농축산", "농축산 부문"),
        ("폐기물", "폐기물 부문"),
        ("흡수원", "흡수원 부문"),
        ("전환", "전환 부문"),
        ("산업", "산업 부문"),
        ("수소", "수소 부문"),
    ],
    "데이터상태": [
        ("reported", "원문 표·문장에 보고된 값"),
        ("visual_only", "시각자료 판독으로만 확보한 값"),
        ("gap_fill", "빈칸 보완 단계에서 보강한 값"),
        ("calculated", "organizer가 산식으로 계산·교정한 값"),
        ("conflicting", "중복·상충 검수 대상 값"),
    ],
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


@lru_cache(maxsize=1)
def load_appendix4_projects() -> tuple[dict[str, str], ...]:
    """부록4 사업목록 CSV를 읽는다. 파일이 없으면 빈 튜플로 no-op."""
    return tuple(_read_csv(_APPENDIX4_PATH))


@lru_cache(maxsize=1)
def load_appendix3_units() -> tuple[dict[str, str], ...]:
    """부록3 감축원단위 CSV를 읽는다. 파일이 없으면 빈 튜플로 no-op."""
    return tuple(_read_csv(_APPENDIX3_PATH))


def normalise_key_text(value: Any) -> str:
    """시트 dedup·참조사전 매칭이 공유하는 텍스트 정규화."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("ㆍ", "·").replace("･", "·").replace("ᆞ", "·").replace("・", "·")
    return re.sub(r"[\s\u00a0]+", " ", text).strip().casefold()


def normalise_match_text(value: Any) -> str:
    """사업명 매칭 전용 정규화: 공백 제거, 괄호 기호 제거."""
    text = normalise_key_text(value).replace(" ", "")
    text = re.sub(r"[()（）\[\]{}<>〈〉《》【】]", "", text)
    return text


def _bigrams(text: str) -> set[str]:
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _jaccard(left: str, right: str) -> float:
    left_set = _bigrams(left)
    right_set = _bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def match_reference_name(
    query: Any,
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    name_key: str,
    threshold: float,
) -> dict[str, Any] | None:
    """완전/포함/bigram Jaccard 규칙으로 가장 좋은 후보를 반환한다."""
    query_key = normalise_match_text(query)
    if not query_key:
        return None

    best: dict[str, Any] | None = None
    for index, row in enumerate(candidates):
        candidate_key = normalise_match_text(row.get(name_key, ""))
        if not candidate_key:
            continue
        confidence = ""
        score = 0.0
        if query_key == candidate_key:
            confidence = "high"
            score = 1.0
        elif query_key in candidate_key or candidate_key in query_key:
            confidence = "medium"
            score = min(len(query_key), len(candidate_key)) / max(len(query_key), len(candidate_key))
        else:
            score = _jaccard(query_key, candidate_key)
            if score >= threshold:
                confidence = "low"
        if not confidence:
            continue
        candidate = {"row": row, "confidence": confidence, "score": score, "index": index}
        if best is None:
            best = candidate
            continue
        current_rank = _CONFIDENCE_RANK[confidence]
        best_rank = _CONFIDENCE_RANK[best["confidence"]]
        if (current_rank, score, -index) > (best_rank, best["score"], -best["index"]):
            best = candidate
    return best


def match_appendix4_project(project_name: Any, threshold: float | None = None) -> dict[str, Any] | None:
    threshold = getattr(config, "APPENDIX4_MATCH_THRESHOLD", 0.55) if threshold is None else threshold
    return match_reference_name(project_name, load_appendix4_projects(), name_key="사업명", threshold=threshold)


def _split_multi_value(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _select_appendix3_measure(row: dict[str, str], monitoring_factor: Any = "") -> dict[str, str]:
    monitors = _split_multi_value(row.get("모니터링인자", ""))
    values = _split_multi_value(row.get("원단위값", ""))
    units = _split_multi_value(row.get("단위", ""))
    if not monitors:
        return row

    index = 0
    monitor_key = normalise_match_text(monitoring_factor)
    if monitor_key:
        for i, monitor in enumerate(monitors):
            candidate_key = normalise_match_text(monitor)
            if monitor_key == candidate_key or monitor_key in candidate_key or candidate_key in monitor_key:
                index = i
                break
    selected = dict(row)
    selected["모니터링인자"] = monitors[min(index, len(monitors) - 1)]
    if values:
        selected["원단위값"] = values[min(index, len(values) - 1)]
    if units:
        selected["단위"] = units[min(index, len(units) - 1)]
    return selected


def _pick_appendix3_unit(match: dict[str, Any], monitoring_factor: Any) -> dict[str, Any]:
    return _select_appendix3_measure(match["row"], monitoring_factor)


def match_appendix3_unit(project_name: Any, monitoring_factor: Any = "", threshold: float | None = None) -> dict[str, Any] | None:
    threshold = getattr(config, "APPENDIX3_MATCH_THRESHOLD", 0.55) if threshold is None else threshold
    match = match_reference_name(project_name, load_appendix3_units(), name_key="감축사업명", threshold=threshold)
    if match is None:
        return None
    row = _pick_appendix3_unit(match, monitoring_factor)
    return {**match, "row": row}


def find_appendix3_unit_by_id(unit_id: Any, monitoring_factor: Any = "") -> dict[str, str] | None:
    target = str(unit_id or "").strip()
    if not target:
        return None
    for row in load_appendix3_units():
        if str(row.get("번호", "")).strip() == target:
            return _select_appendix3_measure(row, monitoring_factor)
    return None


def build_codebook_rows() -> list[dict[str, str]]:
    """90_코드북 시트에 쓸 정적 코드북 행을 만든다."""
    rows: list[dict[str, str]] = []
    for code_type, entries in _CODEBOOK_DEFINITIONS.items():
        for code, definition in entries:
            rows.append({
                "코드유형": code_type,
                "코드": code,
                "라벨": code,
                "정의": definition,
                "비고": "가이드라인·파이프라인 표준 코드",
            })
    return rows
