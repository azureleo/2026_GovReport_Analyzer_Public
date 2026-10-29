# 원문 누락 후보 감사 — 2026-10-23

> 2026-10-24 최신 수정·검증은 [V3 안내](source_coverage_rules_v3.md)를, 첫 분류 보완은 [V2 안내](source_coverage_rules_v2.md)를 참고하세요. 아래 최초 검증 수치와 해시는 V1 당시 기록입니다.

## 작업 위치와 보존

- 브랜치: `codex/source-coverage-audit`
- 작업 폴더: `C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트`
- 기준: `tmp2`의 **현재 미커밋 복원 소스까지 포함한** 10월 10일 초기 통합본. 단순히 Git HEAD만 가져오지 않았습니다.
- Git 기준 커밋: `baceb021820df6c5b3952ce89ee9804af5b7f138`
- 복사 전/작업 후 원본 `tmp2` 구현 SHA256:
  `07669a4d6cf1a6ba21c14d51b1a1dfe5b45c39474de98a09854a6c62d34f1fc7` — 동일.
- 새 구현 SHA256:
  `46e6d58ab946a56dd1470af199e4d906bb9d6bfe9da15dabc767171f0ab79a57`
- 가이드라인 프롬프트 SHA256:
  `3334767dd258e7c89670d5999131380df502d06f89df74ad537ff3a912de5b51` — 동일.
- `.env`는 원본 tmp2와 바이트 단위 동일하며 수정하지 않았습니다. `config.py`의 기존 설정 값도 동일하고, 새 감사 옵션과 내부 원장 키만 추가했습니다.
- 원본 프로젝트와 `tmp2`의 소스·Excel·실행 기록은 수정하지 않았습니다. 원격 push·merge·자동 커밋은 하지 않았습니다. 새 브랜치의 작업은 미커밋 상태입니다.

초기 복원 검증기 `verify_initial_restoration.py`는 그대로 보존했습니다. 개선 폴더에서 그 검증기를 실행하면 **새 구현 해시가 초기 해시와 다른 것이 정상**입니다. 검증기를 새 해시에 맞춰 느슨하게 변경하지 않았습니다.

## 구현 흐름

| 단계 | 실제 동작 | 구현 위치 |
|---|---|---|
| 추출 전 | native 표·시각 객체 표시·표지 문맥의 비교용 스냅샷 보존. 이미지 본체/전체 HTML은 중복 저장하지 않음 | `source_snapshot`, Supervisor STEP 0 |
| 추출 기록 | 기존 배치에 모델 경계 요청 여부·키워드 미호출·성공 복원·재개 제한 사유와 입력 해시 기록. 기존 페이지 라우팅 결과 보존 | `ExtractorAgent._record_extraction`, `_extract_sheet`, `_run_extraction_batch`, `extract` |
| 추출/정제 후 | 세 대상 시트의 원시/정제 행을 각각 복사. V2부터 계층 확인용 연차별 행도 보존. 재시도 시 최종 선택 시도와 시도별 원시 행 수 기록 | `snapshot`, Supervisor STEP 2~3 |
| 저장 후 | 실제 Excel을 read-only로 재개방해 열 이름·행 번호·셀 좌표·값 대조 | `read_excel`, `RowIndex`, `compare` |
| 영속 저장 | 상세 JSONL → 요약 JSON. 기존 감사/종료 기록과 경로 연결. 저장 실패는 경고하고 이미 저장한 Excel 유지 | `persist`, Supervisor STEP 4 |
| 독립 실행 | 저장 객체와 실제 Excel만 재사용. 원문 SHA256 확인, 새 실행은 Excel SHA256도 확인 | `audit_saved_run`, `scripts/audit_source_coverage.py` |

새 규칙이 업무 값을 보완하는 단계는 아닙니다. Organizer·Excel 쓰기·품질 기준·재시도 결정과 독립적입니다. `RUN_STATE_ENABLED=0`이어도 감사 결과는 저장됩니다.

이번 기능의 새 파일은 `utils/source_coverage.py`, `scripts/audit_source_coverage.py`, `tests/test_source_coverage.py`, 이 안내서입니다. 기존 파일 수정은 `agents/extractor_agent.py`, `agents/supervisor.py`, `config.py`, `main.py`, `README.md`, `.env.example`, `.gitignore`입니다. Git 상태에 보이는 다른 수정/미추적 파일들은 tmp2의 미커밋 복원 기준선을 보존 복사한 것이며, 이번에 이미지 모델·정제기·Excel 쓰기 동작을 변경한 것이 아닙니다.

## 실행 방법

### 1. 기존 결과 검사 — 권장 시작점, 모델 호출 없음

VSCode PowerShell에서 아래를 입력합니다. 세 입력 경로는 같은 실행의 파일이어야 합니다. 새 출력명은 항상 별도로 만듭니다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\source-coverage-audit\졸업프로젝트"
$projectRoot = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$savedRun = "$projectRoot\tmp2\output\서울_초기통합본_6Luna_전체_20261021_221648_497103"
$auditTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"

py scripts/audit_source_coverage.py `
  --input "$projectRoot\서울특별시_탄소중립계획.pdf" `
  --excel "$savedRun.xlsx" `
  --pipeline-audit "${savedRun}_pipeline_audit.json" `
  --output-prefix ".\output\tmp2_source_audit_$auditTag"
```

`.env`·API 키·Codex CLI·네트워크가 필요하지 않습니다. PDF는 해시 확인만 하며 본문 재파싱/이미지 렌더링도 하지 않습니다. 저장된 `document_objects`가 없거나 문서 해시가 다르면 중단합니다. 입력 PDF와 출력 Excel은 절대 수정하지 않습니다.

출력:

- `*_missing_candidates.jsonl`: 비교 가능 사실의 모든 상태와 해석 보류 진단. 파일명과 달리 전 행이 누락은 아닙니다.
- `*_coverage_summary.json`: 상태별 건수·비교 범위·원시/정제 스냅샷 가용성·입력 해시·생산자/감사기 출처.
- `*_pipeline_audit.json`: 독립 실행의 새 연결 인덱스. 원래의 대형 감사 원장은 수정하지 않고 경로/해시로 연결합니다. 재감사 입력에는 원래 실행의 감사 파일을 사용하세요.

종료 코드 `0`은 **감사 파일 생성 성공**이며, 누락 없음/정확도 통과를 뜻하지 않습니다. 입력 오류·불일치·저장 실패는 `2`입니다. 기존 감사 출력 덮어쓰기는 기본 차단하고 `--overwrite`로만 허용합니다. 이 옵션도 원본 pipeline audit는 덮어쓸 수 없습니다.

### 2. 다음 전체 추출 때 함께 기록

새 작업 폴더의 `main.py`에서 기존 실행 명령에 **`--source-coverage audit`만 추가**합니다. 또는 기존 `.env`/환경변수에 `SOURCE_COVERAGE_MODE=audit`를 명시합니다. 이번 작업에서는 실제 `.env`를 수정하지 않았습니다.

```powershell
py main.py "원본 문서 절대경로.pdf" --output ".\output\새실행명.xlsx" --source-coverage audit
```

이 명령은 기존 전체 파이프라인이므로 **기존 설정에 따른 실제 모델 호출**은 발생합니다. 감사 옵션 자체는 호출을 추가하지 않습니다. 모델명·추론 강도·no-images·라우팅·배치·기존 재시도 옵션은 비교하려는 실행과 동일하게 유지하세요. 이 초기 통합본은 최신 `--env-file` 지원을 전제로 하지 않습니다.

기본 OFF, CLI `--source-coverage off`로 명시적 비활성도 가능합니다. 감사 활성화가 자동 재판독/2차 추출을 켜지는 않습니다.

Supervisor 실행은 Excel 옆에 후보/요약을 저장하고, 기존 `pipeline_audit.context.source_coverage`와 `run_outcome.source_coverage`에 실제 저장 경로·파일 해시·상태를 기록합니다. 원시/정제/추출 전 객체는 `pipeline_audit.ledgers.source_coverage_snapshot`에 있습니다. 감사가 실패하면 경고와 `status=failed`를 기록하고 Excel 성공 상태는 유지합니다.

구현이 달라져 새 작업 폴더의 run-state 지문은 초기 tmp2와 다릅니다. 초기 체크포인트를 새 코드의 성공 응답으로 강제로 복원하지 않습니다. 과거 결과를 재사용하려면 위 독립 실행을 사용하세요.

## 판정 범위와 의미

지원 범위는 **표 안에 식별 가능한 사업명/과제명이 있는** 정량감축량·예산·대응기반 과제/담당부서입니다. 본문 문장 전체나 16시트 전수 누락 평가가 아닙니다.

- 숫자는 쉼표/부호를 읽되, `-`·빈칸·판독불가를 0으로 바꾸지 않습니다. 0은 유효한 값입니다.
- 명시된 연도 열만 비교합니다. `2024~2033`이라는 기간으로 매년 행을 만들지 않습니다. `’24`는 표지/해당 객체의 명시 계획기간이 유일하게 세기를 해소할 때만 읽고 그 근거 객체를 기록합니다.
- 반복 연도 머리글의 실제 행 번호를 기록하고, 합계 열을 연간 값으로 배치하지 않습니다. 빈 사업명은 임의로 위 행에서 채우지 않습니다.
- 명확한 rowspan/colspan 머리글은 기존 HTML 파서 결과를 소비합니다. 복잡한 단위 대응·다중 지표·파싱 오류는 보류합니다.
- ㎡↔ha, kW↔MW, 천그루↔그루, 원/천원/백만원/억원 등 명시 배율만 대조합니다. CO2와 CO2eq를 같게 취급하지 않습니다.
- 투자 표에서 페이지의 명시 화폐 단위가 유일할 때 보조 문맥으로 사용하며 `unit_origin`에 표시합니다. 다른 활동 표로 이 단위를 옮기지 않습니다.
- 사업 코드는 단독 식별자가 아닙니다. 이름·연도·계획·부문·재원·모니터링인자 등 명시 문맥을 구분합니다. 페이지/근거 연결이 없거나 여러 문맥이 경쟁하면 보류합니다.
- 임의의 의미 유사도로 사업을 합치지 않습니다. V2는 표 제목·명시 세부 사업 열·원문 활동량과 연차별 계획의 출처 연결에 한해 대조합니다. 여전히 근거가 부족한 별칭/합계는 사람이 확인해야 합니다.

| 상태 | 해석 |
|---|---|
| `covered` | 해당 명시 사실이 같은 대상 시트의 실제 저장 셀에서 확인됨 |
| `covered_elsewhere` | 지원하는 다른 시트에 동등한 사실이 있음. 재추출 대상으로 자동 지정하지 않음 |
| `missing_candidate` | 명시 사실이 저장 셀에서 매칭되지 않음. 실제 누락 확정 아님 |
| `cleaning_excluded` | 원행 격리 원장과 연결됨. 자동 재시도하지 않음 |
| `conflict_review` / `ambiguous_review` | 값 충돌 또는 문맥/귀속의 근거 부족 |
| `unit_review` | 대응 행이 있으나 단위 차원이 다름. CO2와 CO2eq 등을 자동 동일시하지 않음 |
| `storage_review` | 정제 스냅샷에 있던 사실이 실제 저장 셀에서 확인되지 않음 |
| `unassessable` | 복잡 표·이름 없음·기간·단위 근거 부족·이미지 등 현재 규칙으로 판단할 수 없음 |
| `excluded` | 명시 공란·페이지 대리 이미지·현재 비교 스키마 밖 성과표 등. 업무 데이터 부재나 잘못된 데이터라는 뜻은 아님 |

객체 ID·근거 ID·별칭·물리 페이지·객체 bbox(개별 셀 bbox가 아님)·표의 행/열·원문 행·연도 머리글·단위 원문을 보존합니다. V2는 판정 버전과 사실 ID 버전을 분리하여 기존 사실 ID를 유지합니다. 같은 응답을 다시 검사해도 중복 append하지 않습니다. 원본 객체가 중복되어도 사실 ID를 중복 생성하지 않고 별칭과 source_instances를 합칩니다.

`model_requested`는 **기존 `call_text` 경계에 요청한 여부**입니다. 공급자 내부 캐시·토큰 과금·CLI 내부 재시도 횟수의 실측이 아닙니다. 기존 성공 복원/키워드 미호출/호출 성공 빈 배열/호출 실패를 구분합니다. 단순히 같은 페이지의 배치가 실행됐다고 해당 셀이 프롬프트에 포함됐다고 단정하지 않으며 `exact_cell_content_exposure=not_verified`를 표시합니다. 지자체명·Vision·최종 품질 검수 호출은 이 텍스트 시트 배치 집계 밖입니다.

과거 감사 파일에 원시/정제 스냅샷이 없으면 두 단계는 `unknown`입니다. 최종 셀 부재를 ‘최초 모델이 놓쳤다’ 또는 ‘정제에서 삭제됐다’로 단정하지 않습니다. 과거 Excel 해시가 없으면 파일명 대응만 확인했음을 `legacy_filename_only_unverified_hash`로 표시합니다. 최신 기록은 해시 불일치를 차단합니다. 과거의 객체는 추출 후 객체일 수 있으며 독립 정답 자료가 아닙니다.

`unassessable`은 객체/행/셀 단위의 진단 기록 수입니다. 업무 행 수나 정답 항목 수가 아니며 다른 건수와 단순 합산하여 정확도/누락률을 계산하면 안 됩니다. 추출 전 snapshot은 본문 대부분의 텍스트 객체를 제외한 제한적 비교 인벤토리입니다.

## 검증 결과

Python 3.13.4 / pytest 9.0.3. 추가 패키지 설치나 실제 모델 호출은 하지 않았습니다.

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
New-Item -ItemType Directory -Path ".test_tmp" -Force | Out-Null
$testTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py -B -m pytest tests/test_source_coverage.py tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py -q --basetemp ".test_tmp\coverage_$testTag"
```

- **153 passed / 7.62초**: 신규 33개 + 기존 핵심 120개.
- 실제 PDF 생성·파싱 → 추출(외부 응답 fixture) → 실제 Organizer → 실제 Excel → 재개방 대조를 검증했습니다.
- 감사 OFF/ON에서 **모델 호출 인자·횟수와 16개 업무 시트의 셀 값·자료형**이 동일했습니다.
- 세 대상 스키마의 실제 셀 반영, 빠진 연도, 0/빈칸, 단위 환산, 다른 영역의 같은 과제 코드, 충돌/보류/격리/다른 시트 반영, 성공 배치 재개, 저장 실패 시 Excel 보존을 확인했습니다.
- 독립 CLI는 config·dotenv·LLM 모듈을 import하지 않았으며 네트워크/하위 프로세스 실행 차단 상태에서도 통과했습니다. main CLI의 `--source-coverage audit` 전파도 fixture로 확인했습니다.
- 테스트 기록: `output/source_coverage_regression.xml`. 전체 레거시 테스트를 모두 해결했다는 의미는 아닙니다.

기존 서울 초기 통합 실행 `서울_초기통합본_6Luna_전체_20261021_221648_497103`도 모델 재호출 없이 검사했습니다.

- 비교 가능 명시 사실: **422개** (예산 416, 활동량 3, 예상감축량 3; 최초 설명의 '활동량 6'을 바로잡음).
- 같은 시트 반영 182, 다른 지원 시트 반영 3, 누락 **후보** 237.
- 해석 보류 진단 2,226건. 이미지/이름 귀속 불명확/빈 이름/빈 값 등이 포함되며 2,226개 업무 사실이 빠졌다는 뜻이 아닙니다.
- 과거 실행의 raw/cleaned 단계는 unknown. 237개를 그대로 진짜 누락/모델 오답/재호출 대상으로 확정하지 않았습니다.
- 결과 접두사: `output/tmp2_source_audit_20261023_delivery`. 원본 Excel/PDF/감사 파일은 그대로입니다.

이는 **연결 및 회귀 검증 완료**이지 **모델 정확도 검증 완료**가 아닙니다. 다음 단계는 후보의 사업명 별칭·상하위 사업·재원·원문을 사람이 확인하여 재확인할 범위를 확정하는 것입니다. 제한 예산의 2차 추출과 후보 검증/병합은 이번에 구현하거나 활성화하지 않았습니다.
