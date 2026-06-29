"""
에이전트 5: 감독·검수 에이전트

전체 파이프라인을 조율하고 결과의 품질을 검수합니다.
carbon_guideline.md 기반 16개 시트 구조에 맞춰 동작합니다.
"""

import json
import logging
import time
from pathlib import Path

import config
from utils.pdf_reader import extract_pdf, PDFContent
from utils.hwp_reader import extract_hwp, is_hwp_file
from utils import llm_cache, llm_client
from agents.guideline_agent import GuidelineAgent
from agents.extractor_agent import ExtractorAgent
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent
from agents.gap_fill_agent import GapFillAgent
from agents.hybrid_review_agent import HybridReviewAgent
from agents.excel_agent import ExcelAgent

logger = logging.getLogger(__name__)

QUALITY_SYSTEM = """당신은 지자체 탄소중립 기본계획 데이터 품질 검수 전문가입니다.
반드시 JSON만 반환하세요."""


_TIMING_ORDER = [
    "문서 파싱",
    "가이드라인 로드",
    "텍스트 추출",
    "이미지 분석",
    "정리·정제",
    "빈칸 보완",
    "보조 모델 검수",
    "보조 후보 판정·병합",
    "엑셀 작성",
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


def _quality_score(final_data: dict) -> tuple[float, list[str]]:
    issues = []
    score = 100.0

    municipality = final_data.get("municipality_name", "알 수 없음")
    if not municipality or municipality == "알 수 없음":
        issues.append("지자체명 미확인")
        score -= 20

    # 핵심 시트 데이터 존재 확인
    emissions = final_data.get("emissions_regional", []) + final_data.get("emissions_management", [])
    if not emissions:
        issues.append("온실가스 배출 데이터 없음 (중요)")
        score -= 25

    targets = final_data.get("reduction_targets", [])
    if not targets:
        issues.append("감축목표 데이터 없음")
        score -= 15

    projects = final_data.get("mitigation_projects", [])
    if not projects:
        issues.append("감축사업 목록 없음")
        score -= 10

    forecast = final_data.get("emissions_forecast", [])
    if not forecast:
        issues.append("배출전망 데이터 없음")
        score -= 5

    regional = final_data.get("regional_conditions", [])
    if not regional:
        issues.append("지역여건 데이터 없음")
        score -= 5

    vision = final_data.get("vision_strategy", [])
    if not vision:
        issues.append("비전·전략 없음")
        score -= 5

    # 채움률 확인
    total_rows = sum(len(v) for v in final_data.values() if isinstance(v, list))
    if total_rows < 10:
        issues.append(f"전체 추출 행 수 매우 적음 ({total_rows}행)")
        score -= 15

    return max(0.0, score), issues


class Supervisor:
    """에이전트 5: 감독·검수 에이전트"""

    QUALITY_THRESHOLD = 40.0

    def __init__(self):
        self._reports: list[str] = []
        self._timings: dict[str, float] = {}

    def _log(self, msg: str):
        print(msg)
        self._reports.append(msg)

    def _add_timing(self, label: str, elapsed: float):
        self._timings[label] = self._timings.get(label, 0.0) + elapsed

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

        stats = llm_cache.get_cache_stats()
        if stats:
            self._log(
                "  - LLM 캐시: "
                f"hit {stats.get('hit', 0)}, "
                f"miss {stats.get('miss', 0)}, "
                f"write {stats.get('write', 0)}, "
                f"disabled {stats.get('disabled', 0)}"
            )

    def _llm_quality_review(self, final_data: dict) -> dict:
        municipality = final_data.get("municipality_name", "?")
        sheet_counts = {k: len(v) for k, v in final_data.items() if isinstance(v, list) and v}
        prompt = f"""다음은 '{municipality}' 탄소중립 계획 추출 결과 요약입니다:
- 시트별 행 수: {json.dumps(sheet_counts, ensure_ascii=False)}
- 총 추출 행: {sum(sheet_counts.values())}

이 추출 결과의 완성도를 평가하고 JSON으로 반환하세요:
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
            score, issues = _quality_score(final_data)
            return {
                "assessment": f"LLM 검수 호출이 실패하여 규칙 기반 품질 점수({score:.1f}/100)만 기록했습니다.",
                "quality_level": "보통" if score >= self.QUALITY_THRESHOLD else "미흡",
                "key_issues": issues,
            }
        return llm_client.parse_json(resp)

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
        self._timings = {}
        llm_cache.reset_cache_stats()

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

        # STEP 1: 가이드라인 (carbon_guideline.md 우선, HWP fallback)
        self._log("\n[감독관] STEP 1: 가이드라인 에이전트 실행...")
        t0 = time.time()
        guideline_agent = GuidelineAgent(hwp_path=hwp_path)
        extraction_prompts = guideline_agent.get_all_prompts()
        elapsed = time.time() - t0
        self._add_timing("가이드라인 로드", elapsed)
        self._log(guideline_agent.report())
        self._log(f"[감독관] STEP 1 완료: {elapsed:.1f}초")

        # STEP 2~3: 추출·정제 (품질 미달 시 재시도)
        final_data = None
        for attempt in range(1, max_pipeline_retries + 1):
            self._log(f"\n[감독관] STEP 2~3: 추출·정제 시도 {attempt}/{max_pipeline_retries}")

            extractor = ExtractorAgent()
            t0 = time.time()
            raw_data = extractor.extract(
                pages=pdf_content.pages,
                full_text=pdf_content.full_text,
                extraction_prompts=extraction_prompts,
            )
            elapsed = time.time() - t0
            self._add_timing("텍스트 추출", elapsed)
            self._log(extractor.report())
            self._log(f"[감독관] 텍스트 추출 완료: {elapsed:.1f}초")

            municipality = raw_data.get("municipality_name", "알 수 없음")

            if include_images:
                image_agent = ImageAgent()
                t0 = time.time()
                raw_data = image_agent.extract(
                    pages=pdf_content.pages,
                    text_results=raw_data,
                    municipality=municipality,
                )
                elapsed = time.time() - t0
                self._add_timing("이미지 분석", elapsed)
                self._log(image_agent.report())
                self._log(f"[감독관] 이미지 분석 완료: {elapsed:.1f}초")
            else:
                self._log("[에이전트2b 이미지분석] 비활성화 (--no-images)")

            organizer = OrganizerAgent()
            t0 = time.time()
            final_data = organizer.organize(raw_data)
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
                    )
                except llm_client.LLMQuotaExceededError as exc:
                    enhanced_raw = raw_data
                    logger.warning("빈칸 보완 quota/한도 문제로 건너뜀: %s", exc)
                elapsed = time.time() - t0
                self._add_timing("빈칸 보완", elapsed)
                self._log(gap_agent.report())
                self._log(f"[감독관] 빈칸 보완 완료: {elapsed:.1f}초")

                # 보완 후보가 추가된 경우에만 같은 organizer로 재정제(_final_data 갱신).
                if enhanced_raw is not raw_data:
                    raw_data = enhanced_raw
                    t0 = time.time()
                    final_data = organizer.organize(raw_data)
                    elapsed = time.time() - t0
                    self._add_timing("정리·정제", elapsed)
                    self._log(f"[감독관] 빈칸 보완 후 재정제 완료: {elapsed:.1f}초")

            if getattr(config, "HYBRID_REVIEW_ENABLED", False):
                self._log("\n[감독관] STEP 3c: 보조 모델 타깃 검수 실행...")
                hybrid_agent = HybridReviewAgent()
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
                    final_data["hybrid_review_candidates"] = review_candidates
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
                        final_data["hybrid_merge_log"] = merge_log
                    self._log(hybrid_agent.adjudication_report())
                    self._log(f"[감독관] 보조 후보 판정·병합 완료: {elapsed:.1f}초")

            score, issues = _quality_score(final_data)
            self._log(f"\n[감독관] 품질 점수: {score:.1f}/100")
            if issues:
                self._log(f"[감독관] 이슈: {', '.join(issues)}")

            if score >= self.QUALITY_THRESHOLD:
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
        if final_data and final_data.get("hybrid_review_candidates"):
            excel_data["hybrid_review_candidates"] = final_data["hybrid_review_candidates"]
        if final_data and final_data.get("hybrid_merge_log"):
            excel_data["hybrid_merge_log"] = final_data["hybrid_merge_log"]
        result_path = excel_agent.write(excel_data, output_path)
        elapsed = time.time() - t0
        self._add_timing("엑셀 작성", elapsed)
        self._log(excel_agent.report())
        self._log(f"[감독관] 엑셀 작성 완료: {elapsed:.1f}초")

        # LLM 최종 검수
        self._log("\n[감독관] LLM 최종 품질 검수 중...")
        t0 = time.time()
        review = self._llm_quality_review(final_data)
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
        self._log(f"  최종 품질 점수: {_quality_score(final_data)[0]:.1f}/100")
        self._log_timing_summary(time.time() - pipeline_start)
        self._log("=" * 60)

        return result_path

    def get_report(self) -> str:
        return "\n".join(self._reports)
