"""최종 검토의 데이터 규칙·검증 범위·선택 골든 결과를 분리한다.

상태는 저장 후 검사에서 결정한다. LLM은 확인 순서에 대한 참고 의견만 제공하며
업무 값, 정확도, 실행 상태와 재시도 조건을 바꾸지 않는다.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

VERSION = "final-review-v2"
VERIFICATION_RULES = {"source.context_not_located", "provenance.object_page_mismatch"}
TOPICS = {
    "source_coverage": "원문 대조 범위 확인",
    "object_binding": "개별 원문 객체 연결 확인",
    "identity_check_coverage": "충돌 검사에 필요한 식별 정보 확인",
    "quality_check": "규칙 검사 실행 상태 확인",
}


def build_review_context(quality, evaluation, quality_state=None):
    """원장 진단 수와 검증 불가를 정답/오답 수로 합산하지 않는다."""
    counts, limits, rules = Counter(), Counter(), Counter()
    if quality is not None:
        for record in quality.get("issues", []):
            certainty = record.get("certainty", "unassessed")
            rule = str(record.get("rule_id", ""))
            if certainty == "unassessed" or (certainty != "confirmed" and rule in VERIFICATION_RULES):
                limits[certainty] += 1
            else:
                counts[certainty] += 1
                rules[rule] += 1
        # 저장 보고서는 상세 JSONL이 분리될 수 있다. 상세 미제공을 무경고로 추정하지 않는다.
        detailed = "issues" in quality
        if not detailed:
            data_status, data_label = "unassessed", "규칙 상세 미제공"
        elif not quality.get("business_rows"):
            data_status, data_label = "no_data", "업무 데이터 없음"
        elif counts["confirmed"] or counts["suspected"]:
            data_status, data_label = "needs_review", "검토 필요"
        else:
            data_status, data_label = "no_findings", "검사 범위 내 규칙 위반 미발견"
    else:
        state = (quality_state or {}).get("status", "disabled")
        data_status, data_label = ("failed", "규칙 검사 실패") if state == "failed" else ("unassessed", "규칙 검사 미실행")
    metrics = deepcopy((quality or {}).get("metrics", {}))
    coverage = (quality or {}).get("source_coverage", {})
    comparable = coverage.get("comparable_facts")
    if coverage.get("status") == "failed":
        scope_status, scope_label = "failed", "원문 대조 실패"
    elif isinstance(comparable, (int, float)) and not isinstance(comparable, bool) and comparable > 0:
        scope_status, scope_label = "partial", "원문 일부 항목 대조"
    elif comparable == 0:
        scope_status, scope_label = "insufficient", "원문 대조 범위 부족"
    else:
        scope_status, scope_label = "unassessed", "원문 대조 미실행/미평가"
    status = evaluation.get("status", "unassessed")
    accuracy_labels = {
        "disabled": "미평가 (골든 평가 OFF)", "unassessed": "미평가",
        "evaluated": "선택한 골든 범위에서 평가", "required_unavailable": "요청한 골든 평가 미완료",
        "failed": "골든 평가 실패",
    }
    return {
        "schema_version": VERSION,
        "rule_check": {"status": data_status, "label": data_label,
                       "confirmed": counts["confirmed"], "suspected": counts["suspected"],
                       "available_rule_ids": dict(rules),
                       "note": "규칙 위반/의심 진단 건수이며 독립 오답 행 수가 아님"},
        "verification_scope": {"status": scope_status, "label": scope_label,
                               "comparable_facts": comparable, "diagnostic_counts": dict(limits),
                               "note": "검증 근거 부족은 데이터 오답이나 낮은 정확도를 뜻하지 않음"},
        "fact_accuracy": {"status": status, "label": accuracy_labels.get(status, "미평가"),
                          "reason": evaluation.get("reason"), "metrics": deepcopy(evaluation.get("metrics", {})),
                          "contract_version": evaluation.get("contract_version"),
                          "note": "골든 OFF/미등록은 품질 감점 사유가 아님"},
        "metrics": metrics,
    }


def review_prompt(municipality, sheet_counts, context):
    return f"""'{municipality}' 탄소중립 계획의 저장 후 검사 요약을 검토하세요.
업무 행 수: {json.dumps(sheet_counts, ensure_ascii=False)}
검사 결과: {json.dumps(context, ensure_ascii=False)}

당신의 역할은 확인된 진단에 대한 보조 설명과 검토 순서 제안입니다.
- 우수/보통/미흡 같은 전체 등급이나 새로운 정확도 점수를 만들지 마세요.
- rule_check, verification_scope, fact_accuracy는 이미 검사 코드에서 결정했습니다. 변경하지 마세요.
- 객체 연결률과 식별 정보 검사 범위는 사실 정확도가 아닙니다. 0%라도 전체 오답으로 해석하지 마세요.
- 골든 OFF/미등록은 사용 조건이며 데이터 품질 저하나 감점의 근거가 아닙니다.
- 원문 비교 항목이 0개이면 누락 없음/100% 반영으로 판단하지 마세요.
- 검증 범위 부족과 데이터 규칙 위반을 분리하세요. 규칙 위반 건수는 오답 행 수가 아닙니다.
- 데이터 규칙은 available_rule_ids에 있는 항목만 언급하세요. 원문/값을 직접 검증했다고 주장하지 마세요.
- 각 배열은 최대 6개 항목으로 간결하게 작성하세요.

JSON 객체만 반환하세요. 해당 의견이 없으면 빈 배열을 사용하세요.
{{
  "data_findings": [{{"rule_id": "available_rule_ids에 있는 ID", "note": "확인할 사항"}}],
  "verification_notes": [{{"topic": "source_coverage|object_binding|identity_check_coverage|quality_check", "note": "확인 범위의 한계"}}],
  "next_checks": ["우선 확인할 rule_id 또는 topic"]
}}"""


def normalize_review(response, context, error=None):
    """LLM의 새/과거 응답을 보존하되 자유 서술 등급을 최종 상태에 사용하지 않는다."""
    rules = context["rule_check"]["available_rule_ids"]
    valid = isinstance(response, dict) and any(isinstance(response.get(k), list) for k in ("data_findings", "verification_notes", "next_checks"))
    status = "failed" if error is not None else "received" if valid else "legacy_or_unstructured"
    findings, notes, next_checks = [], [], []
    if valid:
        for record in response.get("data_findings", []) if isinstance(response.get("data_findings"), list) else []:
            if isinstance(record, dict) and isinstance(record.get("rule_id"), str) and record["rule_id"] in rules and isinstance(record.get("note"), str):
                findings.append({"rule_id": record["rule_id"], "note": record["note"][:2000], "role": "model_comment"})
        for record in response.get("verification_notes", []) if isinstance(response.get("verification_notes"), list) else []:
            if isinstance(record, dict) and isinstance(record.get("topic"), str) and record["topic"] in TOPICS and isinstance(record.get("note"), str):
                notes.append({"topic": record["topic"], "note": record["note"][:2000], "role": "model_comment"})
        for topic in response.get("next_checks", []) if isinstance(response.get("next_checks"), list) else []:
            if isinstance(topic, str) and topic in {*rules, *TOPICS} and topic not in next_checks:
                next_checks.append(topic)
    return {"schema_version": VERSION, "rule_check": deepcopy(context["rule_check"]),
            "verification_scope": deepcopy(context["verification_scope"]), "fact_accuracy": deepcopy(context["fact_accuracy"]),
            "assessment": f"데이터 규칙: {context['rule_check']['label']}; 검증 범위: {context['verification_scope']['label']}; 사실 정확도: {context['fact_accuracy']['label']}",
            "llm_status": status, "data_findings": findings[:6], "verification_notes": notes[:6], "next_checks": next_checks[:6],
            "raw_response": deepcopy(response), "error": str(error) if error is not None else None,
            "note": "상태는 저장 후 검사에서 결정. 모델 의견과 과거 응답의 등급은 정답률/최종 상태에 미반영"}


def review_log_lines(review, context):
    rule, scope, accuracy = review["rule_check"], review["verification_scope"], review["fact_accuracy"]
    scope_detail = f"비교 가능한 원문 항목 {scope['comparable_facts']}건" if scope['comparable_facts'] is not None else "비교 항목 수 미평가"
    lines = ["[감독관] 저장 결과 검토 요약 (정답률 등급 아님)",
             f"  데이터 규칙: {rule['label']} (확인된 규칙 위반 {rule['confirmed']}건 / 의심 {rule['suspected']}건)",
             f"  검증 범위: {scope['label']} ({scope_detail})"]
    for key, label in (("object_binding", "개별 원문 객체 연결"), ("identity_check_coverage", "식별 정보가 충분한 충돌 검사 대상")):
        metric = context.get("metrics", {}).get(key, {})
        if metric.get("available"):
            lines.append(f"  {label}: {metric['numerator']}/{metric['denominator']}행 (사실 정확도 아님)")
        else:
            lines.append(f"  {label}: 미평가")
    lines.append(f"  사실 정확도: {accuracy['label']}" + (f" / {accuracy['reason']}" if accuracy.get("reason") and accuracy['status'] != "disabled" else ""))
    labels = {"received": "참고 의견 저장", "legacy_or_unstructured": "구형/비정형 응답 보존; 전체 등급 미사용", "failed": "의견 생성 실패; 규칙 진단 유지"}
    lines.append(f"  LLM 보조 검토: {labels[review['llm_status']]} (정답률·등급에 미반영)")
    if review["next_checks"]:
        lines.append("  제안된 확인 순서: " + ", ".join(TOPICS.get(k, k) for k in review["next_checks"]))
    return lines
