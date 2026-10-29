# 네 가지 감사 분류 수정과 재평가 — 2026-10-24

`codex/source-coverage-audit` 브랜치의 공용 감사 모듈 `utils/source_coverage.py`를 수정했습니다. 독립 실행기와 이 작업 폴더의 `main.py --source-coverage audit`가 함께 사용합니다. 원본 프로젝트·`tmp2`·모델·추론 강도·프롬프트·라우팅·배치·`.env`·업무 Excel 작성은 변경하지 않았습니다. 실행 코드의 변경은 공용 감사 모듈 하나이며, 검증 CLI·테스트·문서를 함께 보완했습니다. 커밋·push·병합은 수행하지 않았습니다.

## 수정한 동작

| 문제 | 수정 | 적용 함수 |
|---|---|---|
| 354쪽 수익금/정산금을 투자예산 누락으로 취급 | 명시 수익금·수입금 제목은 `excluded/non_investment_revenue_outside_budget_scope`. 원래 금액·단위·출처·기존 사실 ID는 보존. 수익금 제목과 예산 열/투자 문맥이 충돌하면 `mixed_revenue_spending_context`로 보류 | `_financial_role`, `itemize` |
| 267쪽 차량 동력원 괄호가 Excel에서 생략되어 거짓 누락 | 알려진 차량 동력원 열거만 분리하고 정확한 나머지 이름을 대조. 단일 원문 사업, 같은 연도·출처, 서로 다른 동력원 집합의 경쟁 없음, 2개 이상 명시 재원 내역과 합계 일치, 실제 Excel 재원별 금액/문맥 일치가 함께 필요 | `_vehicle_qualifier_base`, `_funding_components`, `RowIndex._vehicle_funding_proof`, `_identity_options` |
| 283쪽 연간 칸 공란/기간 합계를 단위 부족으로 보류 | 단위 판정 전에 연간 칸이 전부 공란인지 확인. `excluded/source_annual_values_blank_total_only`로 구분하며 기간 합계 594,200 원문과 열 위치는 `aggregate_values`에 보존. 합계를 매년 복제하지 않음 | `itemize` |
| 361쪽 병합 사업명이 붙은 반복 머리글의 `’26`을 판독불가 값으로 취급 | 명시 연도 2개 이상과 구분 머리글/연도 표기를 확인. 연도별 위치를 `excluded/repeated_header_with_merged_business_label`로 보존하고 이후 대조의 연도 머리글을 갱신. 사업명을 임의로 다음 공란 행에 채우지 않음 | `itemize` |

차량 괄호는 CNG·전기·수소 등 동력원 2종 이상이 명시된 경우에 한정합니다. 지역·단계·임의 설명·단일 동력원 괄호는 삭제하지 않습니다. 원문 국비/시비 등의 행을 사업 사실로 새로 생성하는 기능도 아닙니다. 명시 합계 바로 아래의 재원 내역을 별칭 검증 근거로만 사용하며, 해당 내역이 누락/충돌하면 확정 반영으로 처리하지 않습니다. 총액 충돌이나 단위 차이는 기존 검토 상태로 남습니다.

수익금 제외는 감사 범위에만 적용합니다. 최초 추출기가 이미 수익금을 잘못 예산으로 저장한 경우 그 셀을 지우는 기능은 아닙니다. 예산이라는 명시 열과 수익금 제목이 함께 있으면 근거 충돌을 보존하며 임의 제외하지 않습니다. 제목이 투자계획인 사업의 이름에 '정산금'이 있다는 이유만으로 예산을 제외하지도 않습니다.

## 적용 경로

| 경로 | 실제 효과 |
|---|---|
| `scripts/audit_source_coverage.py` | 저장된 객체·Excel을 새 규칙으로 대조. 모델·추출·정제·Excel 쓰기 재실행 없음 |
| `main.py --source-coverage audit` | 기존 파이프라인으로 Excel을 만든 후 같은 규칙으로 대조 기록 저장 |
| 감사 OFF | 이번 변경에 따른 모델 호출·업무 셀 변경 없음 |
| 자동 2차 판독 | 아직 구현/활성화하지 않음. `excluded`는 재판독 대상에서 제외한다는 기록만 남음 |

별도 작업 폴더는 `C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트`입니다. 원본 프로젝트나 `tmp2`에 직접 수정한 것이 아니므로 해당 폴더에서 실행해야 합니다. `.env` 변경이나 새 실행기는 필요하지 않습니다.

## 검증 결과

- 오프라인 테스트 **204 passed / 8.56초**: 기존 핵심 120건과 감사 관련 84건. 유료 모델 호출 없음.
- 실제 PDF 파싱 → 외부 응답 fixture → 실제 정제 → Excel 저장 → 재개방 대조를 포함했습니다. 감사 ON/OFF의 모델 호출 인자·횟수와 업무 셀 값/자료형이 같습니다.
- 수익금 제외는 Supervisor 저장 후 감사와 독립 재감사에서 같은 결과였습니다. 차량 별칭은 실제 Organizer·Excel을 거친 셀 주소와 원시/정제 스냅샷에서도 확인했습니다.
- 잘못된 출처/연도, 재원 금액 누락/충돌, 다른 계획, 지역/단계 괄호, 다른 동력원 세부 사업, 단위 차이, 명시 0, 연도와 같은 숫자의 정상 금액을 시험했습니다. 중복 수익금 객체는 같은 ID의 진단을 중복 생성하지 않고 출처 별칭을 보존합니다.
- 기존 검토 표본 **40/40 유지**.
- 추가 32건: 수정 전 V2 결과를 넣은 대조 실행은 **28/32**, 불일치 H16/H22/H31/H32 그대로 재현. V3는 **32/32 일치**.
- 네 가지 문제를 수정하기 위해 32건을 사용했으므로 V3의 32/32는 **개발 회귀 검증**입니다. 새 미사용 표본의 독립 검증이나 모델 정확도 측정이 아닙니다. 이전 원문 검토 파일과 28/32 기록은 수정하지 않았습니다.

### 저장 결과 전체 재감사

같은 `tmp2/output/서울_초기통합본_6Luna_전체_20261021_221648_497103`의 파일을 읽기만 했습니다.

| 비교 항목 | V2 | V3 |
|---|---:|---:|
| 비교 가능 사실 | 535 | 525 |
| 동일 시트 반영 | 238 | 239 |
| 다른 시트 반영 | 3 | 3 |
| 누락 후보 | 280 | 269 |
| 귀속 검토 | 2 | 2 |
| 충돌 검토 | 9 | 9 |
| 단위 검토 | 3 | 3 |
| 제외 진단 | 833 | 904 |
| 해석 보류 진단 | 1,390 | 1,319 |

기존 535개 사실의 ID는 모두 보존됐고, 그중 수익금 10개 연도 항목은 제외 진단으로 옮겨 비교 가능 사실에서는 빠졌습니다. 누락 후보 감소 11건은 **도로청소차 기존 반영 1건 + 수익금 10개 연도 항목 재분류**입니다. 새로 추출하거나 Excel 값을 채운 결과가 아닙니다. 진단 수는 행 단위 공란/반복 머리글 분리에도 영향을 받으므로 정확도 분모나 개선율로 쓰지 않습니다.

실제 예산 셀 부재로 확인한 추가 표본의 10건은 계속 누락 후보입니다. 이 과거 실행에는 원시/정제 스냅샷이 없어 최초 판독 누락인지 후단 손실인지는 단정할 수 없습니다. 기존 충돌 9건과 단위 검토 3건도 그대로 남습니다.

## 재현 명령

분류 확인만 하려면 다음 독립 감사로 충분합니다. 모델 호출이나 `main.py` 전체 재실행은 필요하지 않습니다. 같은 실행의 PDF·Excel·원래 pipeline audit를 사용합니다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트"
$projectRoot = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$savedRun = "$projectRoot\tmp2\output\서울_초기통합본_6Luna_전체_20261021_221648_497103"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"

py scripts/audit_source_coverage.py `
  --input "$projectRoot\서울특별시_탄소중립계획.pdf" `
  --excel "$savedRun.xlsx" `
  --pipeline-audit "${savedRun}_pipeline_audit.json" `
  --output-prefix ".\output\후보분류_v3_$stamp"
```

자동 회귀:

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
$testTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py -B -m pytest tests/test_source_coverage.py tests/test_source_coverage_v2.py tests/test_source_coverage_v3.py tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py -q --basetemp ".test_tmp\coverage_$testTag"
```

실제 전체 추출도 새로 하려는 경우에는 [V2의 전체 실행 안내](source_coverage_rules_v2.md)를 참고할 수 있습니다. 기존 모델과 조건에 `--source-coverage audit`를 붙이는 방식은 같습니다. 테스트 후 실제 실행에서 dotenv를 다시 쓰려면 `Remove-Item Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue`를 실행합니다.

## 산출물과 보존

이 작업 폴더의 `output`에 저장했습니다.

- `source_coverage_v3_delivery_20261024_missing_candidates.jsonl`
- `source_coverage_v3_delivery_20261024_coverage_summary.json`
- `source_coverage_v3_delivery_20261024_pipeline_audit.json`
- `coverage_v3_delivery_review40_20261024.json`, `coverage_v3_delivery_review32_20261024.json`
- `coverage_v3_review32_v2_control_20261024.json`: 이전 결과를 넣어 네 가지 불일치가 계속 검출되는지 확인
- `coverage_v3_regression_delivery_20261024.xml`: 204건 테스트 기록
- `coverage_v3_delivery_preservation_20261024.json`: 입력·원본 tmp2·모델 설정·프롬프트 보존 및 구현 해시
- `coverage_rules_v2_snapshot_20261024/`: 수정 직전 코드와 검증기/문서 사본

검증 CLI `scripts/verify_coverage_review.py`는 32건의 원문 검토 파일에 고정된 기대 상태와 사유를 읽도록 보완했습니다. 원문 검토 파일을 분류 코드에 넣거나 기대 결과에 맞춰 원본을 수정하지 않았습니다. 상세 진단의 규칙 버전은 `source-coverage-v3`, 기존 사실 ID 버전은 유지합니다.

후속 범위는 실제 예산 누락의 제한적 2차 추출과 신규 사실 검증/병합입니다. 별칭 연결을 무제한 확장하거나 보류를 모두 재호출하는 방식은 이번 수정에 포함하지 않았습니다.
