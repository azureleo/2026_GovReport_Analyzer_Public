"""골든셋 채점기의 고정 계약과 공용 정규화 도우미."""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from agents.organizer_agent import (  # noqa: E402
    dedup_key_text,
    normalize_direct_indirect_type,
    normalize_project_id,
    normalize_provenance_pages,
    to_float,
)

무시열 = {"근거ID", "출처페이지", "데이터상태", "derivation_type"}
골든열 = [
    "골든_출처유형", "골든_출처페이지", "골든_채점제외", "골든_비고",
    "골든_산출유형",
]
산출유형목록 = [
    "reported", "explicit", "normalized", "calculated", "inferred", "external_lookup",
]
# 2026-08 스키마 확장 전 골든셋도 계속 평가할 수 있도록 허용하는 후방 호환 열.
선택확장열 = {
    "06_감축목표": {"기준배출량기준", "목표배출량기준", "감축률계산값"},
    "10_정량감축량": {"감축량유형", "시간기준"},
    "12_대응기반강화": {
        "평가유형", "기후변수", "시나리오", "기준기간", "미래기간", "공간단위",
        "부문", "리스크항목", "취약성지표", "값", "단위", "리스크등급",
        "방법론", "자료출처", "연계적응과제",
    },
    "14_점검실적": {"예산액", "예산유형", "예산단위", "예산집행률"},
}
출처유형목록 = ["본문텍스트", "텍스트표", "이미지표", "그래프", "이미지"]
텍스트유래 = {"본문텍스트", "텍스트표"}
시각유래 = {"이미지표", "그래프", "이미지"}
수치시트 = {"02_지역여건", "03_배출현황_지역", "04_배출현황_관리권한", "05_배출전망", "06_감축목표", "09_연차별이행계획", "10_정량감축량", "11_재정투자계획", "14_점검실적"}
의미완화적용시트 = {
    "01_계획개요",
    "02_지역여건",
    "07_비전전략",
    "13_이행관리환류",
}
SEMANTIC_MATCH_MIN_JACCARD = 0.6
CHAR_SIMILARITY_MIN_JACCARD = 0.30
값필드 = {
    "02_지역여건": ["값", "단위"],
    "03_배출현황_지역": ["배출량", "단위"],
    "04_배출현황_관리권한": ["배출량", "단위"],
    "05_배출전망": ["전망값", "단위"],
    "06_감축목표": ["기준배출량", "배출전망", "목표감축량", "목표배출량", "감축률(%)", "감축률계산값"],
    "09_연차별이행계획": ["목표물량"],
    "10_정량감축량": ["활동량", "예상감축량", "감축량유형", "시간기준"],
    "11_재정투자계획": ["예산액", "예산단위"],
    "12_대응기반강화": ["값", "단위", "리스크등급"],
    "14_점검실적": ["예산액", "예산유형", "예산단위", "예산집행률", "달성여부"],
}
텍스트값필드 = {
    "단위", "예산단위", "달성여부", "감축량유형", "시간기준",
    "기준배출량기준", "목표배출량기준", "예산유형", "리스크등급",
}
데이터시트 = [name for name in config.EXCEL_HEADERS if name[:2].isdigit() and int(name[:2]) <= 15]
일차키 = {
    "01_계획개요": ["개요유형", "항목명"],
    "02_지역여건": ["지표범주", "지표명", "연도"],
    "03_배출현황_지역": ["배출유형", "부문", "세부부문", "연도"],
    "04_배출현황_관리권한": ["관리부문", "세부부문", "직간접구분", "연도"],
    "05_배출전망": ["시나리오", "부문", "연도"],
    "06_감축목표": [
        "목표수준",
        "목표범위",
        "부문",
        "기준연도",
        "목표연도",
    ],
    "07_비전전략": ["전략수준", "전략명"],
    "08_감축사업목록": ["관리번호"],
    "09_연차별이행계획": ["관리번호", "연도"],
    "10_정량감축량": ["관리번호", "연도", "모니터링인자"],
    "11_재정투자계획": ["계획구분", "부문", "사업명", "재원구분", "연도"],
    "13_이행관리환류": ["거버넌스기구", "절차단계"],
    "14_점검실적": ["점검연도", "관리번호"],
    "15_변경과제_조치": ["점검연도", "관리번호"],
}
이차키 = {
    "01_계획개요": ["항목명"],
    "02_지역여건": ["지표명", "연도"],
    "03_배출현황_지역": ["배출유형", "부문", "연도"],
    "04_배출현황_관리권한": ["관리부문", "직간접구분", "연도"],
    "05_배출전망": ["부문", "연도"],
    "06_감축목표": ["부문", "목표연도"],
    "07_비전전략": ["전략명"],
    "08_감축사업목록": ["사업명"],
    "09_연차별이행계획": ["사업명", "연도"],
    "10_정량감축량": ["사업명", "연도"],
    "11_재정투자계획": ["부문", "재원구분", "연도"],
    "12_대응기반강화": ["과제명"],
    "13_이행관리환류": ["거버넌스기구"],
    "14_점검실적": ["점검연도", "사업명"],
    "15_변경과제_조치": ["사업명"],
}


@dataclass(frozen=True, slots=True)
class 행:
    번호: int
    값: dict[str, Any]
    출처유형: str = ""
    출처페이지: str = ""


@dataclass(frozen=True, slots=True)
class 시트자료:
    이름: str
    헤더: list[str]
    행들: list[행]
    상태: str = "정상"
    오류: list[str] = field(default_factory=list)
    제외행수: int = 0
    경고: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class 매칭:
    골든: 행
    출력: 행
    방식: str
    키: str


@dataclass(slots=True)
class 값집계:
    전체: int = 0
    일치: int = 0
    골든만: int = 0
    출력만: int = 0

    def 비율(self) -> float | None:
        if self.전체 == 0:
            return None
        return round(self.일치 / self.전체, 4)


@dataclass(slots=True)
class 출처집계:
    골든행: int = 0
    매칭행: int = 0
    값전체: int = 0
    값일치: int = 0

    def 리콜(self) -> float | None:
        if self.골든행 == 0:
            return None
        return round(self.매칭행 / self.골든행, 4)

    def 값일치율(self) -> float | None:
        if self.값전체 == 0:
            return None
        return round(self.값일치 / self.값전체, 4)


def 계약헤더(sheet_name: str) -> list[str]:
    return [header for header in config.EXCEL_HEADERS[sheet_name] if header not in 무시열]


def 레거시계약헤더(sheet_name: str) -> list[str]:
    optional = 선택확장열.get(sheet_name, set())
    return [header for header in 계약헤더(sheet_name) if header not in optional]


def 산출유형그룹(value: Any) -> str:
    """골든·출력의 산출 유형을 평가용 공통 그룹으로 정규화한다."""
    normalized = 문자열(value).casefold()
    if normalized in {"", "reported", "explicit"}:
        return "reported"
    if normalized in {"normalized", "calculated", "inferred", "external_lookup"}:
        return normalized
    return "inferred"


def 값있음(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def 문자열(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def 연도문자열(value: Any) -> str:
    parsed = to_float(value)
    if parsed is not None and parsed.is_integer():
        return str(int(parsed))
    return dedup_key_text(value)


def 키값(field_name: str, value: Any) -> str:
    if field_name == "관리번호":
        return normalize_project_id(value).replace("-", "")
    if field_name == "직간접구분":
        return dedup_key_text(normalize_direct_indirect_type(value))
    if field_name.endswith("연도") or field_name == "연도":
        return 연도문자열(value)
    return dedup_key_text(value)


def 키(row: 행, sheet_name: str, relaxed: bool) -> str | None:
    fields = 이차키.get(sheet_name) if relaxed else 일차키.get(sheet_name)
    if not relaxed:
        if sheet_name == "08_감축사업목록":
            project_id = 키값("관리번호", row.값.get("관리번호"))
            fields = ["관리번호"] if project_id else ["부문", "사업명"]
        elif sheet_name == "12_대응기반강화":
            task_id = 키값("과제ID", row.값.get("과제ID"))
            fields = ["과제ID"] if task_id else ["대응기반영역", "과제명"]
    if not fields:
        return None
    parts: list[str] = []
    for field_name in fields:
        normalized = 키값(field_name, row.값.get(field_name))
        if not normalized:
            return None
        parts.append(f"{field_name}={normalized}")
    return "|".join(parts)


def 페이지집합(value: Any) -> set[int]:
    normalized = normalize_provenance_pages(value)
    return {int(part) for part in normalized.split(",") if part}


def 경로라벨(label: str | None) -> str:
    raw = label or time.strftime("%Y%m%d_%H%M%S")
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", raw).strip("_") or time.strftime("%Y%m%d_%H%M%S")
