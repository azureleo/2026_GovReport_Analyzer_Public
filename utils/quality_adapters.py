"""저장된 업무 셀과 서로 다른 진단 형식을 평가용으로만 정규화한다."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import openpyxl
from openpyxl.utils import get_column_letter
import config

VERSION = "saved-quality-v1"


def present(value):
    return value is not None and str(value).strip() != ""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def norm(value):
    return re.sub(r"\s+", "", str(value if value is not None else "")).casefold()


def unit_norm(value):
    # 아래첨자·공백·표기 끝 마침표만 접는다. 활동량/CO2/CO2eq 차원은 유지한다.
    text = norm(unicodedata.normalize("NFKC", str(value if value is not None else "")))
    if (text.startswith("(") and text.endswith(")")) or (text.startswith("[") and text.endswith("]")):
        text = text[1:-1]
    return text.removeprefix("단위:").rstrip(".")


def number(value):
    if isinstance(value, bool) or not present(value):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def pages(value):
    """범위/배열 표기를 허용하되 잘못된 토큰을 조용히 제거하지 않는다."""
    if not present(value):
        return [], None
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            parsed, error = pages(item)
            if error:
                return [], error
            result.extend(parsed)
        return sorted(set(result)), None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ([int(value)], None) if float(value).is_integer() else ([], "정수가 아닌 페이지")
    text = str(value).strip()
    if text.startswith("["):
        try:
            return pages(json.loads(text))
        except ValueError:
            return [], "페이지 배열 파싱 실패"
    if not re.fullmatch(r"[\d\s,;~\-–]+", text):
        return [], "지원하지 않는 페이지 표기"
    result = []
    for token in re.split(r"[,;\s]+", text):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)[~\-–](\d+)", token)
        if match:
            start, end = map(int, match.groups())
            if start > end or end - start > 10000:
                return [], "잘못된 페이지 범위"
            result.extend(range(start, end + 1))
        elif token.isdigit():
            result.append(int(token))
        else:
            return [], "잘못된 페이지 토큰"
    return sorted(set(result)), None


def ids(row):
    result = set()
    for field in ("객체ID", "근거ID", "object_id", "evidence_id", "object_ids", "evidence_ids"):
        value = row.get(field)
        if not present(value):
            continue
        if isinstance(value, str) and value.startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        result.update(str(v) for v in (value if isinstance(value, (list, tuple, set)) else [value]) if present(v))
    return result


def metric(numerator, denominator, scope, *, reason=None):
    available = denominator is not None and denominator > 0 and numerator is not None
    return {"value": round(numerator / denominator, 6) if available else None,
            "numerator": numerator, "denominator": denominator, "scope": scope,
            "available": available, "unavailable_reason": None if available else (reason or "분모 없음 / 평가 대상 없음"),
            "rule_version": VERSION}


def issue(rule, sheet="", row=None, field=None, *, certainty="suspected", severity="warning",
          message="", evidence=None, action="원문과 해당 셀을 확인하세요"):
    name = config.SHEET_KEY_TO_NAME.get(sheet, sheet)
    headers = config.EXCEL_HEADERS.get(name, [])
    cell = f"{get_column_letter(headers.index(field) + 1)}{row}" if row and field in headers else None
    return {"rule_id": rule, "severity": severity, "certainty": certainty,
            "sheet_key": sheet, "sheet": name, "excel_row": row, "field": field, "cell": cell,
            "message": message, "evidence": evidence, "action": action, "rule_version": VERSION}


def read_saved_workbook(path):
    """16 업무 시트만 읽는다. 빈 행은 제외하되 실제 Excel 행 번호는 보존한다."""
    data, diagnostics = {}, []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        for key in config.EXTRACTION_SHEETS:
            name = config.SHEET_KEY_TO_NAME[key]
            data[key] = []
            if name not in wb.sheetnames:
                diagnostics.append(issue("storage.sheet_missing", key, certainty="confirmed", severity="error"))
                continue
            ws = wb[name]
            iterator = ws.iter_rows(values_only=True)
            headers = list(next(iterator, ()))
            if len(headers) != len(set(headers)):
                diagnostics.append(issue("storage.duplicate_header", key, certainty="confirmed", severity="error"))
            for field in config.EXCEL_HEADERS[name]:
                if field not in headers:
                    diagnostics.append(issue("storage.header_missing", key, 1, field, certainty="confirmed", severity="error"))
            for n, values in enumerate(iterator, 2):
                if not any(present(v) for v in values):
                    continue
                row = dict(zip(headers, values))
                row["_excel_row"] = n
                data[key].append(row)
    finally:
        wb.close()
    return data, diagnostics


def excel_value(value):
    """현재 writer의 직렬화 계약. 모델/업무 데이터는 변경하지 않는다."""
    if isinstance(value, (dict, list, tuple, set)):
        value = json.dumps(list(value) if isinstance(value, set) else value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        value = "Y" if value else "N"
    if isinstance(value, str):
        value = "".join(c for c in value if ord(c) >= 32 or c in "\n\r\t")[:32767]
        if value.startswith(("=", "+", "-", "@")):
            value = "'" + value
    return None if value == "" else value


def compare_storage(saved, expected):
    diagnostics, checked, matched = [], 0, 0
    if expected is None:
        return diagnostics, metric(None, None, "메모리→실제 Excel 업무 셀", reason="저장 전 스냅샷 미제공")
    for key in config.EXTRACTION_SHEETS:
        headers = config.EXCEL_HEADERS[config.SHEET_KEY_TO_NAME[key]]
        memory = [r for r in expected.get(key, []) if any(present(excel_value(r.get(f))) for f in headers)]
        actual = saved[key]
        if len(memory) != len(actual):
            diagnostics.append(issue("storage.row_count", key, certainty="confirmed", severity="error",
                                     evidence={"expected": len(memory), "saved": len(actual)}))
        for n in range(max(len(memory), len(actual))):
            left = memory[n] if n < len(memory) else {}
            right = actual[n] if n < len(actual) else {}
            for field in headers:
                a, b = excel_value(left.get(field)), right.get(field)
                checked += 1
                equal = a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                   and math.isclose(a, b, rel_tol=1e-14, abs_tol=0))
                matched += equal
                if not equal:
                    diagnostics.append(issue("storage.cell_mismatch", key, right.get("_excel_row", n + 2), field,
                        certainty="confirmed", severity="error", evidence={"expected": a, "saved": b}))
    return diagnostics, metric(matched, checked, "메모리→실제 Excel 업무 셀 (writer 직렬화 기준)")


def adapt_warnings(data):
    """출처가 불분명한 과거 경고를 확정 오답으로 승격하지 않는다."""
    result = []
    for record in data.get("validation_report", []):
        sheet = record.get("대상시트키") or record.get("대상시트") or record.get("영역", "")
        sheet = {v: k for k, v in config.SHEET_KEY_TO_NAME.items()}.get(sheet, sheet)
        message = record.get("문제내용") or record.get("사유") or record.get("설명") or record.get("내용", "")
        raw_severity = record.get("심각도") or record.get("severity")
        severity = {"오류": "error", "경고": "warning", "정보": "info"}.get(raw_severity, raw_severity)
        certainty = record.get("certainty", "suspected")
        result.append(issue("upstream." + str(record.get("rule_id") or record.get("항목") or "diagnostic"), sheet,
            record.get("excel_row"), record.get("field"),
            severity=severity if severity in {"error", "warning", "info"} else "warning",
            certainty=certainty if certainty in {"confirmed", "suspected", "unassessed"} else "unassessed", message=str(message),
            evidence={"pages": record.get("출처페이지"), "ids": sorted(ids(record))}))
    return result


def deduplicate_issues(records):
    unique = {}
    for item in records:
        # 서술만 같은 익명 충돌은 경고 기록 수만 합친다. 영향 행 수를 추측하지 않는다.
        fingerprint = canonical({k: item.get(k) for k in ("rule_id", "sheet_key", "excel_row", "field", "evidence", "message")})
        if fingerprint not in unique:
            unique[fingerprint] = {**item, "issue_id": hashlib.sha256(fingerprint.encode()).hexdigest()[:20], "occurrences": 0}
        ranking = {"info": 0, "warning": 1, "error": 2}
        if ranking[item["severity"]] > ranking[unique[fingerprint]["severity"]]:
            unique[fingerprint]["severity"] = item["severity"]
        unique[fingerprint]["occurrences"] += 1
    return list(unique.values())
