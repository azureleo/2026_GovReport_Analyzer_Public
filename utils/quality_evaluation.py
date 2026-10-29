"""실제 저장 결과의 결정적 검사. 정확도 채점이나 자동 수정/재호출은 수행하지 않는다."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re

import config
from utils.integration_audit import document_hash, save_json
from utils.quality_adapters import (VERSION, adapt_warnings, canonical, compare_storage, deduplicate_issues,
    ids, issue, metric, norm, number, pages, present, read_saved_workbook, unit_norm)

REQUIRED = {
    "document_meta": ["지자체명", "계획명", "계획시작연도", "계획종료연도"],
    "plan_overview": ["개요유형", "항목명", "항목값"],
    "regional_conditions": ["지표명", "값"],
    "emissions_regional": ["배출유형", "부문", "연도", "배출량", "단위"],
    "emissions_management": ["관리부문", "직간접구분", "연도", "배출량", "단위"],
    "emissions_forecast": ["시나리오", "연도", "전망값", "단위"],
    "reduction_targets": ["목표수준", "기준연도", "목표연도"],
    "vision_strategy": ["전략수준"], "mitigation_projects": ["사업명", "부문"],
    "annual_implementation": ["사업명"],
    "quantitative_reductions": ["사업명", "연도", "예상감축량", "단위"],
    "financial_plan": ["사업명", "연도", "예산액", "예산단위"],
    "foundation_measures": ["대응기반영역", "과제명"],
    "governance_feedback": ["거버넌스기구", "역할"],
    "monitoring_performance": ["점검연도", "사업명", "이행실적"],
    "changes_actions": ["점검연도", "사업명"],
}
ALTERNATIVES = {
    "vision_strategy": ["전략명", "비전문구"],
    "annual_implementation": ["연간계획", "목표물량"],
    "reduction_targets": ["목표감축량", "목표배출량", "감축률(%)"],
    "changes_actions": ["변경사유", "지연미달성사유", "조치계획"],
}
MONEY_SCALES = {"원": 1, "천원": 1000, "만원": 10000, "백만원": 1000000, "억원": 100000000,
                "조원": 1000000000000, "krw": 1, "천원단위": 1000, "백만 원": 1000000}
NON_MONEY = {"tco2eq", "천톤co2eq", "톤co2eq", "만톤", "tco2", "개", "개소", "개소(가구)", "가구", "일", "%", "명", "대", "kwh", "mwh"}
NUMERIC_FIELDS = {"배출량", "전망값", "기준배출량", "목표배출량", "목표감축량", "예상감축량", "예산액", "감축률(%)"}
EMISSION_SHEETS = {"emissions_regional", "emissions_management", "emissions_forecast", "quantitative_reductions"}
IDENTITIES = {
    "financial_plan": ["지자체명", "계획구분", "부문", "사업명", "재원구분", "연도"],
    "quantitative_reductions": ["지자체명", "사업명", "관리번호", "연도", "모니터링인자"],
    "emissions_regional": ["지자체명", "인벤토리출처", "배출범위", "배출유형", "부문", "세부부문", "연도"],
    "emissions_management": ["지자체명", "인벤토리출처", "관리부문", "세부부문", "직간접구분", "연도"],
    "emissions_forecast": ["지자체명", "전망범위", "시나리오", "부문", "세부부문", "연도"],
}
CITY_ALIASES = ({"서울", "서울시", "서울특별시"}, {"경기", "경기도"}, {"강원도", "강원특별자치도"},
                {"부산", "부산시", "부산광역시"}, {"인천", "인천시", "인천광역시"})


def money_scale(value):
    text = unit_norm(value).replace("₩", "원").replace("(krw)", "")
    return {norm(k): v for k, v in MONEY_SCALES.items()}.get(text)


def same_city(left, right):
    a, b = norm(left), norm(right)
    return a == b or any(a in aliases and b in aliases for aliases in CITY_ALIASES)


def period_row(row):
    """명시된 합계/기간만 허용한다. 공란 연도를 임의 합계로 해석하지 않는다."""
    for field in ("연도", "기간", "기간구분", "집계구분", "시간구분", "연도원문"):
        text = str(row.get(field, ""))
        if re.search(r"합계|총계|기간총|누계|전체기간", text) or re.search(r"\d{4}\s*[~–-]\s*\d{4}", text):
            return True
    return present(row.get("기간시작")) and present(row.get("기간종료"))


def fact_values(sheet, row):
    """값 충돌은 명시된 차원과 배율까지 포함해 판정한다."""
    values = {f: row.get(f) for f in NUMERIC_FIELDS if f in row}
    if sheet == "financial_plan":
        scale, amount = money_scale(row.get("예산단위")), number(row.get("예산액"))
        values["예산액"] = round(amount * scale, 8) if scale is not None and amount is not None else row.get("예산액")
        values["dimension"] = "KRW" if scale is not None else norm(row.get("예산단위"))
    else:
        values["dimension"] = unit_norm(row.get("단위"))
    return canonical(values)


def _source_maps(document, objects):
    texts = {}
    if document is not None:
        for p in document.pages:
            texts[p.page_number] = norm(p.text + "\n" + "\n".join(p.tables))
    lookup = defaultdict(set)
    for obj in objects or []:
        item = obj.to_dict() if hasattr(obj, "to_dict") else obj
        for identifier in (item.get("object_id"), item.get("metadata", {}).get("evidence_id")):
            if identifier:
                lookup[str(identifier)].add(item.get("page_number", item.get("page")))
    return texts, lookup


def evaluate_saved_excel(path, *, expected=None, diagnostics=None, document=None, objects=None,
                         municipality=None, coverage=None):
    saved, findings = read_saved_workbook(path)
    storage_issues, storage_metric = compare_storage(saved, expected)
    findings.extend(storage_issues)
    findings.extend(adapt_warnings(diagnostics or {}))
    texts, object_ids = _source_maps(document, objects)
    page_count = len(document.pages) if document is not None else None
    # 메타 행의 다수결로 지역을 추측하지 않고 호출 측의 문서 문맥을 사용한다.
    counts = Counter()
    refs = []
    for key, rows in saved.items():
        seen, fact_groups = {}, defaultdict(list)
        for row in rows:
            n = row["_excel_row"]
            counts["rows"] += 1
            refs.append((key, n))
            def add(rule, field=None, *, certainty="suspected", severity="warning", message="", evidence=None):
                findings.append(issue(rule, key, n, field, certainty=certainty, severity=severity,
                                      message=message, evidence=evidence))
            requirements = list(REQUIRED[key])
            if period_row(row):
                requirements = [f for f in requirements if f != "연도"]
            for field in requirements:
                counts["required"] += 1
                if present(row.get(field)):
                    counts["filled"] += 1
                else:
                    add("required.missing", field, certainty="confirmed", severity="error", message="행 유형별 필수 필드 공란")
            alternatives = ALTERNATIVES.get(key)
            if alternatives and not any(present(row.get(f)) for f in alternatives):
                add("required.one_of", message="최소 한 필드 필요: " + ", ".join(alternatives), certainty="confirmed", severity="error")
            for field, value in row.items():
                if not present(value):
                    continue
                if field in NUMERIC_FIELDS and number(value) is None:
                    add("type.numeric", field, certainty="confirmed", severity="error", evidence=value)
                if field.endswith("연도") or field in {"기간시작", "기간종료"}:
                    if field == "연도" and period_row({field: value}):
                        continue
                    years = re.findall(r"\d{4}", str(value))
                    # 복수 목표연도는 현재 계약에서 허용한다.
                    valid = years and all(1800 <= int(y) <= 2200 for y in years)
                    if len(years) > 1 and field != "목표연도":
                        valid = False
                    residue = re.sub(r"\d{4}(?:\.0)?", "", str(value))
                    valid = valid and not re.sub(r"[\s,;/\[\]년~–-]", "", residue)
                    if not valid:
                        add("time.invalid_year", field, certainty="confirmed", severity="error", evidence=value)
            for start, end in (("계획시작연도", "계획종료연도"), ("기간시작", "기간종료"), ("기준연도", "목표연도")):
                a, b = number(row.get(start)), number(row.get(end))
                if a is not None and b is not None and a > b:
                    add("time.reversed_period", end, certainty="confirmed", severity="error", evidence={start: a, end: b})
            if municipality and present(row.get("지자체명")) and not same_city(municipality, row["지자체명"]):
                add("context.municipality_mismatch", "지자체명", message="문서 지역과 다름. 타지역 비교자료 여부 확인", evidence={"document": municipality, "row": row["지자체명"]})
            if key == "financial_plan" and present(row.get("예산단위")):
                unit = unit_norm(row["예산단위"])
                if money_scale(unit) is None:
                    known_bad = unit in NON_MONEY or "co2" in unit
                    unstated = unit in {"원문에단위표기없음", "단위미상", "미상", "미표기", "단위미표기", "알수없음"}
                    add("budget.non_money_unit" if known_bad else "budget.unit_unassessed" if unstated else "budget.unknown_unit", "예산단위",
                        certainty="confirmed" if known_bad else "unassessed" if unstated else "suspected",
                        severity="error" if known_bad else "warning", evidence=row["예산단위"])
            if key in EMISSION_SHEETS and present(row.get("단위")):
                unit = unit_norm(row["단위"])
                if not re.fullmatch(r"(?:kg|t|kt|mt|톤|천톤|만톤|백만톤)co2(?:eq|e)?", unit):
                    add("emissions.unit_dimension", "단위", message="배출/감축량 단위로 확인되지 않음", evidence=row["단위"])
            parsed_pages, page_error = pages(row.get("출처페이지"))
            if page_error or any(p < 1 or (page_count is not None and p > page_count) for p in parsed_pages):
                add("provenance.invalid_page", "출처페이지", certainty="confirmed", severity="error", evidence=row.get("출처페이지"), message=page_error or "원문 페이지 범위 밖")
            elif parsed_pages:
                counts["pages_present"] += 1
            else:
                add("provenance.page_unassessed", "출처페이지", certainty="unassessed", message="출처페이지가 없어 원문 대응 미평가")
            row_ids = ids(row)
            bound = row_ids & object_ids.keys()
            if object_ids:
                counts["object_eligible"] += 1
                if bound:
                    counts["object_bound"] += 1
                    known_pages = {p for identifier in bound for p in object_ids[identifier] if p is not None}
                    if parsed_pages and known_pages and not known_pages.intersection(parsed_pages):
                        add("provenance.object_page_mismatch", "출처페이지", evidence={"ids": sorted(bound), "object_pages": sorted(known_pages), "row_pages": parsed_pages})
                else:
                    add("provenance.object_unassessed", "근거ID", certainty="unassessed", evidence=sorted(row_ids), message="개별 원문 객체 ID 연결이 없어 사실 정확도 미평가")
            if parsed_pages and all(p in texts for p in parsed_pages):
                text = "".join(texts[p] for p in parsed_pages).replace(",", "")
                # 단순 숫자 존재는 합격이 아니다. 식별 정보·연도·단위도 진단한다.
                for field in ("사업명", "부문", "연도", "단위", "예산단위"):
                    value = row.get(field)
                    if present(value) and norm(value) not in text:
                        add("source.context_not_located", field, message="해당 페이지 텍스트에서 원문 표현 미확인 (이미지·별칭 가능)", evidence=value)
            business = {f: row.get(f) for f in config.EXCEL_HEADERS[config.SHEET_KEY_TO_NAME[key]] if f not in config.PROVENANCE_HEADERS}
            signature = canonical(business)
            if signature in seen:
                add("identity.exact_duplicate", certainty="confirmed", message="동일 업무 값 반복 (사실의 진위와 별개)", evidence={"other_excel_row": seen[signature]})
            else:
                seen[signature] = n
            fields = IDENTITIES.get(key)
            if fields:
                counts["identity_eligible"] += 1
                if all(present(row.get(f)) for f in fields):
                    counts["identity_checked"] += 1
                    fact_groups[tuple(norm(row[f]) for f in fields)].append(row)
        for group in fact_groups.values():
            variants = {fact_values(key, r) for r in group}
            if len(variants) > 1:
                for row in group:
                    findings.append(issue("identity.value_conflict", key, row["_excel_row"], certainty="suspected",
                        message="동일 식별 조건에 서로 다른 값. 출처/기간/배율 확인", evidence={"excel_rows": [r["_excel_row"] for r in group]}))
    records = (diagnostics or {}).get("extraction_records", [])
    execution = Counter(str(r.get("status", "unknown")) for r in records)
    execution_detail = Counter()
    for r in records:
        if r.get("reused") or r.get("status") == "reused":
            execution_detail["reused_success"] += 1
        elif r.get("status") == "ok":
            row_count = r.get("rows", r.get("row_count"))
            execution_detail["successful_empty" if row_count == 0 else "successful_with_rows" if isinstance(row_count, int) else "successful_row_count_unavailable"] += 1
        elif r.get("status") == "skipped":
            execution_detail["not_called"] += 1
        else:
            execution_detail["incomplete"] += 1
        if r.get("status") not in {"ok", "reused", "skipped"}:
            findings.append(issue("execution.batch_incomplete", r.get("sheet_key", ""), certainty="confirmed", severity="error",
                evidence={k: r.get(k) for k in ("batch_id", "status", "page_nums", "error")}, message="해당 배치 미완료. 시트 전체 누락을 뜻하지 않음"))
    unique = deduplicate_issues(findings)
    affected = {(r["sheet_key"], r["excel_row"]) for r in unique if r["excel_row"] and r["excel_row"] > 1}
    certainty_counts = Counter(r["certainty"] for r in unique)
    return {
        "schema_version": VERSION, "excel_path": str(Path(path).resolve()), "excel_sha256": document_hash(path),
        "scope": "실제 저장된 16개 업무 시트; 규칙 검사는 사실 정확도가 아님",
        "rule_score": None, "rule_score_unavailable_reason": "서로 다른 진단을 임의 가중합하지 않음. 골든 정확도와 분리",
        "metrics": {"required_field_fill": metric(counts["filled"], counts["required"], "존재하는 행의 유형별 필수 필드"),
                    "source_page_presence": metric(counts["pages_present"], counts["rows"], "출처페이지 표기; 사실 정확도 아님"),
                    "object_binding": metric(counts["object_bound"], counts["object_eligible"], "개별 원문 객체 연결; 사실 정확도 아님", reason="원문 객체 미제공/행 없음"),
                    "identity_check_coverage": metric(counts["identity_checked"], counts["identity_eligible"], "식별 계약이 있는 수치 시트 중 충돌 검사 가능한 행; 식별 필드 부족은 미검사"),
                    "storage_agreement": storage_metric},
        "row_counts": {key: len(rows) for key, rows in saved.items()}, "business_rows": counts["rows"],
        "issue_counts": dict(certainty_counts), "issues_by_rule": dict(Counter(i["rule_id"] for i in unique)),
        "raw_warning_records": len(findings), "unique_issues": len(unique), "affected_rows": len(affected),
        "unlocated_issues": sum(i["excel_row"] is None for i in unique),
        "execution": {"record_status_counts": dict(execution), "details": dict(execution_detail), "records": len(records),
                      "note": "정상 빈 반환·미호출·복원은 실패와 구분. 행 수로 정확도를 판단하지 않음"},
        "source_coverage": coverage or {"status": "unassessed", "reason": "원문 항목 대조 미실행"},
        "review_status": "no_business_data" if counts["rows"] == 0 else "needs_review" if unique else "no_rule_findings",
        "accuracy": {"cell_accuracy": None, "row_recall": None, "row_precision": None,
                     "reason": "골든 평가 결과는 별도 evaluation에 기록"}, "issues": unique,
    }


def persist_quality_report(path, report):
    base = Path(path).with_suffix("")
    paths = {"json": Path(str(base) + "_quality_report.json"), "markdown": Path(str(base) + "_quality_report.md"),
             "issues": Path(str(base) + "_quality_issues.jsonl")}
    # 큰 원장/원문 객체를 복제하지 않는다. 개별 문제의 셀·근거와 기존 경로만 보존한다.
    import json
    paths["issues"].write_text("".join(json.dumps(i, ensure_ascii=False, default=str) + "\n" for i in report["issues"]), encoding="utf-8")
    payload = {k: v for k, v in report.items() if k != "issues"}
    payload["issues_path"] = str(paths["issues"].resolve())
    save_json(paths["json"], payload)
    lines = ["# 저장 결과 품질 검사", "", "규칙 검사는 사실 정확도가 아닙니다. 미평가는 0점/100점으로 처리하지 않습니다.", "",
             f"- 업무 행: {report['business_rows']}", f"- 검토 상태: {report['review_status']}",
             f"- 확정 검사 위반 / 의심 / 미평가: {report['issue_counts']}",
             f"- 경고 기록 {report['raw_warning_records']} / 중복 제거 {report['unique_issues']} / 위치 확인된 영향 행 {report['affected_rows']}",
             f"- 골든 평가: {report.get('evaluation', {}).get('status', 'unassessed')}",
             f"- 골든 미평가 사유: {report.get('evaluation', {}).get('reason', '')}", "", "| 지표 | 값 | 분모 | 범위/미평가 사유 |", "|---|---:|---:|---|"]
    for key, m in report["metrics"].items():
        lines.append(f"| {key} | {m['value'] if m['available'] else '미평가'} | {m['denominator']} | {m['scope']} / {m['unavailable_reason'] or ''} |")
    lines.extend(["", "## 검사별 건수", "", *[f"- {k}: {v}" for k, v in report["issues_by_rule"].items()], "",
                  f"상세 셀/근거: `{paths['issues'].name}`", "", "LLM 의견은 보조 진단이며 정답률 또는 자동 수정 지시로 사용하지 않습니다."])
    evaluation = report.get("evaluation", {})
    lines.extend(["", "## 골든 대조", "", f"계약: {evaluation.get('contract_version', '미평가')} / 범위: {evaluation.get('scope', '')}",
                  f"골든 검토 상태: {evaluation.get('review_status', '')}",
                  "골든 매칭 정밀도는 골든 기준 비율이며, 불완전한 골든에서 실제 오답률로 해석할 수 없습니다.", ""])
    for label, values in (("v2", evaluation.get("metrics", {})), ("v1 참조 (과거 계약)", evaluation.get("legacy_v1", {}).get("metrics", {}))):
        lines.append(f"- {label}: " + ", ".join(f"{key}={value * 100:.2f}%" if isinstance(value, (int, float)) else f"{key}=미평가" for key, value in values.items() if key in {"cell_accuracy", "row_recall", "row_precision", "value_agreement"}))
    lines.append(f"- v2 분모/건수: {evaluation.get('counts', {})}")
    final_review = report.get("llm_review", {}).get("result", {})
    if final_review.get("schema_version") == "final-review-v2":
        from utils.final_review import review_log_lines
        lines.extend(["", "## 최종 검토 요약 (정답률 등급 아님)", "",
                      *review_log_lines(final_review, {"metrics": report["metrics"]}), "",
                      "모델 해설과 원응답은 JSON의 llm_review에 보존합니다. 과거 응답의 전체 등급은 사용하지 않습니다."])
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "saved", "paths": {k: str(v.resolve()) for k, v in paths.items()},
            "sha256": {k: document_hash(v) for k, v in paths.items()}, "schema_version": VERSION,
            "review_status": report["review_status"], "issue_counts": report["issue_counts"],
            "affected_rows": report["affected_rows"], "rule_score": None}
