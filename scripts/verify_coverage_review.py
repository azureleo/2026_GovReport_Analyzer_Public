"""고정된 원문 검토 표본과 분류 산출물을 대조한다. 판독/분류 모듈은 호출하지 않는다."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


EXPECTATIONS = {
    "final_cell_missing": ("missing_candidate", None),
    "already_present_alias": ("covered", None),
    "already_present_hierarchy": ("covered", None),
    "unit_and_hierarchy_review": ("unit_review", None),
    "covered_confirmed": ("covered", None),
    "covered_elsewhere_confirmed": ("covered_elsewhere", None),
    "render_proxy_not_independent_fact": ("excluded", "page_render_proxy"),
    "unsupported_diagram_context": ("unassessable", "non_tabular_diagram_or_fragment"),
    "context_linking_review": ("unassessable", "caption_business_context_required"),
    "blank_source_not_missing": ("excluded", {"source_value_blank", "source_values_blank_row"}),
    "source_context_capture_gap": ("unassessable", "explicit_unit_evidence_required"),
    "outside_current_numeric_scope": ("excluded", "outside_supported_performance_schema"),
}


def location(record):
    return (record.get("source", {}).get("object_id"), record.get("source_row"), record.get("source_column"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="개발 검토 표본의 분류 회귀 검증; 전체 정확도 측정 아님")
    parser.add_argument("--review", required=True, help="별도 원문 검토의 review_results.json")
    parser.add_argument("--journal", required=True, help="검증할 missing_candidates.jsonl")
    parser.add_argument("--baseline-journal", help="원래 분류 기록; 동일 사실 집단의 상태 전환 집계용")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.resolve() in {Path(args.review).resolve(), Path(args.journal).resolve()} or output.exists():
        parser.error("입력/기존 파일을 덮어쓰지 않는 새 output 경로를 지정하세요.")
    reviews = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
    journal = [json.loads(line) for line in Path(args.journal).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    by_id = {r["candidate_id"]: r for r in journal}
    checks = []
    for review in reviews["records"]:
        old = review["candidate"]
        observed = by_id.get(old["candidate_id"])
        if observed is None and "identity" not in old:
            candidates = [r for r in journal if "identity" not in r and location(r) == location(old)]
            if len(candidates) == 1:
                observed = candidates[0]
        if "review_status" in review:
            # 별도 표본 검토 결과에 이미 고정된 기대 상태/사유를 소비한다.
            # 판독/분류 코드에는 검토 표본이나 정답 사전을 전달하지 않는다.
            expected_status, expected_reason = review["review_status"], review.get("review_reason_code")
        else:
            expected_status, expected_reason = EXPECTATIONS[review["review_verdict"]]
        reason_ok = expected_reason is None or (observed and observed.get("reason") in
                    (expected_reason if isinstance(expected_reason, set) else {expected_reason}))
        passed = bool(observed and observed["status"] == expected_status and reason_ok)
        checks.append({"sample_id": review["sample_id"], "review_verdict": review["review_verdict"],
            "baseline_candidate_id": old["candidate_id"], "observed_candidate_id": observed.get("candidate_id") if observed else None,
            "expected_status": expected_status, "observed_status": observed.get("status") if observed else None,
            "observed_reason": observed.get("reason") if observed else None, "passed": passed})
    report = {"model_accuracy_evaluated": False, "scope": "development_review_regression_not_independent_holdout",
        "extraction_calls": 0, "review_sha256": sha(args.review), "journal_sha256": sha(args.journal),
        "samples": len(checks), "passed": sum(r["passed"] for r in checks),
        "failed": sum(not r["passed"] for r in checks), "checks": checks}
    if args.baseline_journal:
        old_journal = [json.loads(line) for line in Path(args.baseline_journal).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        facts = [r for r in old_journal if "identity" in r]
        report["same_fact_cohort"] = {"baseline_facts": len(facts),
            "retained_facts": sum(r["candidate_id"] in by_id for r in facts),
            "transitions": dict(Counter(f"{r['status']} -> {by_id[r['candidate_id']]['status']}" if r["candidate_id"] in by_id else
                                         f"{r['status']} -> not_found" for r in facts)),
            "newly_comparable_facts": sum("identity" in r and r["candidate_id"] not in {f["candidate_id"] for f in facts} for r in journal)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"검토 표본 {report['samples']} / 통과 {report['passed']} / 불일치 {report['failed']}")
    if "same_fact_cohort" in report:
        print(json.dumps(report["same_fact_cohort"], ensure_ascii=False))
    print(output.resolve())
    return 0 if report["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
