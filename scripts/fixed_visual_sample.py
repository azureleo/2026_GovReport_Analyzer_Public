"""Build, validate, run, and score a deterministic visual-object sample."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.fixed_visual_sample import (
    FixedSampleDataset,
    FixedSampleError,
    FixedSampleItem,
    build_fixed_sample_dataset,
    evaluate_fixed_sample_merge_ab,
    evaluate_fixed_sample_results,
    load_annotations,
    load_fixed_sample_dataset,
    load_result_rows,
    prepare_merge_annotations,
    validate_fixed_sample_dataset,
)
from utils.vision_recovery import (
    ObjectOutcomeLedger,
    normalize_terminal_status,
    split_in_half,
)
from utils.visual_contract import VISUAL_CONTRACT_VERSION, normalize_visual_table_rows


SYSTEM_PROMPT = """당신은 한국 지자체 탄소중립 계획서의 시각 객체를 판독하는 검증 모델입니다.
이미지에 실제로 보이는 내용만 사용하고 숫자를 추정하거나 외부 지식을 보충하지 마세요.
항목명, 부호, 기간, 단위는 이미지의 표기를 축약하거나 바꾸지 말고 그대로 기록하세요.
모든 sample_id에 대해 반드시 결과 한 개를 반환하세요."""


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _image_base64(dataset: FixedSampleDataset, item: FixedSampleItem) -> str:
    image_path = (dataset.manifest_path.parent / item.image_path).resolve()
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def _prompt(items: Sequence[FixedSampleItem]) -> str:
    mappings = []
    for index, item in enumerate(items, start=1):
        context = " ".join(item.page_text_excerpt.split())[:700]
        mappings.append(
            f"- image_index={index}, sample_id={item.sample_id}, page={item.page}, "
            f"object_type_hint={item.element_type}, title_hint={item.title}, "
            f"related_sheet_hint={item.related_sheet or 'unknown'}, "
            f"page_context={context}"
        )
    return f"""첨부 이미지는 고정 표본 데이터셋의 시각 객체입니다. 각 이미지를 매핑된 객체에 집중해 판독하세요.

[입력 매핑]
{chr(10).join(mappings)}

JSON 형식:
{{
  "analyses": [
    {{
      "sample_id": "E001",
      "contract_version": {VISUAL_CONTRACT_VERSION},
      "status": "extracted|not_relevant|no_data|needs_review",
      "object_type": "그래프|이미지표|이미지|지도·사진|장식|기타",
      "scope": "municipality|external_reference|generic|unknown",
      "target_sheet": "관련 시트 내부 키 또는 null",
      "title": "이미지에서 확인한 제목",
      "unit": "단위 원문 또는 null",
      "table": [
        {{
          "연도": 2030,
          "항목": "계열 또는 지표",
          "값": 123.4,
          "단위": "원문 단위",
          "fields": {{
            "값근거": "명시라벨|표셀|축추정|계산값|불명",
            "기간원문": "21~30년처럼 이미지에 적힌 기간 또는 생략",
            "집계수준": "합계|세부",
            "합계그룹": "같이 검산할 합계·세부 행의 그룹명",
            "대상 시트의 키 필드": "이미지에서 확인한 원문값"
          }}
        }}
      ],
      "summary": "원문에 보이는 내용만 요약",
      "confidence": "high|medium|low",
      "notes": "판독 제한이나 불확실성"
    }}
  ]
}}

판정 규칙:
- analyses에 위 매핑의 모든 sample_id를 정확히 한 번씩 포함하세요.
- 해당 객체가 데이터 표·그래프·기록 대상 시각자료이면 status=extracted입니다.
- 데이터가 있는 외부 사례 그래프도 시각 객체 인식에는 해당하므로 status=extracted로 두고 scope=external_reference로 구분하세요.
- 사진·장식처럼 데이터 기록 대상이 아닌 시각물만 status=not_relevant입니다.
- 객체는 맞지만 값이나 의미를 읽을 수 없으면 status=no_data 또는 needs_review입니다.
- 정확히 읽히지 않는 축 값은 임의 보간하지 말고 null로 두세요.
- 항목명·범례명은 동의어나 축약어로 바꾸지 말고 이미지의 원문을 그대로 복사하세요.
- `21~30년` 같은 기간을 `21~1930` 또는 임의의 단일 연도로 확장하지 마세요. 연도는 null로 두고 fields.기간원문에 원문 그대로 기록하세요.
- 양수·음수 부호를 반드시 보존하세요. `-17%`를 `17`로 바꾸면 안 됩니다.
- 하나의 정량값은 table의 한 행으로 분리하세요. BAU, 감축량, 감축률, 신규, 누계 값을 fields 문자열 안에 묶지 마세요.
- 그래프 중앙이나 표에 합계가 명시되어 있으면 합계도 별도 행으로 반환하고, 관련 세부 행과 동일한 fields.합계그룹을 기록하세요.
- 막대·점 옆에 숫자가 직접 적혀 있으면 값근거=명시라벨, 표 셀이면 표셀, 축 눈금으로 읽은 값이면 축추정으로 기록하세요.
- target_sheet는 related_sheet_hint를 우선 검토하되 실제 이미지 내용과 다르면 올바른 시트로 수정하세요.
- fields에는 대상 시트의 행 식별에 필요한 키 필드만 이미지에서 확인되는 범위로 기록하세요.
- 반드시 JSON만 반환하세요."""


def _normalize_analysis(raw: dict[str, Any], item: FixedSampleItem) -> dict[str, Any]:
    requested_status = str(raw.get("status") or "").strip()
    if requested_status not in {"extracted", "not_relevant", "no_data", "needs_review"}:
        if str(raw.get("type") or "") == "해당없음":
            requested_status = "not_relevant"
        elif isinstance(raw.get("table"), list) and raw.get("table"):
            requested_status = "extracted"
        elif any(str(raw.get(key) or "").strip() for key in ("title", "summary")):
            requested_status = "extracted"
        else:
            requested_status = "needs_review"
    table = raw.get("table") if isinstance(raw.get("table"), list) else []
    table = normalize_visual_table_rows(
        table,
        chart_type=str(raw.get("object_type") or raw.get("chart_type") or item.element_type),
        title=str(raw.get("title") or item.title),
    )
    status = normalize_terminal_status(
        requested_status,
        response_received=True,
        table=table,
        data_expected=item.data_included,
    )
    notes = str(raw.get("notes") or "")
    if requested_status == "extracted" and status == "no_data":
        suffix = "데이터 객체이나 명시적인 값이 없어 no_data로 정규화"
        notes = f"{notes}; {suffix}" if notes else suffix
    return {
        "sample_id": item.sample_id,
        "contract_version": VISUAL_CONTRACT_VERSION,
        "page": item.page,
        "role": item.role,
        "status": status,
        "response_received": True,
        "object_type": str(raw.get("object_type") or raw.get("chart_type") or item.element_type),
        "scope": str(raw.get("scope") or "unknown"),
        "target_sheet": str(raw.get("target_sheet") or item.related_sheet or ""),
        "title": str(raw.get("title") or ""),
        "unit": raw.get("unit"),
        "table": table,
        "summary": str(raw.get("summary") or raw.get("description") or ""),
        "confidence": str(raw.get("confidence") or "low"),
        "notes": notes,
    }


def _needs_review_row(item: FixedSampleItem, reason: str) -> dict[str, Any]:
    return {
        "sample_id": item.sample_id,
        "page": item.page,
        "role": item.role,
        "status": "needs_review",
        "response_received": False,
        "object_type": item.element_type,
        "scope": "unknown",
        "title": "",
        "unit": None,
        "table": [],
        "summary": "",
        "confidence": "low",
        "notes": reason,
    }


def _extract_analyses(parsed: Any) -> list[dict[str, Any]]:
    rows = parsed.get("analyses", []) if isinstance(parsed, dict) else parsed
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _call_batch(
    dataset: FixedSampleDataset,
    items: Sequence[FixedSampleItem],
    *,
    max_retries: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from utils import llm_client

    started = time.perf_counter()
    try:
        parsed, parse_ok = llm_client.call_vision_batch_json(
            [_image_base64(dataset, item) for item in items],
            _prompt(items),
            system=SYSTEM_PROMPT,
            max_retries=max_retries,
            stage="vision",
        )
        analyses = _extract_analyses(parsed) if parse_ok else []
        error = "" if parse_ok else "JSON parse failed"
    except Exception as exc:  # The batch is recorded and may be retried per object.
        analyses = []
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    expected_ids = [item.sample_id for item in items]
    returned_ids = [str(row.get("sample_id") or "") for row in analyses]
    return analyses, {
        "sample_ids": expected_ids,
        "returned_ids": returned_ids,
        "elapsed_seconds": round(elapsed, 3),
        "error": error,
    }


def _accept_analyses(
    items: Sequence[FixedSampleItem],
    analyses: Sequence[dict[str, Any]],
    result_by_id: dict[str, dict[str, Any]],
    outcomes: ObjectOutcomeLedger,
) -> tuple[list[FixedSampleItem], list[str], list[str]]:
    item_by_id = {item.sample_id: item for item in items}
    extra_ids: list[str] = []
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for raw in analyses:
        sample_id = str(raw.get("sample_id") or "").strip()
        if sample_id not in item_by_id:
            if sample_id:
                extra_ids.append(sample_id)
            continue
        rows_by_id.setdefault(sample_id, []).append(raw)

    duplicate_ids = sorted(
        sample_id for sample_id, rows in rows_by_id.items() if len(rows) != 1
    )
    for sample_id, rows in rows_by_id.items():
        if len(rows) != 1 or sample_id in result_by_id:
            continue
        normalized = _normalize_analysis(rows[0], item_by_id[sample_id])
        result_by_id[sample_id] = normalized
        outcomes.record(
            sample_id,
            normalized["status"],
            response_received=True,
            reason=normalized.get("notes") or "VLM 객체 응답 확보",
        )
    missing = [item for item in items if item.sample_id not in result_by_id]
    return missing, duplicate_ids, sorted(set(extra_ids))


def _recover_visual_items(
    dataset: FixedSampleDataset,
    items: Sequence[FixedSampleItem],
    *,
    batch_index: int,
    retry_policy: str,
    max_retries: int,
    max_split_depth: int,
    max_object_attempts: int,
    result_by_id: dict[str, dict[str, Any]],
    outcomes: ObjectOutcomeLedger,
    batch_logs: list[dict[str, Any]],
    depth: int = 0,
    path: str = "root",
    call_batch=None,
) -> None:
    caller = call_batch or _call_batch
    pending = [
        item for item in items
        if item.sample_id not in result_by_id
        and outcomes.attempt_count(item.sample_id) < max_object_attempts
    ]
    if not pending:
        return

    outcomes.mark_attempt(
        (item.sample_id for item in pending),
        label=f"batch={batch_index},path={path},depth={depth}",
    )
    analyses, log = caller(dataset, pending, max_retries=max_retries)
    missing, duplicate_ids, extra_ids = _accept_analyses(
        pending,
        analyses,
        result_by_id,
        outcomes,
    )
    log.update({
        "batch_index": batch_index,
        "split_depth": depth,
        "recovery_path": path,
        "retry_policy": retry_policy,
        "missing_ids": [item.sample_id for item in missing],
        "duplicate_ids": duplicate_ids,
        "extra_ids": extra_ids,
    })
    batch_logs.append(log)

    if not missing or retry_policy == "none":
        return
    if retry_policy == "individual":
        if depth > 0:
            return
        for item in missing:
            _recover_visual_items(
                dataset,
                [item],
                batch_index=batch_index,
                retry_policy="none",
                max_retries=max_retries,
                max_split_depth=max_split_depth,
                max_object_attempts=max_object_attempts,
                result_by_id=result_by_id,
                outcomes=outcomes,
                batch_logs=batch_logs,
                depth=depth + 1,
                path=f"{path}.individual:{item.sample_id}",
                call_batch=caller,
            )
        return

    if retry_policy != "adaptive" or depth >= max_split_depth:
        return
    retryable = [
        item for item in missing
        if outcomes.attempt_count(item.sample_id) < max_object_attempts
    ]
    if not retryable:
        return
    groups = split_in_half(retryable) if len(retryable) > 1 else [retryable]
    for group_index, group in enumerate(groups, start=1):
        _recover_visual_items(
            dataset,
            group,
            batch_index=batch_index,
            retry_policy="adaptive",
            max_retries=max_retries,
            max_split_depth=max_split_depth,
            max_object_attempts=max_object_attempts,
            result_by_id=result_by_id,
            outcomes=outcomes,
            batch_logs=batch_logs,
            depth=depth + 1,
            path=f"{path}.{group_index}",
            call_batch=caller,
        )


def _markdown_report(payload: dict[str, Any]) -> str:
    metrics = payload["evaluation"]["metrics"]
    counts = payload["evaluation"]["counts"]
    return "\n".join([
        f"# 고정 시각 표본 벤치마크: {payload['dataset_id']}",
        "",
        "## 실행 조건",
        "",
        f"- 데이터셋 지문: `{payload['dataset_fingerprint']}`",
        f"- Vision 백엔드: `{payload['provider']}:{payload['model'] or 'backend-default'}`",
        f"- 캐시: `{'on' if payload['cache_enabled'] else 'off'}`",
        f"- 누락 재시도 정책: `{payload['retry_missing']}`",
        f"- 최대 분할 깊이: `{payload.get('max_split_depth', 0)}`",
        f"- 객체당 최대 시도: `{payload.get('max_object_attempts', 1)}`",
        f"- 표본 수: {metrics['sample_count']}개",
        f"- 소요 시간: {payload['elapsed_seconds']:.1f}초",
        "",
        "## 지표",
        "",
        "| 지표 | 값 |",
        "|---|---:|",
        f"| 응답 커버리지 | {metrics['response_coverage']:.4f} |",
        f"| 기대 객체 리콜 | {metrics['expected_recall']} |",
        f"| 음성 특이도 | {metrics['negative_specificity']} |",
        f"| 분류 정확도 | {metrics['classification_accuracy']:.4f} |",
        f"| 검토 정답 분류 정확도 | {metrics.get('reviewed_classification_accuracy')} |",
        f"| 수치값 보유 커버리지 | {metrics['numeric_value_coverage']} |",
        f"| 구조화 셀 정확도 | {metrics.get('structured_cell_accuracy')} ({metrics['value_accuracy_note']}) |",
        f"| 숫자값 재현율 | {metrics.get('numeric_value_recall')} |",
        f"| 숫자값 정밀도 | {metrics.get('numeric_value_precision')} |",
        "",
        "## 건수",
        "",
        f"- 양성: {counts['positive_hits']}/{counts['positives']}",
        f"- 음성: {counts['negative_hits']}/{counts['negatives']}",
        f"- 수치 포함 양성: {counts['numeric_hits']}/{counts['data_positives']}",
        f"- 상태: `{json.dumps(counts['statuses'], ensure_ascii=False)}`",
        f"- 미응답 ID: `{', '.join(payload['evaluation']['missing_ids']) or '없음'}`",
        f"- 재시도로 복구: `{', '.join(payload.get('recovery', {}).get('recovered_ids', [])) or '없음'}`",
        f"- 최종 needs_review: `{', '.join(payload.get('recovery', {}).get('needs_review_ids', [])) or '없음'}`",
        "",
        "값 정확도는 원본 대조를 마친 `expected_visual_rows`가 있는 주석에 대해서만 계산합니다.",
        "현재 자동 지표는 객체 유실, 관련성 판정, 수치가 실제로 반환됐는지를 비교합니다.",
        "",
    ])


def run_dataset(args: argparse.Namespace) -> int:
    validation = validate_fixed_sample_dataset(
        args.manifest,
        source_override=args.source,
        require_images=True,
    )
    if not validation.valid:
        print(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    os.environ["LLM_CACHE_ENABLED"] = "1" if args.cache_mode == "on" else "0"
    os.environ["RUN_STATE_ENABLED"] = "0"
    os.environ["STAGE_PROVIDER_VISION"] = args.provider
    if args.model:
        os.environ["STAGE_MODEL_VISION"] = args.model
    if args.cache_dir:
        os.environ["LLM_CACHE_DIR"] = str(Path(args.cache_dir).resolve())

    from utils import llm_client
    from utils.llm_cache import get_cache_stats, reset_cache_stats

    llm_client.reset_llm_stats()
    reset_cache_stats()
    dataset = load_fixed_sample_dataset(args.manifest)
    roles = set(args.roles.split(",")) if args.roles else set()
    items = [item for item in dataset.items if not roles or item.role in roles]
    if not items:
        raise FixedSampleError("실행 대상으로 선택된 표본이 없습니다.")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_size = max(1, int(args.batch_size))
    batches = [items[index:index + batch_size] for index in range(0, len(items), batch_size)]
    started = time.perf_counter()
    result_by_id: dict[str, dict[str, Any]] = {}
    outcomes = ObjectOutcomeLedger(item.sample_id for item in items)
    batch_logs: list[dict[str, Any]] = []
    print(
        f"[고정표본] {dataset.dataset_id}: {len(items)}개, {len(batches)}배치, "
        f"cache={args.cache_mode}, retry_missing={args.retry_missing}"
    )
    for batch_index, batch in enumerate(batches, start=1):
        log_start = len(batch_logs)
        _recover_visual_items(
            dataset,
            batch,
            batch_index=batch_index,
            retry_policy=args.retry_missing,
            max_retries=args.max_retries,
            max_split_depth=max(0, int(args.max_split_depth)),
            max_object_attempts=max(1, int(args.max_object_attempts)),
            result_by_id=result_by_id,
            outcomes=outcomes,
            batch_logs=batch_logs,
        )
        for attempt_log in batch_logs[log_start:]:
            returned = len(attempt_log.get("returned_ids", []))
            expected = len(attempt_log.get("sample_ids", []))
            depth = int(attempt_log.get("split_depth", 0) or 0)
            label = (
                f"배치 {batch_index}/{len(batches)}"
                if depth == 0
                else f"  복구 {attempt_log.get('recovery_path')}"
            )
            print(
                f"  {label}: 응답 {returned}/{expected}, "
                f"{attempt_log.get('elapsed_seconds', 0.0):.1f}초"
            )

    recovered_ids: list[str] = []
    terminal_rows: list[dict[str, Any]] = []
    for item in items:
        outcome = outcomes.outcome(
            item.sample_id,
            unresolved_reason="재시도 상한 도달 또는 VLM 최종 응답 누락",
        )
        terminal_rows.append(outcome.to_dict())
        if item.sample_id not in result_by_id:
            result_by_id[item.sample_id] = _needs_review_row(item, outcome.terminal_reason)
        row = result_by_id[item.sample_id]
        row["status"] = outcome.status
        row["response_received"] = outcome.response_received
        row["attempt_count"] = outcome.attempt_count
        row["terminal_reason"] = outcome.terminal_reason
        row["attempt_history"] = outcome.history
        if outcome.response_received and outcome.attempt_count > 1:
            recovered_ids.append(item.sample_id)
    results = [result_by_id[item.sample_id] for item in items]
    results_path = output_dir / "results.jsonl"
    _write_jsonl(results_path, results)
    _write_jsonl(output_dir / "batches.jsonl", batch_logs)
    elapsed = time.perf_counter() - started
    evaluation = evaluate_fixed_sample_results(
        FixedSampleDataset(dataset.manifest_path, dataset.payload, tuple(items)),
        results,
        load_annotations(args.annotations),
    )
    payload = {
        "dataset_id": dataset.dataset_id,
        "dataset_fingerprint": dataset.fingerprint,
        "manifest": str(dataset.manifest_path),
        "provider": args.provider,
        "model": args.model,
        "cache_enabled": args.cache_mode == "on",
        "retry_missing": args.retry_missing,
        "max_split_depth": max(0, int(args.max_split_depth)),
        "max_object_attempts": max(1, int(args.max_object_attempts)),
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 3),
        "llm_stats": llm_client.get_llm_stats(),
        "cache_stats": get_cache_stats(),
        "evaluation": evaluation,
        "recovery": {
            "total_attempts": sum(row["attempt_count"] for row in terminal_rows),
            "recovered_ids": sorted(recovered_ids),
            "needs_review_ids": sorted(
                row["object_id"] for row in terminal_rows if row["status"] == "needs_review"
            ),
            "object_outcomes": terminal_rows,
        },
        "results": str(results_path),
    }
    report_json = output_dir / "report.json"
    report_md = output_dir / "report.md"
    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_markdown_report(payload), encoding="utf-8")
    print(json.dumps({
        "report_json": str(report_json),
        "report_markdown": str(report_md),
        "metrics": evaluation["metrics"],
    }, ensure_ascii=False))
    return 0


def _format_metric(value: Any) -> str:
    if value is None:
        return "미산출"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _merge_ab_markdown(payload: dict[str, Any]) -> str:
    legacy = payload["metrics"]["legacy"]
    evidence = payload["metrics"]["evidence"]
    comparison = payload["comparison"]
    metric_rows = [
        ("채점 표본", "confirmed_samples"),
        ("판정 정확도", "decision_accuracy"),
        ("자동 병합 행", "auto_merge_rows"),
        ("오병합 행", "false_merge_rows"),
        ("자동 병합 정밀도", "auto_merge_precision"),
        ("자동 병합 재현율", "auto_merge_recall"),
        ("대상 시트 정확도", "target_sheet_accuracy"),
        ("실제 병합 시트 정확도", "merged_sheet_accuracy"),
        ("정확 근거 매칭률", "exact_evidence_match_rate"),
        ("needs_review 후보", "needs_review_candidates"),
        ("needs_review 사유 보유율", "needs_review_reason_coverage"),
        ("복수 근거 격리율", "multiple_evidence_isolation_rate"),
        ("전부 null 격리율", "null_isolation_rate"),
        ("텍스트 충돌 격리율", "text_conflict_isolation_rate"),
    ]
    lines = [
        f"# 근거 기반 병합 A/B: {payload['dataset_id']}",
        "",
        "동일한 저장 Vision 결과를 `legacy`와 `evidence` 병합 경로에 각각 투입한 결과입니다.",
        "모델은 다시 호출하지 않으므로 차이는 병합 정책에서만 발생합니다.",
        "",
        "## 주석 상태",
        "",
        f"- 전체: {payload['annotation_status']['total']}개",
        f"- 검토 완료·채점 가능: {payload['annotation_status']['confirmed']}개",
        f"- 미검토: {payload['annotation_status']['pending']}개",
        "",
        "## 지표",
        "",
        "| 지표 | 기존 병합 | 근거 기반 병합 |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| {label} | {_format_metric(legacy.get(key))} | {_format_metric(evidence.get(key))} |"
        for label, key in metric_rows
    )
    lines.extend([
        "",
        "## 변화량",
        "",
        f"- 자동 병합 정밀도 변화: {_format_metric(comparison.get('auto_merge_precision_delta'))}",
        f"- 자동 병합 재현율 변화: {_format_metric(comparison.get('auto_merge_recall_delta'))}",
        f"- 오병합 감소 행: {_format_metric(comparison.get('false_merge_reduction'))}",
        f"- 자동 병합 행 변화: {_format_metric(comparison.get('auto_merge_row_delta'))}",
        "",
    ])
    if not payload["annotation_status"]["confirmed"]:
        lines.extend([
            "> 아직 검토 완료된 병합 주석이 없어 정확도·정밀도·재현율은 미산출입니다. ",
            "> `annotations_merge.jsonl`에서 정답을 원본 대조하고 `review_status=reviewed` 이상으로 표시한 뒤 다시 실행하세요.",
            "",
        ])
    return "\n".join(lines)


def prepare_merge_annotations_command(args: argparse.Namespace) -> int:
    dataset = load_fixed_sample_dataset(args.manifest)
    default_base = dataset.manifest_path.parent / "annotations_template.jsonl"
    base_path = Path(args.base).resolve() if args.base else default_base
    base_rows = load_annotations(base_path) if base_path.exists() else []
    output = Path(args.output).resolve()
    if output.exists() and not args.force:
        raise FixedSampleError(f"출력 파일이 이미 존재합니다: {output} (--force로 덮어쓰기)")
    rows = prepare_merge_annotations(dataset, base_rows)
    _write_jsonl(output, rows)
    print(json.dumps({
        "annotations": str(output),
        "objects": len(rows),
        "base": str(base_path) if base_path.exists() else None,
        "confirmed": sum(
            str(row.get("review_status") or "").casefold()
            in {"confirmed", "확정", "reviewed", "검토완료"}
            for row in rows
        ),
    }, ensure_ascii=False))
    return 0


def _flatten_detail_rows(
    details: Sequence[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for detail in details:
        values = detail.get(field)
        for index, value in enumerate(values if isinstance(values, list) else [], start=1):
            if not isinstance(value, dict):
                continue
            rows.append({
                "sample_id": detail.get("sample_id"),
                "page": detail.get("page"),
                "target_sheet": detail.get("target_sheet"),
                "decision": detail.get("decision"),
                "sequence": index,
                **value,
            })
    return rows


def run_merge_ab(args: argparse.Namespace) -> int:
    validation = validate_fixed_sample_dataset(args.manifest, require_images=False)
    if not validation.valid:
        print(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    dataset = load_fixed_sample_dataset(args.manifest)
    result_rows = load_result_rows(args.results)
    annotation_rows = load_annotations(args.annotations)
    payload = evaluate_fixed_sample_merge_ab(
        dataset,
        result_rows,
        annotation_rows,
        municipality=args.municipality,
    )
    payload.update({
        "manifest": str(dataset.manifest_path),
        "results": str(Path(args.results).resolve()),
        "annotations": str(Path(args.annotations).resolve()),
    })
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_details = payload["details"]["legacy"]
    evidence_details = payload["details"]["evidence"]
    legacy_rows = _flatten_detail_rows(legacy_details, "merged_rows")
    evidence_rows = _flatten_detail_rows(evidence_details, "merged_rows")
    evidence_candidates = _flatten_detail_rows(evidence_details, "observations")
    needs_review = [
        row for row in evidence_candidates
        if str(row.get("병합상태") or "").strip() == "needs_review"
    ]
    _write_jsonl(output_dir / "legacy_rows.jsonl", legacy_rows)
    _write_jsonl(output_dir / "evidence_rows.jsonl", evidence_rows)
    _write_jsonl(output_dir / "evidence_candidates.jsonl", evidence_candidates)
    _write_jsonl(output_dir / "needs_review.jsonl", needs_review)
    report_json = output_dir / "ab_report.json"
    report_md = output_dir / "ab_report.md"
    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_merge_ab_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "report_json": str(report_json),
        "report_markdown": str(report_md),
        "legacy_rows": len(legacy_rows),
        "evidence_rows": len(evidence_rows),
        "needs_review": len(needs_review),
        "comparison": payload["comparison"],
    }, ensure_ascii=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="고정 시각 객체 표본 데이터셋 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="원본 PDF와 사람 인벤토리에서 표본을 고정")
    build.add_argument("source_pdf")
    build.add_argument("--inventory", required=True)
    build.add_argument("--audit")
    build.add_argument("--dataset-id", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--failure-status", action="append", default=None)
    build.add_argument("--positive-controls", type=int, default=12)
    build.add_argument("--negative-controls", type=int, default=5)
    build.add_argument("--dpi", type=int, default=144)
    build.add_argument("--force", action="store_true")

    validate = subparsers.add_parser("validate", help="원본·객체·렌더 이미지 해시 검증")
    validate.add_argument("manifest")
    validate.add_argument("--source")
    validate.add_argument("--allow-missing-images", action="store_true")

    run = subparsers.add_parser("run", help="고정 표본만 VLM으로 판독하고 자동 지표 생성")
    run.add_argument("manifest")
    run.add_argument("--source")
    run.add_argument("--provider", default="codex", choices=("codex", "claude", "gemini", "openai"))
    run.add_argument("--model", default="")
    run.add_argument("--batch-size", type=int, default=4)
    run.add_argument("--max-retries", type=int, default=1)
    run.add_argument("--cache-mode", choices=("off", "on"), default="off")
    run.add_argument("--cache-dir")
    run.add_argument(
        "--retry-missing",
        choices=("none", "individual", "adaptive"),
        default="none",
        help="none, 즉시 개별 재시도, 또는 실패 묶음 이분 분할 후 개별 격리",
    )
    run.add_argument("--max-split-depth", type=int, default=6)
    run.add_argument("--max-object-attempts", type=int, default=3)
    run.add_argument("--roles", default="failure,positive_control,negative_control")
    run.add_argument("--annotations", help="원본 대조한 expected_visual_rows가 있는 JSONL")
    run.add_argument("--output-dir", required=True)

    evaluate = subparsers.add_parser("evaluate", help="기존 results.jsonl을 동일 계약으로 재평가")
    evaluate.add_argument("manifest")
    evaluate.add_argument("results")
    evaluate.add_argument("--annotations")

    prepare = subparsers.add_parser(
        "prepare-merge-annotations",
        help="값 판독 주석을 보존하면서 병합 A/B용 사람 검수 계약 생성",
    )
    prepare.add_argument("manifest")
    prepare.add_argument("--base", help="기존 annotations JSONL; 생략 시 표본 폴더 기본 템플릿")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--force", action="store_true")

    merge_ab = subparsers.add_parser(
        "merge-ab",
        help="동일 Vision 결과로 기존 병합과 근거 기반 병합을 무API 비교",
    )
    merge_ab.add_argument("manifest")
    merge_ab.add_argument("--results", required=True)
    merge_ab.add_argument("--annotations", required=True)
    merge_ab.add_argument("--municipality", default="서울특별시")
    merge_ab.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            dataset = build_fixed_sample_dataset(
                args.source_pdf,
                args.inventory,
                args.output_dir,
                dataset_id=args.dataset_id,
                audit_path=args.audit,
                failure_statuses=args.failure_status or ("vision_유실",),
                positive_controls=args.positive_controls,
                negative_controls=args.negative_controls,
                dpi=args.dpi,
                materialize_images=True,
                force=args.force,
            )
            print(json.dumps({
                "manifest": str(dataset.manifest_path),
                "dataset_id": dataset.dataset_id,
                "fingerprint": dataset.fingerprint,
                "objects": len(dataset.items),
                "counts": dataset.payload.get("counts", {}),
            }, ensure_ascii=False))
            return 0
        if args.command == "validate":
            report = validate_fixed_sample_dataset(
                args.manifest,
                source_override=args.source,
                require_images=not args.allow_missing_images,
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return 0 if report.valid else 2
        if args.command == "run":
            return run_dataset(args)
        if args.command == "prepare-merge-annotations":
            return prepare_merge_annotations_command(args)
        if args.command == "merge-ab":
            return run_merge_ab(args)
        dataset = load_fixed_sample_dataset(args.manifest)
        evaluation = evaluate_fixed_sample_results(
            dataset,
            load_result_rows(args.results),
            load_annotations(args.annotations),
        )
        print(json.dumps(evaluation, ensure_ascii=False, indent=2))
        return 0
    except (FixedSampleError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        print(f"고정 표본 처리 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
