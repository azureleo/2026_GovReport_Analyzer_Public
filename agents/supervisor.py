"""
에이전트 5: 감독·검수 에이전트

전체 파이프라인을 조율하고 결과의 품질을 검수합니다.
"""

import json
import logging
import time
from pathlib import Path

import config
from utils.pdf_reader import extract_pdf, PDFContent
from utils import llm_client
from agents.guideline_agent import GuidelineAgent
from agents.extractor_agent import ExtractorAgent
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent
from agents.excel_agent import ExcelAgent

logger = logging.getLogger(__name__)

QUALITY_SYSTEM = """당신은 지자체 탄소중립 기본계획 데이터 품질 검수 전문가입니다.
반드시 JSON만 반환하세요."""


def _quality_score(final_data: dict) -> tuple[float, list[str]]:
    issues = []
    score = 100.0

    if not final_data.get("municipality_name") or final_data.get("municipality_name") == "알 수 없음":
        issues.append("지자체명 미확인")
        score -= 20

    ghg = final_data.get("ghg", [])
    if len(ghg) == 0:
        issues.append("온실가스 데이터 없음 (중요)")
        score -= 30
    else:
        if not any(g.get("종류") == "현황" for g in ghg):
            issues.append("온실가스 현황 데이터 없음")
            score -= 15
        if not any(g.get("종류") == "목표" for g in ghg):
            issues.append("온실가스 감축 목표 데이터 없음")
            score -= 10
        total_cells = len(ghg) * len(config.YEARS)
        filled = sum(
            1 for g in ghg for y in config.YEARS
            if g.get("연도별", {}).get(str(y)) is not None
        )
        fill_rate = filled / total_cells if total_cells > 0 else 0
        if fill_rate < 0.2:
            issues.append(f"GHG 연도별 채움률 낮음 ({fill_rate:.0%})")
            score -= 10

    if len(final_data.get("strategy", [])) == 0:
        issues.append("감축 전략 데이터 없음")
        score -= 10

    empty_summary = [s for s in final_data.get("summary", []) if not s.get("내용")]
    if empty_summary:
        issues.append(f"요약카드 빈 항목: {', '.join(s['항목'] for s in empty_summary)}")
        score -= len(empty_summary) * 3

    return max(0.0, score), issues


class Supervisor:
    """에이전트 5: 감독·검수 에이전트"""

    QUALITY_THRESHOLD = 50.0

    def __init__(self):
        self._reports: list[str] = []

    def _log(self, msg: str):
        print(msg)
        self._reports.append(msg)

    def _llm_quality_review(self, final_data: dict) -> dict:
        summary_text = json.dumps(final_data.get("summary", []), ensure_ascii=False)
        prompt = f"""다음은 '{final_data.get('municipality_name', '?')}' 탄소중립 계획 추출 결과 요약입니다:
- GHG 데이터 행 수: {len(final_data.get('ghg', []))}
- 감축전략 데이터 행 수: {len(final_data.get('strategy', []))}
- 요약카드: {summary_text}

이 추출 결과의 완성도를 평가하고 JSON으로 반환하세요:
{{
  "assessment": "평가 의견 (2~3문장)",
  "quality_level": "우수|보통|미흡",
  "key_issues": ["이슈1", "이슈2"]
}}"""
        resp = llm_client.call_text(prompt, system=QUALITY_SYSTEM)
        return llm_client.parse_json(resp)

    def run(
        self,
        pdf_path: str | Path,
        output_path: str | Path,
        hwp_path: str | None = None,
        max_pipeline_retries: int = 2,
    ) -> Path:
        pdf_path = Path(pdf_path)
        output_path = Path(output_path)

        self._log("=" * 60)
        self._log("[감독관] 파이프라인 시작")
        self._log(f"  - 입력 PDF : {pdf_path}")
        self._log(f"  - 출력 경로: {output_path}")
        self._log("=" * 60)

        # STEP 0: PDF 파싱
        self._log("\n[감독관] STEP 0: PDF 파싱 중...")
        t0 = time.time()
        pdf_content: PDFContent = extract_pdf(pdf_path, render_graph_pages=True)
        self._log(f"[감독관] PDF 파싱 완료: {pdf_content.total_pages}페이지 ({time.time()-t0:.1f}초)")

        # STEP 1: 가이드라인
        self._log("\n[감독관] STEP 1: 가이드라인 에이전트 실행...")
        guideline_agent = GuidelineAgent(hwp_path=hwp_path)
        extraction_prompts = guideline_agent.get_all_prompts()
        self._log(guideline_agent.report())

        # STEP 2~3: 추출·정제 (품질 미달 시 재시도)
        final_data = None
        for attempt in range(1, max_pipeline_retries + 1):
            self._log(f"\n[감독관] STEP 2~3: 추출·정제 시도 {attempt}/{max_pipeline_retries}")

            extractor = ExtractorAgent()
            raw_data = extractor.extract(
                pages=pdf_content.pages,
                full_text=pdf_content.full_text,
                extraction_prompts=extraction_prompts,
            )
            self._log(extractor.report())

            image_agent = ImageAgent()
            municipality = raw_data.get("municipality_name", "알 수 없음")
            raw_data = image_agent.extract(
                pages=pdf_content.pages,
                text_results=raw_data,
                municipality=municipality,
            )
            self._log(image_agent.report())

            organizer = OrganizerAgent()
            final_data = organizer.organize(raw_data, pdf_content.full_text[:6000])
            self._log(organizer.report())

            score, issues = _quality_score(final_data)
            self._log(f"\n[감독관] 품질 점수: {score:.1f}/100")
            if issues:
                self._log(f"[감독관] 이슈: {', '.join(issues)}")

            if score >= self.QUALITY_THRESHOLD:
                self._log(f"[감독관] 품질 기준 통과")
                break
            elif attempt < max_pipeline_retries:
                self._log(f"[감독관] 품질 미달. 재시도합니다...")
            else:
                self._log(f"[감독관] 최대 재시도 도달. 현재 결과로 진행합니다.")

        # STEP 4: 엑셀 작성
        self._log("\n[감독관] STEP 4: 엑셀 작성 에이전트 실행...")
        excel_agent = ExcelAgent()
        organizer_final = OrganizerAgent()
        organizer_final._final_data = final_data
        result_path = excel_agent.write(organizer_final.get_excel_ready(), output_path)
        self._log(excel_agent.report())

        # LLM 최종 검수
        self._log("\n[감독관] LLM 최종 품질 검수 중...")
        review = self._llm_quality_review(final_data)
        self._log(f"[감독관] 검수 결과: {review.get('quality_level', '?')}")
        self._log(f"  평가: {review.get('assessment', '')}")
        if review.get("key_issues"):
            self._log(f"  주요 이슈: {', '.join(review.get('key_issues', []))}")

        self._log("\n" + "=" * 60)
        self._log("[감독관] 파이프라인 완료")
        self._log(f"  출력 파일: {result_path}")
        self._log(f"  최종 품질 점수: {_quality_score(final_data)[0]:.1f}/100")
        self._log("=" * 60)

        return result_path

    def get_report(self) -> str:
        return "\n".join(self._reports)
