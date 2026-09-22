"""재현 가능한 실행 매니페스트와 배치 체크포인트를 관리한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config


FAILED_BATCH_STATUSES = frozenset({"call_fail", "parse_fail", "partial", "unresolved"})
_VOLATILE_ROW_FIELDS = frozenset({"_row_id", "_entity_id"})

_ENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "document_meta": ("지자체명", "계획명"),
    "plan_overview": ("지자체명", "개요유형", "항목명", "일자"),
    "regional_conditions": ("지자체명", "지표범주", "지표명", "연도"),
    "emissions_regional": ("지자체명", "배출유형", "부문", "세부부문", "연도"),
    "emissions_management": ("지자체명", "관리부문", "직간접구분", "연도"),
    "emissions_forecast": ("지자체명", "시나리오", "부문", "연도"),
    "reduction_targets": ("지자체명", "목표수준", "목표범위", "부문", "기준연도", "목표연도"),
    "vision_strategy": ("지자체명", "전략수준", "전략명"),
    "mitigation_projects": ("지자체명", "관리번호", "사업명", "부문"),
    "annual_implementation": ("지자체명", "관리번호", "사업명", "연도"),
    "quantitative_reductions": ("지자체명", "관리번호", "사업명", "연도", "기간시작", "기간종료", "시간기준"),
    "financial_plan": ("지자체명", "관리번호", "사업명", "재원구분", "연도", "기간시작", "기간종료", "시간기준", "집계대상원문", "부문"),
    "foundation_measures": ("지자체명", "대응기반영역", "과제명"),
    "governance_feedback": ("지자체명", "거버넌스기구", "역할"),
    "monitoring_performance": ("지자체명", "점검연도", "관리번호", "사업명"),
    "changes_actions": ("지자체명", "점검연도", "관리번호", "사업명"),
    "visual_inventory": ("지자체명", "시각자료ID", "출처페이지", "캡션"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(child) for child in value]
    if isinstance(value, (set, frozenset)):
        converted = [_canonical_value(child) for child in value]
        return sorted(converted, key=lambda child: json.dumps(child, ensure_ascii=False, sort_keys=True, default=_json_default))
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path | None) -> str:
    if path is None:
        return ""
    source = Path(path)
    if not source.exists() or not source.is_file():
        return ""
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hash() -> str:
    project_root = Path(__file__).resolve().parents[1]
    paths = [
        project_root / "config.py",
        project_root / "main.py",
        project_root / "data" / "reading_mapping_rules_v1.json",
    ]
    for package in ("agents", "utils"):
        paths.extend(sorted((project_root / package).rglob("*.py")))
    return sha256_json({str(path.relative_to(project_root)): sha256_file(path) for path in paths})


def _state_root() -> Path:
    configured = str(getattr(config, "RUN_STATE_DIR", ".cache/runs") or ".cache/runs")
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as stream:
        stream.write(encoded)
        temporary = Path(stream.name)
    temporary.replace(path)


def canonical_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in row.items()
        if str(key) not in _VOLATILE_ROW_FIELDS
    }


def stable_row_id(sheet_key: str, row: dict[str, Any]) -> str:
    return sha256_json({"sheet_key": sheet_key, "row": canonical_row_payload(row)})


def stable_entity_id(sheet_key: str, row: dict[str, Any]) -> str:
    fields = _ENTITY_FIELDS.get(sheet_key, ())
    if row.get('정보유형'):
        fields = (*fields, '정보유형', '항목원문', '상위기관원문', '구성경로원문', '감축원단위명', '감축원단위단위', '집계대상원문')
    values = {field: row.get(field) for field in fields if row.get(field) not in (None, "")}
    minimum = 1 if sheet_key == "document_meta" else 2
    if len(values) < minimum:
        return ""
    return sha256_json({"sheet_key": sheet_key, "entity": values})


def _source_page_key(row: dict[str, Any]) -> tuple[int, str]:
    value = row.get("출처페이지")
    numbers: list[int] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            try:
                numbers.append(int(item))
            except (TypeError, ValueError):
                continue
    elif value not in (None, ""):
        numbers = [int(token) for token in re.findall(r"\d+", str(value))]
    return (min(numbers) if numbers else 10**9, _canonical_json(canonical_row_payload(row)))


def merge_rows_stably(
    sheet_key: str,
    existing: Iterable[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """정확히 같은 행은 제거하고, 같은 엔터티의 값 충돌은 둘 다 보존한다."""
    merged: dict[str, dict[str, Any]] = {}
    entities: dict[str, str] = {}
    conflicts: list[dict[str, str]] = []
    def add_row(source: dict[str, Any], *, report_conflict: bool) -> None:
        if not isinstance(source, dict):
            return
        row = dict(source)
        row_id = stable_row_id(sheet_key, row)
        if row_id in merged:
            return
        entity_id = stable_entity_id(sheet_key, row)
        previous_row_id = entities.get(entity_id) if entity_id else None
        if report_conflict and previous_row_id and previous_row_id != row_id:
            conflicts.append({
                "sheet_key": sheet_key,
                "entity_id": entity_id,
                "kept_row_id": previous_row_id,
                "incoming_row_id": row_id,
            })
        elif entity_id:
            entities[entity_id] = row_id
        merged[row_id] = row

    for source in existing:
        add_row(source, report_conflict=False)
    for source in incoming:
        add_row(source, report_conflict=True)
    rows = sorted(merged.values(), key=_source_page_key)
    conflicts.sort(key=lambda item: (item["sheet_key"], item["entity_id"], item["incoming_row_id"]))
    return rows, conflicts


def _has_checkpoint_payload(result: Any) -> bool:
    if isinstance(result, list):
        return bool(result)
    if isinstance(result, dict):
        return any(_has_checkpoint_payload(value) for value in result.values())
    return result not in (None, "")


def _merge_checkpoint_payloads(
    kind: str,
    sheet_keys: list[str],
    existing: Any,
    incoming: Any,
) -> Any:
    """부분 체크포인트끼리 합치되 완전 성공 결과의 권위는 건드리지 않는다."""
    if kind == "sheet" and isinstance(existing, list):
        incoming_rows = incoming if isinstance(incoming, list) else []
        sheet_key = sheet_keys[0] if sheet_keys else "unknown"
        rows, _ = merge_rows_stably(sheet_key, existing, incoming_rows)
        return rows
    if kind == "cluster" and isinstance(existing, dict):
        incoming_by_sheet = incoming if isinstance(incoming, dict) else {}
        merged: dict[str, Any] = {}
        for sheet_key in sorted(set(existing) | set(incoming_by_sheet)):
            previous_rows = existing.get(sheet_key)
            fresh_rows = incoming_by_sheet.get(sheet_key)
            if isinstance(previous_rows, list):
                rows, _ = merge_rows_stably(
                    sheet_key,
                    previous_rows,
                    fresh_rows if isinstance(fresh_rows, list) else [],
                )
                merged[sheet_key] = rows
            else:
                merged[sheet_key] = fresh_rows if fresh_rows is not None else previous_rows
        return merged
    return incoming if _has_checkpoint_payload(incoming) else existing


def _coalesce_batch_record(
    previous: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """실패한 재시도가 이전 부분 성공 결과를 지우지 않도록 원장 레코드를 합친다."""
    if not previous or incoming.get("status") == "ok":
        return incoming
    if (
        previous.get("status") not in FAILED_BATCH_STATUSES
        or incoming.get("status") not in FAILED_BATCH_STATUSES
        or not (_has_checkpoint_payload(previous.get("result")) or previous.get("member_statuses"))
    ):
        return incoming

    record = dict(incoming)
    record["member_statuses"] = {**previous.get("member_statuses", {}), **incoming.get("member_statuses", {})}
    record["result"] = _merge_checkpoint_payloads(
        str(incoming.get("kind") or previous.get("kind") or ""),
        [str(value) for value in (incoming.get("sheet_keys") or previous.get("sheet_keys") or [])],
        previous.get("result"),
        incoming.get("result"),
    )
    if not _has_checkpoint_payload(record["result"]) and not record.get("member_statuses"):
        return incoming

    record["status"] = "partial"
    record["recovered"] = True
    marker = "이전 부분 결과 보존"
    latest_error = str(incoming.get("error") or "")
    record["error"] = (
        latest_error if marker in latest_error
        else f"{latest_error} | {marker}".strip(" |")
    )[:1000]
    return record


class RunState:
    """동일 입력·프롬프트·모델 조합의 배치 실행 상태를 영속화한다."""

    def __init__(
        self,
        *,
        run_id: str,
        root: Path,
        manifest: dict[str, Any],
        resume: bool,
        retry_failed_only: bool,
    ) -> None:
        self.run_id = run_id
        self.root = root
        self.ledger_path = root / "ledger.jsonl"
        self.manifest_path = root / "run_manifest.json"
        self.conflicts_path = root / "merge_conflicts.jsonl"
        self.resume = resume
        self.retry_failed_only = retry_failed_only
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._expected: dict[str, dict[str, Any]] = {}
        self._conflict_count = 0
        self._conflict_ids: set[str] = set()

        root.mkdir(parents=True, exist_ok=True)
        if resume:
            self._load_ledger()
        else:
            self.ledger_path.write_text("", encoding="utf-8")
            self.conflicts_path.write_text("", encoding="utf-8")
        self._load_conflicts()
        self._conflict_count = len(self._conflict_ids)
        self.manifest = dict(manifest)
        self.manifest.update({
            "run_id": run_id,
            "state_directory": str(root),
            "resume": resume,
            "retry_failed_only": retry_failed_only,
            "status": "running",
            "updated_at": _utc_now(),
        })
        _atomic_write_json(self.manifest_path, self.manifest)

    @classmethod
    def create(
        cls,
        *,
        input_path: str | Path,
        guideline_path: str | Path | None,
        extraction_prompts: dict[str, str],
        execution_info: dict[str, Any],
        output_path: str | Path,
        resume: bool = False,
        retry_failed_only: bool = False,
    ) -> "RunState":
        input_path = Path(input_path)
        config_snapshot = {
            "pipeline_version": getattr(config, "PIPELINE_VERSION", ""),
            "batch_size": getattr(config, "BATCH_SIZE", None),
            "full_document_scan": getattr(config, "FULL_DOCUMENT_SCAN", False),
            "sheet_clustering": getattr(config, "EXTRACTION_SHEET_CLUSTERING", False),
            "route_min_score": getattr(config, "DOCUMENT_ROUTE_MIN_SCORE", None),
            "route_context_pages": getattr(config, "DOCUMENT_ROUTE_CONTEXT_PAGES", None),
            "text_workers": getattr(config, "TEXT_WORKERS", None),
            "temperature": getattr(config, "LLM_TEMPERATURE", None),
            "cache_version": getattr(config, "LLM_CACHE_VERSION", ""),
            "reading_pipeline_enabled": getattr(config, "READING_PIPELINE_ENABLED", True),
            "text_backend": execution_info.get("text_backend", ""),
            "text_model": execution_info.get("text_model", ""),
            "vision_backend": execution_info.get("vision_backend", ""),
            "vision_model": execution_info.get("vision_model", ""),
        }
        from utils.vision_review import policy_snapshot
        config_snapshot["vision_review_policy"] = policy_snapshot()
        from utils.vision_toc import policy_snapshot as toc_policy_snapshot
        config_snapshot["vision_toc_policy"] = toc_policy_snapshot()
        from utils.text_optimization import policy_snapshot as text_policy_snapshot
        config_snapshot["text_optimization_policy"] = text_policy_snapshot()
        config_snapshot["sheet_clusters"] = getattr(config, "EXTRACTION_SHEET_CLUSTERS", [])
        manifest = {
            "created_at": _utc_now(),
            "input_path": str(input_path.resolve()),
            "input_sha256": sha256_file(input_path),
            "guideline_path": str(Path(guideline_path).resolve()) if guideline_path else "",
            "guideline_sha256": sha256_file(guideline_path),
            "prompt_sha256": sha256_json(extraction_prompts),
            "implementation_sha256": _implementation_hash(),
            "execution_info": execution_info,
            "config": config_snapshot,
            "requested_output": str(Path(output_path)),
        }
        fingerprint = sha256_json({
            "input": manifest["input_sha256"],
            "guideline": manifest["guideline_sha256"],
            "prompt": manifest["prompt_sha256"],
            "implementation": manifest["implementation_sha256"],
            "config": config_snapshot,
        })
        run_id = fingerprint[:24]
        return cls(
            run_id=run_id,
            root=_state_root() / run_id,
            manifest=manifest,
            resume=bool(resume or retry_failed_only),
            retry_failed_only=bool(retry_failed_only),
        )

    @staticmethod
    def _count_jsonl(path: Path) -> int:
        try:
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    def _load_ledger(self) -> None:
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            batch_id = str(record.get("batch_id") or "")
            if batch_id:
                self._records[batch_id] = _coalesce_batch_record(
                    self._records.get(batch_id),
                    record,
                )

    def _load_conflicts(self) -> None:
        try:
            lines = self.conflicts_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            conflict_id = str(entry.get("conflict_id") or sha256_json({
                key: value for key, value in entry.items() if key != "conflict_id"
            }))
            self._conflict_ids.add(conflict_id)

    def batch_id(
        self,
        *,
        kind: str,
        sheet_keys: Iterable[str],
        page_nums: Iterable[int],
        batch_text: str,
        request_fingerprint: str = "",
    ) -> str:
        return sha256_json({
            "run_id": self.run_id,
            "kind": kind,
            "sheet_keys": sorted(str(key) for key in sheet_keys),
            "page_nums": [int(page) for page in page_nums],
            "batch_text_sha256": hashlib.sha256((batch_text or "").encode("utf-8")).hexdigest(),
            "request_fingerprint": str(request_fingerprint or ""),
        })[:32]

    def register_expected(self, batch_id: str, metadata: dict[str, Any]) -> None:
        with self._lock:
            self._expected[batch_id] = dict(metadata)

    def record_for(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(batch_id)
            return dict(record) if record else None

    def restored_result(self, batch_id: str) -> Any | None:
        if not self.resume:
            return None
        record = self.record_for(batch_id)
        if not record or record.get("status") != "ok":
            return None
        return record.get("result")

    def should_execute(self, batch_id: str, *, is_child: bool = False) -> bool:
        if not self.resume or is_child:
            return True
        record = self.record_for(batch_id)
        if record and record.get("status") == "ok" and record.get("result") is not None:
            return False
        if self.retry_failed_only:
            return bool(record and record.get("status") in FAILED_BATCH_STATUSES)
        return True

    def record_batch(
        self,
        *,
        batch_id: str,
        kind: str,
        sheet_keys: Iterable[str],
        page_nums: Iterable[int],
        status: str,
        result: Any = None,
        error: str = "",
        split_depth: int = 0,
        recovered: bool = False,
        member_statuses: dict[str, str] | None = None,
    ) -> None:
        incoming = {
            "batch_id": batch_id,
            "kind": kind,
            "sheet_keys": sorted(str(key) for key in sheet_keys),
            "page_nums": [int(page) for page in page_nums],
            "status": status,
            "result": result,
            "error": str(error)[:1000],
            "split_depth": int(split_depth),
            "recovered": bool(recovered),
            "member_statuses": dict(member_statuses or {}),
            "updated_at": _utc_now(),
        }
        with self._lock:
            record = _coalesce_batch_record(self._records.get(batch_id), incoming)
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, default=_json_default)
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._records[batch_id] = record

    def record_conflicts(self, conflicts: Iterable[dict[str, str]]) -> None:
        with self._lock:
            entries: list[dict[str, str]] = []
            for source in conflicts:
                entry = {key: value for key, value in dict(source).items() if key != "conflict_id"}
                conflict_id = sha256_json(entry)
                if conflict_id in self._conflict_ids:
                    continue
                entry["conflict_id"] = conflict_id
                self._conflict_ids.add(conflict_id)
                entries.append(entry)
            if not entries:
                return
            with self.conflicts_path.open("a", encoding="utf-8") as stream:
                for entry in entries:
                    stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._conflict_count = len(self._conflict_ids)

    def summary(self, *, kinds: set[str] | None = None) -> dict[str, int]:
        with self._lock:
            expected_ids = {
                batch_id
                for batch_id, metadata in self._expected.items()
                if kinds is None or str(metadata.get("kind") or "") in kinds
            }
            statuses = [str(self._records.get(batch_id, {}).get("status", "missing")) for batch_id in expected_ids]
        return {
            "expected": len(expected_ids),
            "ok": sum(status == "ok" for status in statuses),
            "failed": sum(status in FAILED_BATCH_STATUSES for status in statuses),
            "missing": sum(status == "missing" for status in statuses),
            "conflicts": self._conflict_count,
        }

    def completion_status(self) -> str:
        summary = self.summary()
        if summary["expected"] == 0 or summary["ok"] == 0:
            return "failed"
        if summary["failed"] or summary["missing"]:
            return "partial"
        if summary["conflicts"]:
            return "needs_review"
        return "complete"

    def finalize(
        self,
        *,
        output_path: str | Path,
        semantic_result_hash: str,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        summary = self.summary()
        self.manifest.update({
            "status": self.completion_status(),
            "completed_at": _utc_now(),
            "updated_at": _utc_now(),
            "output_path": str(Path(output_path)),
            "output_sha256": sha256_file(output_path),
            "semantic_result_sha256": semantic_result_hash,
            "batch_summary": summary,
        })
        if extra:
            self.manifest.update(extra)
        _atomic_write_json(self.manifest_path, self.manifest)
        sidecar = Path(output_path).with_name(f"{Path(output_path).stem}_run_manifest.json")
        _atomic_write_json(sidecar, self.manifest)
        return sidecar

    def mark_interrupted(self, reason: str = "") -> None:
        """정상 finalize 전에 종료된 실행도 다음 재개의 근거로 남긴다."""
        self.manifest.update({
            "status": "interrupted",
            "interrupted_at": _utc_now(),
            "updated_at": _utc_now(),
            "error": str(reason)[:2000],
            "batch_summary": self.summary(),
        })
        _atomic_write_json(self.manifest_path, self.manifest)
