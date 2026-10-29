# 저장 후 품질 검사와 골든 평가

2026-10-27. 현재 작업본의 main.py에 적용했다. 독립 재평가 CLI는 이번 범위에서 만들지 않았다.

## 평가 흐름

기존 추출 → 정제 → 기존 최소 구조 재시도 검사 → Excel 저장 → **실제 업무 시트 재조회·셀 일치 확인** → 규칙 검사 → 자격 있는 골든 평가 → 기존 1회 LLM 보조 검수 → 품질 보고서·pipeline_audit·run_outcome 저장 순서다.

기존 40점 재시도 조건은 그대로이며 화면 이름만 `최소 구조 검사 (정확도 아님)`으로 바꿨다. 최종 품질 100점 출력은 제거했다. 새 검사 때문에 전체 재추출하거나 업무 값을 변경하지 않는다. 최종 LLM 검수에는 실제 검사 요약을 제공하되, 정확도 산출·자동 수정에는 사용하지 않는다. 모델명·provider·추론 강도·추출 프롬프트·라우팅·배치 설정과 실제 `.env`는 변경하지 않았다.

## 2026-10-28: 최종 LLM 등급과 로그 변경

`우수/보통/미흡`의 전체 등급을 요청하거나 표시하지 않는다. `utils/final_review.py`가 다음 세 항목을 실제 검사에서 결정한다.

- **데이터 규칙:** 확인된 규칙 위반/의심이 있으면 `검토 필요`, 없으면 `검사 범위 내 규칙 위반 미발견`, 검사 정보가 없으면 `미평가/미실행`. 개별 객체 연결 미확인과 원문 텍스트에서 표현을 찾지 못한 진단은 검증 한계로 구분한다. 잘못된 페이지 번호처럼 확정된 출처 규칙 위반은 계속 데이터 규칙에 포함한다.
- **검증 범위:** 원문 일부 항목 대조 / 원문 대조 범위 부족 / 미실행·미평가 / 실패. 비교 가능 항목 0건을 누락 없음으로 바꾸지 않는다. 객체 연결률과 식별 정보 검사 범위를 사실 정확도로 해석하지 않는다.
- **사실 정확도:** 골든 OFF면 `미평가 (골든 평가 OFF)`로 표시하고, 미등록이면 사유를 표시한다. 선택 골든을 평가한 경우만 그 범위의 기존 점수를 별도로 제시한다. OFF/미등록은 데이터 품질 감점 근거가 아니다.

LLM은 확인된 rule ID와 검증 topic에 대한 해설 및 검토 순서만 제안한다. 상태는 코드가 결정하며 LLM의 임의 등급·정답률·과거 `quality_level`은 사용하지 않는다. 자유 서술을 최종 판정처럼 터미널에 출력하지 않는다. 원응답과 채택한 구조화 참고 의견은 `llm_review.result`에 보존하고, `raw_response`는 독립 평가 근거가 아님을 명시한다. 형식이 잘못됐거나 호출에 실패해도 실제 규칙 진단과 이미 저장한 Excel은 유지한다.

품질 JSON/Markdown·감사 파일에 동일한 세 상태를 저장하고, `_run_outcome.json`의 `final_review`에도 요약을 연결한다. 기존 `review_status=needs_review`는 검토할 문제 또는 검증 한계가 있다는 운영 상태이며, 낮은 정답률이라는 뜻이 아니다. 기존 전체 등급을 읽던 소비자는 `llm_review.result.rule_check`, `verification_scope`, `fact_accuracy`로 전환해야 한다.

예상 로그 형식은 다음과 같다. 숫자는 기존 강원 자료의 표시 검증 예이며 PDF 재실행 결과는 아니다.

```text
[감독관] 저장 결과 검토 요약 (정답률 등급 아님)
  데이터 규칙: 검토 필요 (확인된 규칙 위반 664건 / 의심 209건)
  검증 범위: 원문 대조 범위 부족 (비교 가능한 원문 항목 0건)
  개별 원문 객체 연결: 0/3057행 (사실 정확도 아님)
  식별 정보가 충분한 충돌 검사 대상: 392/1114행 (사실 정확도 아님)
  사실 정확도: 미평가 (골든 평가 OFF)
```

전역 골든 기본 설정과 실제 `.env`는 이번 변경에서 수정하지 않았다. 강원 테스트는 기존 명령의 `--evaluation-mode auto`를 `--evaluation-mode off`로 바꾸면 된다. `--allow-draft-evaluation`은 OFF 실행에서 필요 없다. 새 출력 이름·새 상태 폴더를 사용하며 이미지/모델/추출 옵션은 이전 조건을 유지한다. 이번 수정은 최종 검토 표시 변경이며 원문 감사의 2행 머리글 인식 개선이나 전체 PDF 실호출은 포함하지 않는다.

## 세 가지 결과 구분

| 결과 | 의미 | 해석 한계 |
|---|---|---|
| 실행/저장 상태 | 배치 상태와 실제 저장 전후 셀 일치 | 성공 호출이 사실 정확도를 보장하지 않음 |
| 규칙 진단 | 필수 정보, 타입, 단위 차원, 기간, 지역, 중복·충돌, 출처 | 종합 100점이나 사실 정답률로 환산하지 않음 |
| 골든 대조 | 고정 범위·계약의 셀 정확도, 행 재현율, 골든 매칭 정밀도 | 불완전 골든의 미매칭을 실제 오답으로 확정하지 않음 |

`confirmed`는 **확인 가능한 규칙 위반**이다. 예산 단위가 `일/tCO2eq/가구/대`인 경우, 잘못된 연도 형식, 필수 공란, 저장 전후 셀 불일치가 포함된다. 모든 사실이 틀렸다는 뜻은 아니다. `suspected`는 다른 지역 비교자료·미등록 단위·동일 식별자의 값 충돌처럼 원문 검토가 필요한 상태다. `unassessed`는 근거/분모 부족이다. 구 형식 경고의 `오류` 표기만 보고 확정 오답으로 올리지 않는다.

같은 경고의 원기록 수(`occurrences`)와 중복 제거된 문제 수, 위치가 확인된 영향 행 수를 따로 센다. 행 좌표가 없는 과거 충돌 경고를 임의로 852개의 오답 행으로 해석하지 않는다. 빈 16개 시트도 문서별 필수 여부를 추측해 일괄 감점하지 않는다.

연간 행, 명시 기간 합계, 정성·조직 행의 필수 필드를 구분하고 0을 공란으로 취급하지 않는다. 지역 별칭은 명시된 광역명 별칭만 허용하며 서울특별시와 강남구를 합치지 않는다. 원문 페이지의 숫자·단위·사업 표현 존재는 보조 진단이다. 존재 자체로 사실 정확도를 승인하지 않는다. 객체 연결률 0과 사실 정확도 0은 다르다.

## 산출물

Excel과 같은 위치에 저장한다. run-state를 꺼도 생성한다.

| 파일 | 내용 |
|---|---|
| `실행명_quality_report.json` | 지표 값·분모·범위·가용성·미평가 사유, 경고 집계, 골든 요약, LLM 보조 의견 |
| `실행명_quality_report.md` | 사람이 읽는 요약과 v1/v2 골든 결과 |
| `실행명_quality_issues.jsonl` | 규칙 ID, 확정 수준, 실제 시트/행/셀, 근거, 조치, 중복 기록 수 |
| `실행명_evaluation.json` | 골든 평가가 가능할 때 데이터셋·원문/골든/출력/매니페스트/코드 해시, 범위, 점수 |
| `실행명_evaluation_v2_details.json` | 대응·미매칭·모호성·셀 차이 |
| `실행명_evaluation_v1/` | 과거 고정 채점 계약의 참조 점수·상세 리포트 |
| `실행명_pipeline_audit.json` | 기존 원장 + `context.quality/evaluation/llm_review`의 경로·해시·요약 |
| `실행명_run_outcome.json` | `excel_saved`, `run_status`, `review_status`, `evaluation_status`, 보고서 링크 |

`run_status`: `completed/partial/no_data`. `review_status`: `needs_review/no_rule_findings/not_evaluated`. 규칙 이상 없음은 사실 정확도 승인과 다르다. 골든 평가는 `evaluated/unassessed/disabled/required_unavailable/failed`로 구분한다. 품질 보고 실패·필수 골든 평가 불가도 이미 저장한 Excel을 보존하고 명확한 경고를 남긴다. 기존 CLI 성공 종료 코드 0 계약은 유지하므로 자동 처리는 `run_outcome`의 별도 상태를 확인해야 한다.

## 설정과 짧은 실행 예

실제 `.env`는 자동 수정하지 않았다. 새 설정의 기본값은 다음과 같다.

```dotenv
QUALITY_REPORT_ENABLED=1
EVALUATION_MODE=auto
EVALUATION_MANIFEST_PATH=
EVALUATION_DATASET_ID=
ALLOW_DRAFT_EVALUATION=0
EVALUATE_HOLDOUT=0
```

기존 main 실행 명령 그대로 저장 후 규칙 검사가 추가된다. 새 모델 호출은 없다. `QUALITY_REPORT_ENABLED=0`은 규칙 보고서만 끄고, `EVALUATION_MODE=off`는 골든 대조만 끈다. CLI가 필요한 경우:

```powershell
py main.py ".\서울특별시_탄소중립계획.pdf" --output ".\output\서울_검증.xlsx" --allow-draft-evaluation
```

위 옵션은 **검토 전 서울 골든을 사용하겠다는 명시 선택**이다. 기존 모델 선택·재시도·이미지 옵션은 원래 실행 조건을 그대로 사용한다. 추가 옵션은 `--quality-report on|off`, `--evaluation-mode auto|off|required`, `--evaluate`(required 호환), `--evaluation-manifest`, `--evaluation-dataset`, `--evaluate-holdout`이다. required도 저장 Excel/기존 종료 코드는 보존하고 평가 미완료를 기록한다.

## 골든 승인과 채점 계약

기본 매니페스트는 `data/evaluation/benchmark_manifest.json`이다. 기존 서울 골든을 상대 경로로 복사하고 원문/골든 SHA를 유지했지만, **사람 최종 검토 전인 draft를 approved로 임의 변경하지 않았다**. 기본 auto는 등록·잠금·승인된 development 골든과 원문 SHA가 일치해야 채점한다. draft는 위 옵션 또는 `ALLOW_DRAFT_EVALUATION=1`을 명시해야 한다. holdout은 별도 허용 없이는 열지 않는다. 경기·강원은 골든이 없어 정확도가 null이고 사유가 표시된다. 제목/파일명 유사성으로 서울 골든을 다른 문서에 적용하지 않는다.

새 골든을 등록하려면 새 데이터셋 ID, `role`, `review_status`, `locked`, `source_sha256`, `golden`(매니페스트 상대경로), `golden_sha256`을 지정한다. 지역별 판독 결과를 검토해 골든을 확정한 뒤 새 버전에서 approved로 등록한다. 동일 ID의 잠근 골든을 조용히 교체하지 않는다. `.xlsx`는 기존 저장소 제외 정책이므로 배포 시 골든을 별도 제공하거나 외부 매니페스트 경로를 지정해야 한다. 파일이 없으면 미평가로 표시한다.

부분 골든은 `scope: {"kind":"partial", "pages":[1,2], "sheets":["11_재정투자계획"]}`처럼 범위를 지정한다. 골든과 출력 모두에 같은 필터를 적용하고 출처 미지정 행은 `scope_unlocated_*`로 별도 집계한다. 페이지·시트·필드 필터가 하나도 없으면 부분 범위 평가는 거부한다.

- **v1 참조:** 10월 26일 비교에서 사용한 16개 시트의 고정 열 계약과 채점기. 순서 의존 등 과거 한계도 유지하며 새 점수로 과거 기록을 바꾸지 않는다. `utils/evaluation_v1`은 당시 5개 모듈을 격리했고 상대 import, 고정 헤더 주입, 워크북 닫기를 적용했다. 재현에 쓰인 Organizer 정규화도 복사해 고정하고 코드 해시를 기록하므로 이후 업무 정제 변경이 과거 채점에 섞이지 않는다. 기존 `scripts/score_against_golden.py`는 덮어쓰지 않았다.
- **v2:** 식별 정보로 유일하게 대응되는 경우만 매칭한다. 여러 후보 중 골든 값에 가까운 행을 고르지 않는다. 모호성은 미매칭으로 남기며 순서에 영향을 받지 않는다. 같은 통화 차원의 `2억원=200백만원`은 인정하고 CO2/CO2eq·활동량/예산은 합치지 않는다. v1보다 보수적인 대응 때문에 점수가 낮아질 수 있으며 모델 성능 하락을 뜻하지 않는다. 두 버전 점수를 혼합 비교하지 않는다.

## 검증

외부 모델 경계만 fixture/mock으로 대체하고 실제 PDF 객체·Organizer·Excel writer·저장 재조회·평가·보고서 저장을 통과시킨다. 신규 기능 ON/OFF에서 추출 요청과 모든 업무 셀이 동일한지 검사한다. 보고서 저장 실패, 골든 부재/해시 오류, draft/holdout 통제, 0값·정성/기간 행, 중복·충돌, 순서/단위 동치/부분 범위도 포함한다.

```powershell
py -m pytest tests/test_saved_quality.py tests/test_saved_quality_pipeline.py tests/test_main_v9_end_to_end.py -q -p no:cacheprovider --basetemp ".\.test_tmp\quality_$(Get-Date -Format yyyyMMdd_HHmmss)"
```

10월 26일 두 저장 결과의 추가 검증 기록은 `output/quality_evaluation_validation_20261027/`에 있다. 원본 Excel·감사 파일은 해시로 보존하고 실제 모델 호출 없이 수행했다. 연결·회귀 검증과 새 모델 정확도 실측은 구분한다. 독립 재평가 실행기, 새 2차 판독, 비금액 예산의 실제 차단/분리 수정은 이번 범위에 포함하지 않았다.
