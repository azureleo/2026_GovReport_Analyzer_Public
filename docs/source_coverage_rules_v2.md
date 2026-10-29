# 누락 후보 분류 보완 — 2026-10-24

> 이 문서는 V2 당시 기록입니다. 이후 새 표본 32건에서 발견한 네 가지 문제의 수정·재평가는 [V3 안내](source_coverage_rules_v3.md)에 있습니다. V2의 40/40과 새 표본 검증 결과를 구분해 보존합니다.

이번 변경은 `codex/source-coverage-audit`의 감사 분류기에만 적용했습니다. 원본 프로젝트와 `tmp2`는 그대로이며, 모델·추론 강도·프롬프트·라우팅·배치·정제·Excel 업무 값은 수정하지 않았습니다. 기존 출력 재평가에는 모델 호출이 없었습니다.

## 무엇을 고쳤는가

| 확인된 문제 | 수정한 판단 | 자동으로 하지 않는 일 |
|---|---|---|
| 표의 사업명과 Excel의 표 제목형 사업명이 달라 이미 있는 예산을 누락으로 표시 | 제목·원문 페이지가 맞고, 단일 사업 표이거나 세부 사업 열이 명시적으로 일치할 때 연결 | 금액이 같다는 이유만으로 다른 사업 연결 |
| 세부 사업 감축량이 상위 사업명으로 저장됨 | 원문 활동량 표와 감축표의 제목·인접 페이지, 연차별 계획의 세부 사업·활동량·단위·출처·관리번호, 감축행의 활동량·출처가 함께 맞을 때 연결 | 관리번호만으로 모든 하위 사업 합치기 |
| CO2/CO2eq 차이가 일반 누락으로 묶임 | 대응 셀과 연결 근거를 남기고 `unit_review`로 분리 | 두 단위를 같은 것으로 간주하거나 Excel 값 수정 |
| 공란·페이지 렌더 이미지를 모두 같은 보류로 기록 | 명시 공란은 `excluded`, 페이지 전체의 대리 이미지도 `excluded`; 실제 삽입 이미지는 보류 유지 | 공란을 0으로 바꾸기, 삽입 이미지 누락을 해결했다고 간주하기 |
| 복잡 도식·사업 귀속 부족·단위 부족이 구분되지 않음 | `non_tabular_diagram_or_fragment`, `caption_business_context_required`, `explicit_unit_evidence_required` 등 별도 사유 | 제목만 보고 여러 사업/지표에 값을 임의 배정하기 |
| 표 바깥의 네이티브 단위가 비교 입력에서 빠짐 | 원래 파서가 읽은 명시 `단위:` 객체를 보존. 표와 가로 영역이 겹치고 표 위 48pt 이내인 단위 근거가 유일할 때만 사용 | Excel 단위로 원문 단위 추정, PDF/VLM 추가 호출 |

출처 객체·근거 ID·페이지·좌표·원문 문자열과 실제 Excel 셀 주소를 보존합니다. `source-coverage-v2`로 판정을 표시하되 기존 비교 가능 사실의 ID는 유지하므로 같은 사실 집단의 전후 비교가 가능합니다. 출력 충돌은 계속 보류하며, 정상 셀 하나가 있다는 이유로 다른 값의 중복 행을 숨기지 않습니다.

구현은 `utils/source_coverage.py`의 `source_snapshot`, `itemize`, `RowIndex._identity_options`, `RowIndex.match`, `compare`입니다. `snapshot`은 세 대상 시트 외에 `09_연차별이행계획`의 행을 계층 확인 근거로 보존합니다. 연차별 시트가 새로운 누락 평가 대상으로 추가된 것은 아닙니다. 이 스냅샷과 단위 객체는 감사용이며 추출 모델의 입력을 바꾸지 않습니다.

## 기존 원문 검토 표본으로 재평가

원문 PDF·실제 Excel 셀을 확인했던 40건을 고정된 개발 회귀 표본으로 사용했습니다. **새 표본의 독립 정확도 평가가 아닙니다.**

| 원문 검토 결과 | 건수 | V2 결과 |
|---|---:|---|
| 명시 예산의 최종 셀이 없는 사례 | 14 | `missing_candidate` 유지 |
| 다른 사업명/계층 표현으로 이미 반영된 사례 | 3 | `covered`로 정정 |
| 값은 대응하지만 CO2/CO2eq 검토가 필요한 사례 | 3 | `unit_review`로 분리 |
| 기존 동일 시트 반영 | 10 | `covered` 유지 |
| 다른 지원 시트 반영 | 3 | `covered_elsewhere` 유지 |
| 페이지 대리 이미지·공란·복잡 도식 등 | 7 | 제외/보류를 구체적인 이유로 분리 |

- V1 결과를 같은 기준에 넣은 대조 실행: **27/40 일치**, 종료 코드 `2`.
- V2 재평가: **40/40 일치**, 종료 코드 `0`.
- 여기서 '일치'는 사람이 정리한 분류 상태와 사유의 일치입니다. 원래 모델의 셀 정확도 100%라는 뜻이 아닙니다.
- 원문 검토 파일은 분류 코드에서 읽지 않습니다. 별도 검증기 `scripts/verify_coverage_review.py`만 정답 표본과 새 JSONL을 대조합니다.

**300쪽 단위 사례에 대한 원인 정정:** PDF 이미지에는 단위가 보이지만, 원본 PDF의 네이티브 텍스트에도 해당 단위 문자열이 없습니다. 따라서 이전 검토의 '저장 문맥 손실'을 확정 원인으로 볼 수 없습니다. 현재는 명시 단위 근거가 필요한 보류로 유지합니다. 이미지 판독 또는 사람이 확인한 단위 근거를 별도로 전달하는 경로는 아직 구현하지 않았습니다.

## 전체 감사 결과의 범위 변화

대상 실행은 `tmp2/output/서울_초기통합본_6Luna_전체_20261021_221648_497103`입니다. 원본 Excel·PDF·감사 파일은 수정하지 않았습니다.

기존 **422개 사실 전부**가 같은 ID로 유지됐습니다.

| V1 → V2 | 건수 |
|---|---:|
| 반영 → 반영 | 173 |
| 누락 후보 → 반영 | 49 |
| 누락 후보 → 누락 후보 | 183 |
| 누락 후보 → 귀속 검토 | 2 |
| 누락 후보 → 단위 검토 | 3 |
| 반영 → 충돌 검토 | 9 |
| 다른 시트 반영 → 유지 | 3 |

49건 중 원문 검토 표본에서 확인한 정정은 3건이며, 나머지를 모두 사람 검증 완료로 표시하지 않습니다. 충돌 9건은 같은 사업·연도·재원구분에 서로 다른 금액의 저장 행이 함께 연결된 경우입니다. 예를 들어 합계 금액과 다른 금액들이 모두 `재원구분=합계`로 저장되어 있어, 원본 값을 덮어쓰지 않고 검토 대상으로 남겼습니다.

기존 단위 객체를 재사용하면서 **113개가 새로 비교 가능**해졌습니다. 따라서 V1 누락 후보 237개와 V2 280개를 직접 비교해 누락이나 정확도가 악화됐다고 판단할 수 없습니다.

V2의 비교 가능 사실 535개: 동일 시트 반영 238, 다른 시트 반영 3, 누락 후보 280, 귀속 검토 2, 충돌 검토 9, 단위 검토 3입니다. 별도로 제외 진단 833, 해석 보류 진단 1,390건이 있습니다. 진단은 업무 사실/행의 수가 아니며 정확도 분모로 사용하지 않습니다.

이 과거 실행에는 원시/정제 스냅샷이 없어 그 두 단계는 여전히 `unknown`입니다. 297개 배치 기록도 호출 여부 필드가 없는 과거 기록이므로 `unknown_historical=297`이며 '호출 0회'가 아닙니다. **이번 독립 감사의 추가 모델 호출만 0회**입니다.

## 검증과 산출물

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트"
$env:PYTHON_DOTENV_DISABLED = "1"
$testTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py -B -m pytest tests/test_source_coverage.py tests/test_source_coverage_v2.py tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py -q --basetemp ".test_tmp\coverage_$testTag"
```

**178 passed**. 외부 응답은 fixture이며, PDF 파싱·정제·병합·Excel 저장·실제 셀 재조회는 실제 코드로 검증했습니다. 잘못된 사업/연도/재원/페이지, 관리번호만 같은 사례, 단위 충돌, 중복/충돌, 공란과 명시 0, 감사 OFF/ON의 호출 인자·업무 셀 동일성을 포함합니다. 실제 모델의 정확도/속도를 재측정하지 않았습니다.

산출물은 이 작업 폴더의 `output`에 있습니다.

- `source_coverage_v2_delivery_20261024_missing_candidates.jsonl`
- `source_coverage_v2_delivery_20261024_coverage_summary.json`
- `source_coverage_v2_delivery_20261024_pipeline_audit.json`
- `coverage_v2_delivery_review_20261024.json`: 40건과 동일 사실 집단의 전환 내역
- `coverage_v1_review_baseline_20261024.json`: 이전 규칙을 넣은 대조 결과
- `coverage_v2_regression_final.xml`: 회귀 테스트 기록
- `coverage_v2_preservation_20261024.json`: 원본·설정·프롬프트 보존 확인

검증기 재실행 예:

```powershell
$projectRoot = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$reviewTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/verify_coverage_review.py `
  --review "$projectRoot\output\candidate_review_20261024\review_results.json" `
  --journal ".\output\source_coverage_v2_delivery_20261024_missing_candidates.jsonl" `
  --baseline-journal ".\output\tmp2_source_audit_20261023_delivery_missing_candidates.jsonl" `
  --output ".\output\coverage_review_$reviewTag.json"
```

검증기 종료 코드 `0`은 표본 분류 일치, `2`는 불일치입니다. 기존 파일을 덮어쓰지 않습니다. 고정 검토 표본으로 규칙을 계속 조정한 뒤 같은 40건의 일치율만 새 정확도라고 보고하지 말고, 다음에는 규칙 수정에 쓰지 않은 별도 표본을 검토해야 합니다.

## 재판독이 필요한가

**이번 분류 수정 검증에는 필요하지 않습니다.** 저장된 객체·Excel·감사 파일로 재평가를 이미 완료했습니다. 같은 방식으로 새 출력명만 지정하면 언제든 독립 재평가할 수 있습니다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트"
$projectRoot = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$savedRun = "$projectRoot\tmp2\output\서울_초기통합본_6Luna_전체_20261021_221648_497103"
$auditTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/audit_source_coverage.py `
  --input "$projectRoot\서울특별시_탄소중립계획.pdf" `
  --excel "$savedRun.xlsx" `
  --pipeline-audit "${savedRun}_pipeline_audit.json" `
  --output-prefix ".\output\후보분류_재검증_$auditTag"
```

독립 감사 종료 코드 `0`은 파일 생성 성공이며 누락이 없다는 뜻은 아닙니다.

### 새 업무 Excel과 단계별 기록도 필요할 때만 전체 실행

아래는 기준 실행의 기록에 있는 **Codex / gpt-6-luna**를 그대로 명시한 예입니다. 이 작업 폴더와 `tmp2`의 `.env`에는 provider·모델 설정이 없으므로, 단순히 `.env`가 동일하다는 이유로 같은 모델이 선택된다고 보장할 수 없습니다. `.env`를 수정하지 않고 CLI로 명시합니다. 추론 강도는 바꾸지 않습니다. 다른 기준 모델과 비교하려면 그 실행 기록의 값을 별도로 확인해야 합니다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트"
Remove-Item Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue
$runTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
$outBase = ".\output\서울_후보분류검증_$runTag"
New-Item -ItemType Directory -Path ".\output" -Force | Out-Null

py -u main.py "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트\서울특별시_탄소중립계획.pdf" `
  --agent codex --agent-model gpt-6-luna `
  --output "$outBase.xlsx" --source-coverage audit --retries 1 `
  2>&1 | Tee-Object -FilePath "$outBase.log"
$runExitCode = $LASTEXITCODE
Write-Host "종료 코드: $runExitCode / 결과: $outBase"
```

이 명령은 **전체 기존 파이프라인의 실제 모델 호출**을 수행합니다. 확인된 14개 누락만 재판독하는 명령은 아닙니다. `--source-coverage audit`는 기록만 추가하고, 누락 값을 자동 보완하지 않습니다. 자동 2차 추출·예산 상한·신규 후보 검증/병합은 후속 단계입니다. 현재 초기 통합본은 `--env-file`을 지원하지 않습니다. PowerShell의 기존 환경변수는 dotenv보다 우선하므로 다른 실험용 환경변수가 남아 있다면 새 터미널에서 실행 조건을 확인하세요.

새 전체 실행의 의미는 실제 결과 재측정과 원시 → 정제 → 저장 구간의 기록 확보입니다. 원문에서만 보이는 단위, 복잡 도식, 귀속이 모호한 병합 행은 계속 사람/추가 판독 검토 대상입니다.
