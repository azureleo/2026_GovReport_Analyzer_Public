"""
에이전트 5: 감독·검수 에이전트

전체 파이프라인을 조율하고 결과의 품질을 검수합니다.
carbon_guideline.md 기반 16개 시트 구조에 맞춰 동작합니다.
"""
# noqa: SIZE_OK — 파이프라인 단계 조율을 한 파일에 유지하는 기존 supervisor.

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from utils.pdf_reader import extract_pdf, PDFContent, PageContent
from utils.hwp_reader import extract_hwp, is_hwp_file
from utils import llm_cache, llm_client, parallel
from utils.pipeline_artifacts import PipelineArtifacts
from utils.run_state import RunState, sha256_json
from utils.visual_merge_ab import write_visual_merge_snapshot
from utils.semantic_routing import SemanticRoutingReport, validate_and_reclassify
from utils.source_verifier import (
    QualityAssessment,
    SourceObjectInventoryReport,
    SourceVerificationReport,
    assess_quality,
    build_source_object_inventory,
    create_marked_pdf,
    verify_final_data,
)
from agents.guideline_agent import GuidelineAgent
from agents.extractor_agent import ExtractorAgent
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent, detect_prior_plan_pages
from agents.gap_fill_agent import GapFillAgent
from agents.hybrid_review_agent import HybridReviewAgent, reconcile_reflected_merge_log
from agents.excel_agent import ExcelAgent

logger = logging.getLogger(__name__)

QUALITY_SYSTEM = """당신은 지자체 탄소중립 기본계획 데이터 품질 검수 전문가입니다.
반드시 JSON만 반환하세요."""


_TIMING_ORDER = [
    "문서 파싱",
    "가이드라인 로드",
    "실행 산출물 구성",
    "텍스트 추출",
    "이미지 분석",
    "정리·정제",
    "빈칸 보완",
    "시트 단위 보조검수·병합",
    "보조 모델 검수",
    "보조 후보 판정·병합",
    "시트 의미 검증",
    "원문 대조",
    "원문 객체 인벤토리",
    "엑셀 작성",
    "마킹 PDF",
    "LLM 최종 검수",
]


def _fmt_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}초"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}분 {sec:.1f}초"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}시간 {minutes}분 {sec:.1f}초"


def _safe_stage_provider(stage: str) -> str:
    try:
        return llm_client._resolve_provider(stage)
    except RuntimeError:
        configured = getattr(config, "STAGE_PROVIDERS", {}).get(stage, "") or getattr(config, "LLM_PROVIDER", "")
        return str(configured or "미상")


def _safe_stage_model(provider: str, stage: str) -> str:
    try:
        return llm_client._model_identity(provider, stage) or "기본값"
    except RuntimeError:
        configured = getattr(config, "STAGE_MODELS", {}).get(stage, "") or getattr(config, "LOCAL_AGENT_MODEL", "")
        return str(configured or "기본값")


def _git_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "미상"
    return result.stdout.strip() or "미상"


def _execution_info(input_path: Path, started_at: datetime) -> dict[str, str]:
    text_provider = _safe_stage_provider("extraction")
    vision_provider = _safe_stage_provider("vision")
    return {
        "git_commit": _git_commit_hash(),
        "text_backend": text_provider,
        "text_model": _safe_stage_model(text_provider, "extraction"),
        "vision_backend": vision_provider,
        "vision_model": _safe_stage_model(vision_provider, "vision"),
        "run_started_at": started_at.isoformat(timespec="seconds"),
        "input_file": input_path.name,
        "pipeline_version": str(getattr(config, "PIPELINE_VERSION", "v5.3")),
    }


def _ledger_quality_metrics(
    records: list,
    run_state: RunState | None = None,
) -> dict[str, int]:
    # 분할 자식은 복구 수단이지 별도의 원본 작업이 아니다. 품질 분모에는 최초
    # sheet/cluster 루트 배치만 넣어 재시도할수록 점수가 내려가는 현상을 막는다.
    if run_state is not None:
        root_summary = run_state.summary(kinds={"sheet", "cluster"})
        if root_summary["expected"]:
            return {
                "extraction_batches_total": root_summary["expected"],
                "extraction_batches_ok": root_summary["ok"],
                "extraction_batches_failed": root_summary["failed"],
                "extraction_batches_missing": root_summary["missing"],
            }

    def status_of(record) -> str:
        if isinstance(record, dict):
            return str(record.get("status", ""))
        return str(getattr(record, "status", ""))

    statuses = [status_of(record) for record in records]
    return {
        "extraction_batches_total": len(statuses),
        "extraction_batches_ok": sum(status == "ok" for status in statuses),
        "extraction_batches_call_fail": sum(status == "call_fail" for status in statuses),
        "extraction_batches_parse_fail": sum(status == "parse_fail" for status in statuses),
    }


def _deduplicate_validation_rows(rows: list) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for source in rows:
        if not isinstance(source, dict):
            continue
        row = dict(source)
        identity = sha256_json(row)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def _quality_score(
    final_data: dict,
    verification: SourceVerificationReport | None = None,
    source_inventory: SourceObjectInventoryReport | None = None,
) -> tuple[float, list[str]]:
    """호환용 래퍼. 실제 점수는 결정론적 품질 지표에서 계산한다."""
    assessment = assess_quality(final_data, verification, source_inventory)
    return assessment.score, assessment.issues


def _document_text_chars(pdf_content: PDFContent) -> int:
    return len("".join(page.text or "" for page in pdf_content.pages).strip())


class Supervisor:
    """에이전트 5: 감독·검수 에이전트"""

    QUALITY_THRESHOLD = 70.0

    def __init__(self):
        self._reports: list[str] = []
        self._timings: dict[str, float] = {}
        self._quality_assessment_for_review: QualityAssessment | None = None
        self._active_run_state: RunState | None = None
        self._artifact_stats: dict = {}
        self._vision_render_stats: dict = {}

    def mark_interrupted(self, reason: str = "") -> None:
        if self._active_run_state is not None:
            self._active_run_state.mark_interrupted(reason)

    def _log(self, msg: str):
        print(msg)
        self._reports.append(msg)

    def _add_timing(self, label: str, elapsed: float):
        self._timings[label] = self._timings.get(label, 0.0) + elapsed

    def _add_vision_render_stats(self, metrics: dict) -> None:
        for key in (
            "render_requests",
            "render_unique",
            "render_reused",
            "render_seconds",
            "render_png_bytes",
        ):
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                self._vision_render_stats[key] = self._vision_render_stats.get(key, 0) + value

    def _append_text_layer_warning(self, final_data: dict) -> None:
        report = final_data.setdefault("validation_report", [])
        if not isinstance(report, list):
            return
        if any(isinstance(row, dict) and row.get("항목") == "텍스트 레이어 부족" for row in report):
            return
        municipality = final_data.get("municipality_name", "알 수 없음")
        report.append({
            "지자체명": municipality,
            "심각도": "정보",
            "영역": "문서파싱",
            "항목": "텍스트 레이어 부족",
            "문제내용": "텍스트 레이어가 거의 없습니다 — 스캔본 PDF일 수 있으며 추출 결과가 비어 있을 수 있습니다",
            "권장조치": "OCR 또는 텍스트 레이어가 있는 PDF로 다시 실행 후 결과를 비교",
        })

    def _log_timing_summary(self, total_elapsed: float):
        self._log("\n[감독관] 단계별 소요 시간")
        for label in _TIMING_ORDER:
            if label in self._timings:
                self._log(f"  - {label}: {_fmt_seconds(self._timings[label])}")
        other_elapsed = sum(
            elapsed for label, elapsed in self._timings.items()
            if label not in _TIMING_ORDER
        )
        if other_elapsed > 0:
            self._log(f"  - 기타: {_fmt_seconds(other_elapsed)}")
        self._log(f"  - 전체: {_fmt_seconds(total_elapsed)}")

        if self._artifact_stats:
            self._log(
                "  - 실행 산출물: "
                f"DocumentObject {self._artifact_stats.get('document_object_count', 0):,}개, "
                f"가이드라인 프롬프트 {self._artifact_stats.get('guideline_prompt_count', 0):,}개/"
                f"{self._artifact_stats.get('guideline_prompt_chars', 0):,}자"
            )
        if self._vision_render_stats:
            self._log(
                "  - Vision 객체 렌더: "
                f"요청 {int(self._vision_render_stats.get('render_requests', 0)):,}, "
                f"실제 {int(self._vision_render_stats.get('render_unique', 0)):,}, "
                f"재사용 {int(self._vision_render_stats.get('render_reused', 0)):,}, "
                f"누적 {_fmt_seconds(float(self._vision_render_stats.get('render_seconds', 0.0)))}"
            )
        queue_stats = parallel.get_parallel_stats()
        for label, values in queue_stats.items():
            self._log(
                f"  - 작업 큐[{label}]: 제출 {values.get('submitted', 0)}, "
                f"시작 {values.get('started', 0)}, 완료 {values.get('completed', 0)}, "
                f"대기 누적 {_fmt_seconds(float(values.get('queue_wait_seconds', 0.0)))}, "
                f"최대 {_fmt_seconds(float(values.get('max_queue_wait_seconds', 0.0)))}"
            )

        stats = llm_cache.get_cache_stats()
        if stats:
            self._log(
                "  - LLM 캐시: "
                f"hit {stats.get('hit', 0)}, "
                f"miss {stats.get('miss', 0)}, "
                f"write {stats.get('write', 0)}, "
                f"reject {stats.get('rejected', 0)}, "
                f"disabled {stats.get('disabled', 0)}, "
                f"동시중복 합침 {stats.get('coalesced', 0)} "
                f"(대기 누적 {_fmt_seconds(float(stats.get('singleflight_wait_seconds', 0.0)))})"
            )
        llm_stats = llm_client.get_llm_stats()
        if llm_stats:
            wait_seconds = llm_stats.get("quota_wait_seconds", 0.0)
            self._log(
                "  - LLM 호출: "
                f"{llm_stats.get('total_calls', 0)}회"
                f"(실패 {llm_stats.get('failures', 0)}, "
                f"재시도 {llm_stats.get('retries', 0)}, "
                f"타임아웃 {llm_stats.get('timeouts', 0)}), "
                f"quota 대기 누적 {_fmt_seconds(float(wait_seconds))}"
            )
            if llm_stats.get("capacity_errors", 0) or llm_stats.get("capacity_circuit_rejected", 0):
                self._log(
                    "  - 모델 과부하: "
                    f"감지 {llm_stats.get('capacity_errors', 0)}, "
                    f"회로 개방 {llm_stats.get('capacity_circuit_opened', 0)}, "
                    f"차단 {llm_stats.get('capacity_circuit_rejected', 0)}, "
                    f"fallback {llm_stats.get('capacity_fallback_successes', 0)}/"
                    f"{llm_stats.get('capacity_fallback_calls', 0)} 성공"
                )
            call_seconds = sum(
                float(value) for value in llm_stats.get("call_seconds", {}).values()
            )
            input_chars = sum(
                int(value) for value in llm_stats.get("input_chars", {}).values()
            )
            image_count = sum(
                int(value) for value in llm_stats.get("image_count", {}).values()
            )
            self._log(
                "  - LLM 실호출 누적: "
                f"{_fmt_seconds(call_seconds)}, 입력 {input_chars:,}자, 이미지 {image_count:,}개"
            )

    def _llm_quality_review(self, final_data: dict) -> dict:
        assessment = self._quality_assessment_for_review or assess_quality(final_data)
        municipality = final_data.get("municipality_name", "?")
        internal_keys = getattr(config, "INTERNAL_OBJECT_KEYS", set())
        sheet_counts = {
            k: len(v) for k, v in final_data.items()
            if isinstance(v, list) and v and k not in internal_keys
        }
        prompt = f"""다음은 '{municipality}' 탄소중립 계획 추출 결과 요약입니다:
- 시트별 행 수: {json.dumps(sheet_counts, ensure_ascii=False)}
- 총 추출 행: {sum(sheet_counts.values())}
- 결정론적 품질 점수: {assessment.score:.1f}/100
- 결정론적 품질 지표: {json.dumps(assessment.metrics, ensure_ascii=False)}
- 결정론적 이슈: {json.dumps(assessment.issues, ensure_ascii=False)}

이 점수는 행별 원문 근거, 원문 객체 완전성, 필수 필드 채움률, 출처페이지,
정합성, 핵심 시트 존재를 코드로 계산한 결과입니다. 다만 셀 정확도는 명시적인
고정 골든셋 평가를 실행해야만 산출됩니다. 행 수나 배치 성공률만 보고 완전하거나
누락이 없다고 판단하지 마세요.

위 근거를 해석해 JSON으로 반환하세요. 결정론적 점수를 임의로 대체하지 마세요:
{{
  "assessment": "평가 의견 (2~3문장)",
  "quality_level": "우수|보통|미흡",
  "key_issues": ["이슈1", "이슈2"]
}}"""
        try:
            resp = llm_client.call_text(prompt, system=QUALITY_SYSTEM)
        except llm_client.LLMQuotaExceededError:
            raise
        except llm_client.LLMCallError as exc:
            logger.warning("LLM 최종 품질 검수 실패. 규칙 기반 점수만 사용: %s", exc)
            return {
                "assessment": f"LLM 검수 호출이 실패하여 결정론적 품질 점수({assessment.score:.1f}/100)만 기록했습니다.",
                "quality_level": "우수" if assessment.score >= 85 else "보통" if assessment.score >= 70 else "미흡",
                "key_issues": assessment.issues,
            }
        return llm_client.parse_json(resp)

    def _run_sheet_closed_loop(
        self,
        *,
        pages: list[PageContent],
        full_text: str,
        extraction_prompts: dict[str, str],
        run_state: RunState | None = None,
        document_objects: tuple | None = None,
    ) -> tuple[dict, ExtractorAgent, list[dict], list[dict]]:
        """
        시트별 추출·정제·검수 폐루프.

        이미지 분석과 빈칸 보완은 이 폐루프 이후 실행되므로 보조검수가 본 기준본과
        최종본은 다를 수 있다. 최종 organize 검증 패스가 최종본 기준 정합성을 다시 점검한다.
        """
        # 기존 테스트·확장 코드가 무인자 팩토리를 주입할 수 있으므로 새 상태가
        # 실제로 있을 때만 생성자 인자를 전달한다.
        extractor = (
            ExtractorAgent(run_state=run_state, document_objects=document_objects)
            if run_state is not None or document_objects is not None
            else ExtractorAgent()
        )
        organizer = OrganizerAgent()
        hybrid_agent = HybridReviewAgent() if getattr(config, "HYBRID_REVIEW_ENABLED", False) else None
        municipality = extractor._extract_municipality_name(full_text)
        raw_data: dict = {"municipality_name": municipality}
        review_candidates: list[dict] = []
        merge_log: list[dict] = []

        self._log(f"[감독관] 시트별 폐루프 지자체명: {municipality}")
        routed_pages = extractor.route_pages(pages)
        route_summary = {key: len(value) for key, value in routed_pages.items() if value}
        self._log(f"[감독관] 시트별 폐루프 라우팅 완료: {route_summary}")

        review_sheets = set(getattr(config, "HYBRID_REVIEW_SHEETS", []))
        review_enabled = bool(hybrid_agent)
        adjudication_limit = max(0, getattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0))
        remaining_candidates: int | None = None if adjudication_limit <= 0 else adjudication_limit

        for sheet_key in config.EXTRACTION_SHEETS:
            sheet_pages = routed_pages.get(sheet_key, [])
            if not sheet_pages:
                continue
            sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, sheet_key)
            self._log(f"[감독관] 폐루프 {sheet_name}: 추출→정제 시작")
            try:
                rows = extractor.extract_sheet_pages(
                    sheet_key,
                    sheet_pages,
                    municipality,
                    extraction_prompts,
                )
            except llm_client.LLMQuotaExceededError as exc:
                logger.warning("시트별 폐루프 추출 quota/한도 문제로 부분 결과를 보존하고 종료: %s", exc)
                self._log(f"[감독관] 폐루프 {sheet_name}: 추출 quota 발생, 완료 시트만 보존")
                break
            raw_data[sheet_key] = organizer.organize_sheet(sheet_key, rows, municipality)

            if not review_enabled or sheet_key not in review_sheets:
                continue
            if remaining_candidates is not None and remaining_candidates <= 0:
                self._log(f"[감독관] 폐루프 {sheet_name}: 전역 판정 상한 도달로 검수 건너뜀")
                continue

            sheet_basis = {
                "municipality_name": municipality,
                sheet_key: [dict(row) for row in raw_data.get(sheet_key, []) if isinstance(row, dict)],
            }
            try:
                reviewed_data, sheet_candidates, sheet_merge_log = hybrid_agent.review_and_adjudicate_by_sheet(
                    pages=pages,
                    final_data=sheet_basis,
                    extraction_prompts=extraction_prompts,
                    progress=self._log,
                    target_sheets=[sheet_key],
                    routed_pages={sheet_key: sheet_pages},
                    max_candidates_override=remaining_candidates,
                )
            except llm_client.LLMQuotaExceededError as exc:
                sheet_candidates = list(getattr(hybrid_agent, "_last_review_candidates", []))
                sheet_merge_log = list(getattr(hybrid_agent, "_last_merge_log", []))
                review_candidates.extend(sheet_candidates)
                merge_log.extend(sheet_merge_log)
                if isinstance(sheet_basis.get(sheet_key), list):
                    raw_data[sheet_key] = sheet_basis[sheet_key]
                review_enabled = False
                logger.warning("시트별 폐루프 검수 quota/한도 문제로 남은 시트 검수 건너뜀: %s", exc)
                self._log(f"[감독관] 폐루프 {sheet_name}: quota 발생, 남은 시트 검수 비활성")
                continue

            review_candidates.extend(sheet_candidates)
            merge_log.extend(sheet_merge_log)
            if remaining_candidates is not None:
                remaining_candidates = max(0, remaining_candidates - len(sheet_merge_log))

            sheet_rows = reviewed_data.get(sheet_key, [])
            if isinstance(sheet_rows, list):
                raw_data[sheet_key] = [dict(row) for row in sheet_rows if isinstance(row, dict)]
            if any(row.get("최종반영여부") == "반영" for row in sheet_merge_log):
                raw_data[sheet_key] = organizer.organize_sheet(sheet_key, raw_data[sheet_key], municipality)
                reconcile_reflected_merge_log(
                    {"municipality_name": municipality, sheet_key: raw_data[sheet_key]},
                    sheet_merge_log,
                )

        return raw_data, extractor, review_candidates, merge_log

    def run(
        self,
        input_path: str | Path = None,
        output_path: str | Path = None,
        hwp_path: str | None = None,
        max_pipeline_retries: int = 2,
        include_images: bool = True,
        pdf_path: str | Path = None,
    ) -> Path:
        if input_path is None and pdf_path is not None:
            input_path = pdf_path
        if input_path is None:
            raise ValueError("입력 파일 경로를 지정해주세요.")

        input_path = Path(input_path)
        output_path = Path(output_path)

        file_type = "HWP" if is_hwp_file(input_path) else "PDF"
        pipeline_start = time.time()
        run_started_at = datetime.now(timezone.utc).astimezone()
        execution_info = _execution_info(input_path, run_started_at)
        self._timings = {}
        self._artifact_stats = {}
        self._vision_render_stats = {}
        llm_cache.reset_cache_stats()
        llm_client.reset_llm_stats()
        parallel.reset_parallel_stats()

        self._log("=" * 60)
        self._log("[감독관] 파이프라인 시작 (가이드라인 기반 16개 시트)")
        self._log(f"  - 입력 {file_type}: {input_path}")
        self._log(f"  - 출력 경로: {output_path}")
        self._log("=" * 60)

        # STEP 0: 문서 파싱
        self._log(f"\n[감독관] STEP 0: {file_type} 파싱 중...")
        t0 = time.time()
        if is_hwp_file(input_path):
            pdf_content: PDFContent = extract_hwp(input_path)
        else:
            pdf_content: PDFContent = extract_pdf(input_path, render_graph_pages=include_images)
        elapsed = time.time() - t0
        self._add_timing("문서 파싱", elapsed)
        self._log(f"[감독관] {file_type} 파싱 완료: {pdf_content.total_pages}페이지 ({elapsed:.1f}초)")
        text_layer_warning = _document_text_chars(pdf_content) < int(getattr(config, "MIN_DOCUMENT_TEXT_CHARS", 500))
        if text_layer_warning:
            self._log("[감독관] 경고: 텍스트 레이어가 거의 없습니다 — 스캔본 PDF일 수 있으며 추출 결과가 비어 있을 수 있습니다")
        prior_plan_pages = detect_prior_plan_pages(pdf_content.pages)
        if prior_plan_pages:
            self._log(
                "[감독관] 기존계획 평가 장 감지: "
                f"p{min(prior_plan_pages)}~p{max(prior_plan_pages)}"
            )

        # STEP 1: 가이드라인 (carbon_guideline.md 우선, HWP fallback)
        self._log("\n[감독관] STEP 1: 가이드라인 에이전트 실행...")
        t0 = time.time()
        guideline_agent = GuidelineAgent(hwp_path=hwp_path)
        extraction_prompts = guideline_agent.get_all_prompts()
        guideline_report = guideline_agent.report()
        elapsed = time.time() - t0
        self._add_timing("가이드라인 로드", elapsed)
        self._log(guideline_report)
        self._log(f"[감독관] STEP 1 완료: {elapsed:.1f}초")

        t0 = time.time()
        artifacts = PipelineArtifacts.create(
            pdf_content,
            extraction_prompts,
            guideline_report=guideline_report,
        )
        pdf_content = artifacts.document
        extraction_prompts = artifacts.extraction_prompts
        elapsed = time.time() - t0
        self._artifact_stats = dict(artifacts.stats)
        self._add_timing("실행 산출물 구성", elapsed)
        self._log(
            "[감독관] 실행 산출물 구성 완료: "
            f"DocumentObject {len(artifacts.document_objects):,}개, "
            f"프롬프트 {len(extraction_prompts)}개 ({elapsed:.1f}초)"
        )

        run_state: RunState | None = None
        if getattr(config, "RUN_STATE_ENABLED", True):
            run_state = RunState.create(
                input_path=input_path,
                guideline_path=hwp_path,
                extraction_prompts=extraction_prompts,
                execution_info=execution_info,
                output_path=output_path,
                resume=bool(getattr(config, "EXTRACTION_RESUME", False)),
                retry_failed_only=bool(getattr(config, "EXTRACTION_RETRY_FAILED_ONLY", False)),
            )
            self._active_run_state = run_state
            execution_info["run_id"] = run_state.run_id
            execution_info["run_state_dir"] = str(run_state.root)
            mode = (
                "실패 배치만 재실행"
                if run_state.retry_failed_only
                else "체크포인트 재개"
                if run_state.resume
                else "새 실행"
            )
            self._log(f"[감독관] 실행 상태: {mode} ({run_state.run_id})")

        # STEP 2~3: 추출·정제 (품질 미달 시 재시도)
        final_data = None
        source_verification: SourceVerificationReport | None = None
        source_object_inventory: SourceObjectInventoryReport | None = None
        semantic_routing: SemanticRoutingReport | None = None
        quality_assessment: QualityAssessment | None = None
        for attempt in range(1, max_pipeline_retries + 1):
            self._log(f"\n[감독관] STEP 2~3: 추출·정제 시도 {attempt}/{max_pipeline_retries}")

            closed_loop_candidates: list[dict] = []
            closed_loop_merge_log: list[dict] = []
            t0 = time.time()
            if getattr(config, "SHEET_CLOSED_LOOP_ENABLED", False):
                try:
                    raw_data, extractor, closed_loop_candidates, closed_loop_merge_log = self._run_sheet_closed_loop(
                        pages=pdf_content.pages,
                        full_text=pdf_content.full_text,
                        extraction_prompts=extraction_prompts,
                        run_state=run_state,
                        document_objects=artifacts.document_objects,
                    )
                except llm_client.LLMQuotaExceededError as exc:
                    extractor = ExtractorAgent(
                        run_state=run_state,
                        document_objects=artifacts.document_objects,
                    )
                    raw_data = extractor.partial_results()
                    logger.warning("시트별 폐루프 추출 quota/한도 문제로 부분 결과로 계속 진행: %s", exc)
            else:
                extractor = ExtractorAgent(
                    run_state=run_state,
                    document_objects=artifacts.document_objects,
                )
                try:
                    raw_data = extractor.extract(
                        pages=pdf_content.pages,
                        full_text=pdf_content.full_text,
                        extraction_prompts=extraction_prompts,
                    )
                except llm_client.LLMQuotaExceededError as exc:
                    raw_data = extractor.partial_results()
                    logger.warning("텍스트 추출 quota/한도 문제로 부분 결과로 계속 진행: %s", exc)
            elapsed = time.time() - t0
            self._add_timing("텍스트 추출", elapsed)
            self._log(extractor.report())
            self._log(extractor.ledger_summary())
            if run_state is not None:
                state_summary = run_state.summary()
                self._log(
                    "[감독관] 영속 배치 상태: "
                    f"예정 {state_summary['expected']}, 성공 {state_summary['ok']}, "
                    f"실패/부분 {state_summary['failed']}, 미실행 {state_summary['missing']}, "
                    f"충돌 {state_summary['conflicts']}"
                )
            self._log(f"[감독관] 텍스트 추출 완료: {elapsed:.1f}초")

            ledger_records = list(extractor.ledger)
            municipality = raw_data.get("municipality_name", "알 수 없음")
            raw_data["execution_info"] = execution_info

            if include_images:
                image_agent = ImageAgent(run_state=run_state)
                t0 = time.time()
                try:
                    raw_data = image_agent.extract(
                        pages=pdf_content.pages,
                        text_results=raw_data,
                        municipality=municipality,
                        document=pdf_content,
                        document_objects=artifacts.clone_document_objects(),
                    )
                except llm_client.LLMQuotaExceededError as exc:
                    logger.warning("이미지 분석 quota/한도 문제로 기존 텍스트 결과로 계속 진행: %s", exc)
                self._add_vision_render_stats(image_agent.metrics())
                elapsed = time.time() - t0
                self._add_timing("이미지 분석", elapsed)
                self._log(image_agent.report())
                self._log(f"[감독관] 이미지 분석 완료: {elapsed:.1f}초")
            else:
                self._log("[에이전트2b 이미지분석] 비활성화 (--no-images)")

            raw_data["execution_info"] = execution_info
            if getattr(config, "VISUAL_MERGE_SNAPSHOT_ENABLED", True):
                snapshot_path = output_path.with_name(
                    f"{output_path.stem}_visual_merge_input.json.gz"
                )
                try:
                    write_visual_merge_snapshot(snapshot_path, raw_data)
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning("시각 병합 A/B 스냅샷 저장 실패: %s", exc)
                else:
                    self._log(f"[감독관] 시각 병합 A/B 스냅샷: {snapshot_path}")
            organizer = OrganizerAgent()
            t0 = time.time()
            final_data = organizer.organize(
                raw_data,
                ledger=ledger_records,
                routed_page_nums=getattr(extractor, "routed_page_nums", None),
                prior_plan_pages=prior_plan_pages,
            )
            elapsed = time.time() - t0
            self._add_timing("정리·정제", elapsed)
            self._log(organizer.report())
            self._log(f"[감독관] 정리·정제 완료: {elapsed:.1f}초")

            # STEP 3b: 빈칸 보완 (비어 있거나 채움률 낮은 시트만 재추출)
            if getattr(config, "GAP_FILL_ENABLED", True):
                self._log("\n[감독관] STEP 3b: 빈칸 보완 에이전트 실행...")
                gap_agent = GapFillAgent()
                t0 = time.time()
                try:
                    enhanced_raw = gap_agent.enhance(
                        raw_data=raw_data,
                        cleaned=final_data,
                        pages=pdf_content.pages,
                        extracted_page_nums=getattr(extractor, "extracted_page_nums", None),
                    )
                except llm_client.LLMQuotaExceededError as exc:
                    enhanced_raw = raw_data
                    logger.warning("빈칸 보완 quota/한도 문제로 건너뜀: %s", exc)
                elapsed = time.time() - t0
                self._add_timing("빈칸 보완", elapsed)
                self._log(gap_agent.report())
                self._log(f"[감독관] 빈칸 보완 완료: {elapsed:.1f}초")

                ledger_records = list(extractor.ledger) + list(getattr(gap_agent, "ledger", []))

                # 보완 후보가 추가됐거나 보완 실패 원장이 생기면 같은 organizer로 재정제해 검증리포트를 갱신한다.
                if enhanced_raw is not raw_data or getattr(gap_agent, "ledger", []):
                    raw_data = enhanced_raw
                    raw_data["execution_info"] = execution_info
                    t0 = time.time()
                    final_data = organizer.organize(
                        raw_data,
                        ledger=ledger_records,
                        routed_page_nums=getattr(extractor, "routed_page_nums", None),
                        prior_plan_pages=prior_plan_pages,
                    )
                    elapsed = time.time() - t0
                    self._add_timing("정리·정제", elapsed)
                    self._log(f"[감독관] 빈칸 보완 후 재정제 완료: {elapsed:.1f}초")

            if text_layer_warning:
                self._append_text_layer_warning(final_data)

            if closed_loop_candidates:
                final_data.setdefault("hybrid_review_candidates", []).extend(closed_loop_candidates)
            if closed_loop_merge_log:
                reconcile_reflected_merge_log(final_data, closed_loop_merge_log)
                final_data.setdefault("hybrid_merge_log", []).extend(closed_loop_merge_log)

            if getattr(config, "HYBRID_REVIEW_ENABLED", False):
                hybrid_agent = HybridReviewAgent()
                if getattr(config, "HYBRID_REVIEW_TARGETED", True):
                    self._log("\n[감독관] STEP 3c: 검증리포트 기반 타깃 보조검수 실행...")
                    t0 = time.time()
                    try:
                        review_candidates = hybrid_agent.review_targeted(
                            pages=pdf_content.pages,
                            final_data=final_data,
                            extraction_prompts=extraction_prompts,
                        )
                    except llm_client.LLMQuotaExceededError as exc:
                        review_candidates = []
                        logger.warning("타깃 보조검수 quota/한도 문제로 건너뜀: %s", exc)
                    elapsed = time.time() - t0
                    self._add_timing("타깃 보조검수", elapsed)
                    if review_candidates:
                        final_data.setdefault("hybrid_review_candidates", []).extend(review_candidates)
                    self._log(hybrid_agent.report())
                    self._log(f"[감독관] 타깃 보조검수 완료: {elapsed:.1f}초")
                elif getattr(config, "SHEET_CLOSED_LOOP_ENABLED", False):
                    self._log("\n[감독관] STEP 3c/3d: 시트별 폐루프에서 보조검수·병합을 이미 수행해 재실행하지 않음")
                elif getattr(config, "HYBRID_SHEETWISE_FLOW_ENABLED", True):
                    self._log("\n[감독관] STEP 3c/3d: 시트 단위 보조검수·병합 실행...")
                    t0 = time.time()
                    try:
                        final_data, review_candidates, merge_log = hybrid_agent.review_and_adjudicate_by_sheet(
                            pages=pdf_content.pages,
                            final_data=final_data,
                            extraction_prompts=extraction_prompts,
                            progress=self._log,
                        )
                    except llm_client.LLMQuotaExceededError as exc:
                        review_candidates = []
                        merge_log = []
                        logger.warning("시트 단위 보조검수·병합 quota/한도 문제로 건너뜀: %s", exc)
                    elapsed = time.time() - t0
                    self._add_timing("시트 단위 보조검수·병합", elapsed)
                    if review_candidates:
                        final_data.setdefault("hybrid_review_candidates", []).extend(review_candidates)
                    if merge_log:
                        reconcile_reflected_merge_log(final_data, merge_log)
                        final_data.setdefault("hybrid_merge_log", []).extend(merge_log)
                    self._log(hybrid_agent.report())
                    self._log(hybrid_agent.adjudication_report())
                    self._log(f"[감독관] 시트 단위 보조검수·병합 완료: {elapsed:.1f}초")
                else:
                    self._log("\n[감독관] STEP 3c: 보조 모델 타깃 검수 실행...")
                    t0 = time.time()
                    try:
                        review_candidates = hybrid_agent.review(
                            pages=pdf_content.pages,
                            final_data=final_data,
                            extraction_prompts=extraction_prompts,
                        )
                    except llm_client.LLMQuotaExceededError as exc:
                        review_candidates = []
                        logger.warning("보조 모델 검수 quota/한도 문제로 건너뜀: %s", exc)
                    elapsed = time.time() - t0
                    self._add_timing("보조 모델 검수", elapsed)
                    if review_candidates:
                        final_data.setdefault("hybrid_review_candidates", []).extend(review_candidates)
                    self._log(hybrid_agent.report())
                    self._log(f"[감독관] 보조 모델 검수 완료: {elapsed:.1f}초")

                    if review_candidates and getattr(config, "HYBRID_ADJUDICATION_ENABLED", True):
                        self._log("\n[감독관] STEP 3d: 보조 후보 판정·병합 실행...")
                        t0 = time.time()
                        try:
                            final_data, merge_log = hybrid_agent.adjudicate_and_merge(
                                candidates=review_candidates,
                                final_data=final_data,
                                pages=pdf_content.pages,
                            )
                        except llm_client.LLMQuotaExceededError as exc:
                            merge_log = []
                            logger.warning("보조 후보 판정 quota/한도 문제로 건너뜀: %s", exc)
                        elapsed = time.time() - t0
                        self._add_timing("보조 후보 판정·병합", elapsed)
                    if merge_log:
                        reconcile_reflected_merge_log(final_data, merge_log)
                        final_data.setdefault("hybrid_merge_log", []).extend(merge_log)
                    self._log(hybrid_agent.adjudication_report())
                    self._log(f"[감독관] 보조 후보 판정·병합 완료: {elapsed:.1f}초")

            # STEP 3d-2: 원문 표 캡션·섹션을 이용해 행이 올바른 시트에
            # 배치됐는지 검증한다. 자동 이동은 미래연도 현황→전망 오배치로 제한한다.
            if getattr(config, "SEMANTIC_ROUTING_ENABLED", True):
                self._log("\n[감독관] STEP 3d-2: 표 캡션·섹션 기반 시트 의미 검증...")
                t0 = time.time()
                semantic_routing = validate_and_reclassify(
                    final_data,
                    pdf_content,
                    auto_reclassify=bool(
                        getattr(config, "SEMANTIC_ROUTING_AUTO_RECLASSIFY", True)
                    ),
                    document_objects=artifacts.document_objects,
                )
                municipality = final_data.get("municipality_name", "알 수 없음")
                validation_report = final_data.setdefault("validation_report", [])
                if isinstance(validation_report, list):
                    validation_report.extend(
                        semantic_routing.validation_issues(municipality)
                    )
                elapsed = time.time() - t0
                self._add_timing("시트 의미 검증", elapsed)
                self._log(
                    f"[감독관] 시트 의미 검증 완료: "
                    f"{semantic_routing.summary_text()} ({elapsed:.1f}초)"
                )
            else:
                semantic_routing = None
                self._log("[감독관] 시트 의미 검증 비활성화")

            # STEP 3e: 최종 행을 원문과 결정론적으로 대조한다. 기존 외전 검수 도구의
            # 검색어·출처페이지 우선·전체문서 fallback 원리를 인메모리 데이터에 적용한다.
            if getattr(config, "SOURCE_VERIFICATION_ENABLED", True):
                self._log("\n[감독관] STEP 3e: 원문 근거 대조 실행...")
                t0 = time.time()
                source_verification = verify_final_data(
                    final_data,
                    pdf_content,
                    page_radius=max(0, int(getattr(config, "SOURCE_VERIFICATION_PAGE_RADIUS", 1))),
                    max_terms=max(1, int(getattr(config, "SOURCE_VERIFICATION_MAX_TERMS", 12))),
                    global_search=bool(getattr(config, "SOURCE_VERIFICATION_GLOBAL_SEARCH", True)),
                )
                municipality = final_data.get("municipality_name", "알 수 없음")
                final_data["source_verification"] = source_verification.excel_rows(municipality)
                validation_report = final_data.setdefault("validation_report", [])
                if isinstance(validation_report, list):
                    validation_report.extend(source_verification.validation_issues(municipality))
                elapsed = time.time() - t0
                self._add_timing("원문 대조", elapsed)
                self._log(f"[감독관] 원문 대조 완료: {source_verification.summary_text()} ({elapsed:.1f}초)")
            else:
                source_verification = None
                final_data["source_verification"] = []
                self._log("[감독관] 원문 대조 비활성화")

            # STEP 3f: 추출된 행을 출발점으로 삼는 원문대조와 반대 방향으로,
            # 원문의 표·그래프 전체를 분모로 삼아 누락 객체를 계산한다.
            if getattr(config, "SOURCE_OBJECT_INVENTORY_ENABLED", True):
                self._log("\n[감독관] STEP 3f: 원문 표·그래프 객체 인벤토리 연결...")
                t0 = time.time()
                source_object_inventory = build_source_object_inventory(
                    final_data,
                    pdf_content,
                    document_objects=artifacts.document_objects,
                )
                municipality = final_data.get("municipality_name", "알 수 없음")
                final_data["source_object_inventory"] = source_object_inventory.excel_rows(municipality)
                validation_report = final_data.setdefault("validation_report", [])
                if isinstance(validation_report, list):
                    validation_report.extend(source_object_inventory.validation_issues(municipality))
                elapsed = time.time() - t0
                self._add_timing("원문 객체 인벤토리", elapsed)
                self._log(
                    f"[감독관] 원문 객체 연결 완료: "
                    f"{source_object_inventory.summary_text()} ({elapsed:.1f}초)"
                )
            else:
                source_object_inventory = None
                final_data["source_object_inventory"] = []
                self._log("[감독관] 원문 객체 인벤토리 비활성화")

            validation_rows = final_data.get("validation_report", [])
            if isinstance(validation_rows, list):
                final_data["validation_report"] = _deduplicate_validation_rows(validation_rows)

            pipeline_metrics = _ledger_quality_metrics(ledger_records, run_state)
            pipeline_metrics.update({
                "text_tasks_enqueued": int(getattr(extractor, "enqueued_tasks", 0)),
                "text_tasks_deduplicated": int(getattr(extractor, "deduplicated_tasks", 0)),
            })
            if semantic_routing is not None:
                pipeline_metrics.update(semantic_routing.metrics())
            if source_object_inventory is not None:
                pipeline_metrics.update({
                    "automatic_source_object_coverage": round(
                        source_object_inventory.coverage_ratio, 4
                    ),
                    "object_routing_error_rate": round(
                        source_object_inventory.routing_error_rate, 4
                    ),
                })
            final_data["pipeline_metrics"] = pipeline_metrics
            quality_assessment = assess_quality(
                final_data,
                source_verification,
                source_object_inventory,
            )
            score, issues = quality_assessment.score, quality_assessment.issues
            self._log(f"\n[감독관] 품질 점수: {score:.1f}/100")
            components = quality_assessment.metrics.get("components", {})
            if components:
                self._log(
                    "[감독관] 점수 구성: "
                    + ", ".join(f"{name} {value:.1f}" for name, value in components.items())
                )
            if issues:
                self._log(f"[감독관] 이슈: {', '.join(issues)}")
            self._log(
                "[감독관] 독립 지표: "
                "셀 정확도·객체 재현율=고정 평가(--evaluate) 시 산출, "
                f"자동 객체 커버리지="
                f"{quality_assessment.metrics.get('automatic_source_object_coverage', 0):.1%}, "
                f"라우팅 오류율={quality_assessment.metrics.get('routing_error_rate', 0):.1%}"
            )

            quality_threshold = float(getattr(config, "QUALITY_THRESHOLD", self.QUALITY_THRESHOLD))
            if score >= quality_threshold:
                self._log("[감독관] 품질 기준 통과")
                break
            elif attempt < max_pipeline_retries:
                self._log("[감독관] 품질 미달. 재시도합니다...")
            else:
                self._log("[감독관] 최대 재시도 도달. 현재 결과로 진행합니다.")

        # STEP 4: 엑셀 작성
        self._log("\n[감독관] STEP 4: 엑셀 작성 에이전트 실행...")
        excel_agent = ExcelAgent()
        t0 = time.time()
        excel_data = organizer.get_excel_ready()
        for supplemental_key in (
            "hybrid_review_candidates", "hybrid_merge_log", "validation_report",
            "source_verification", "source_object_inventory",
        ):
            if final_data and isinstance(final_data.get(supplemental_key), list):
                excel_data[supplemental_key] = final_data[supplemental_key]
        result_path = excel_agent.write(excel_data, output_path)
        elapsed = time.time() - t0
        self._add_timing("엑셀 작성", elapsed)
        self._log(excel_agent.report())
        self._log(f"[감독관] 엑셀 작성 완료: {elapsed:.1f}초")

        # 외전 검수 도구와 같은 좌표 마킹 산출물. 점수 계산과 분리해 실패해도 Excel은 보존한다.
        if (
            source_verification is not None
            and getattr(config, "SOURCE_VERIFICATION_MARK_PDF", True)
            and input_path.suffix.casefold() == ".pdf"
        ):
            t0 = time.time()
            marked_path = result_path.with_name(f"마킹_{result_path.stem}.pdf")
            try:
                saved_marked_path, mark_count = create_marked_pdf(
                    input_path,
                    source_verification,
                    marked_path,
                    max_marks=max(0, int(getattr(config, "SOURCE_VERIFICATION_MAX_MARKS", 10000))),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("원문 마킹 PDF 생성 실패. Excel 결과는 유지합니다: %s", exc)
                saved_marked_path, mark_count = None, 0
            elapsed = time.time() - t0
            self._add_timing("마킹 PDF", elapsed)
            if saved_marked_path is not None:
                self._log(f"[감독관] 원문 마킹 PDF: {saved_marked_path} ({mark_count}개, {elapsed:.1f}초)")
            else:
                self._log(f"[감독관] 원문 마킹 PDF 생략: 표시 가능한 좌표 없음 ({elapsed:.1f}초)")

        # LLM 최종 검수
        self._log("\n[감독관] LLM 최종 품질 검수 중...")
        t0 = time.time()
        try:
            if quality_assessment is None:
                quality_assessment = assess_quality(
                    final_data,
                    source_verification,
                    source_object_inventory,
                )
            self._quality_assessment_for_review = quality_assessment
            review = self._llm_quality_review(final_data)
        except (llm_client.LLMQuotaExceededError, llm_client.LLMCapacityError) as exc:
            elapsed = time.time() - t0
            self._add_timing("LLM 최종 검수", elapsed)
            logger.warning("저장 후 LLM 최종 검수 capacity/quota 문제로 생략: %s", exc)
            self._log(f"모델 과부하 또는 한도 초과로 최종 검수는 생략했습니다. 결과 파일은 저장되어 있습니다: {result_path}")
        else:
            elapsed = time.time() - t0
            self._add_timing("LLM 최종 검수", elapsed)
            self._log(f"[감독관] 검수 결과: {review.get('quality_level', '?')}")
            self._log(f"  평가: {review.get('assessment', '')}")
            if review.get("key_issues"):
                self._log(f"  주요 이슈: {', '.join(review.get('key_issues', []))}")
            self._log(f"[감독관] LLM 최종 검수 완료: {elapsed:.1f}초")

        self._log("\n" + "=" * 60)
        self._log("[감독관] 파이프라인 완료")
        self._log(f"  출력 파일: {result_path}")
        if quality_assessment is None:
            quality_assessment = assess_quality(
                final_data,
                source_verification,
                source_object_inventory,
            )
        self._log(f"  최종 품질 점수: {quality_assessment.score:.1f}/100")
        if run_state is not None:
            manifest_path = run_state.finalize(
                output_path=result_path,
                semantic_result_hash=sha256_json({
                    key: value
                    for key, value in final_data.items()
                    if (
                        (isinstance(value, list) and key not in getattr(config, "INTERNAL_OBJECT_KEYS", set()))
                        or key in {"municipality_name", "pipeline_metrics"}
                    )
                }),
                extra={
                    "quality_score": quality_assessment.score,
                    "quality_metrics": quality_assessment.metrics,
                    "timings_seconds": {key: round(value, 3) for key, value in self._timings.items()},
                    "llm_cache_stats": llm_cache.get_cache_stats(),
                    "llm_call_stats": llm_client.get_llm_stats(),
                    "parallel_queue_stats": parallel.get_parallel_stats(),
                    "pipeline_artifact_stats": dict(self._artifact_stats),
                    "vision_render_stats": {
                        key: round(float(value), 3) if key.endswith("_seconds") else int(value)
                        for key, value in self._vision_render_stats.items()
                    },
                },
            )
            state_summary = run_state.summary()
            self._log(
                f"  실행 상태: {run_state.completion_status()} "
                f"(성공 {state_summary['ok']}/{state_summary['expected']}, "
                f"미복구 {state_summary['failed'] + state_summary['missing']})"
            )
            self._log(f"  실행 매니페스트: {manifest_path}")
        self._log_timing_summary(time.time() - pipeline_start)
        self._log("=" * 60)

        return result_path

    def get_report(self) -> str:
        return "\n".join(self._reports)
