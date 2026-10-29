# 확정 누락의 제한적 2차 판독·병합 실행기

2026-10-26 업데이트: 저장된 원문·사업목록의 명시 근거로 계획구분·부문을 보완하고, 기존 실응답을 새 후단으로 재평가하는 `--reuse-run`을 추가했습니다. [분류 보완·응답 재사용 안내](second_pass_context.md)를 참고하세요. 이전 구현의 상태를 현재 코드의 `--resume`으로 강제 재개하지 않습니다.

`codex/second-pass-reading-merge`의 독립 실행 기능입니다. 기존 `tmp2`와 `codex/source-coverage-audit`를 복사해 보존하고, 새 worktree에서 구현했습니다. `main.py`의 1차 호출·라우팅·배치·프롬프트·모델 설정과 `.env`는 변경하지 않습니다.

첫 지원 범위는 **원문 검토로 확정된 `financial_plan`의 연간 예산액**입니다. 현재 선택 파일은 2026-10-21 초기 통합본의 누락 24건이며 표 22개에 해당합니다. 검토 대기 245건은 입력하지 않습니다. 다른 시트·미확정 후보·기간 합계는 이 실행기의 자동 처리 범위가 아닙니다.

## 데이터 흐름과 검증

1. `_confirmed_targets.json`의 승인 상태와 외부 `review_files`의 원문 검토 판정을 읽습니다. 내부 후보의 `pending` 상태를 임의로 승인하지 않습니다. 검토 기록은 감사 규칙 버전이 달라도 **사업·연도·재원·금액·단위·객체/근거 ID·페이지·표의 행/열·좌표·기준 파일 해시**가 일치해야 합니다.
2. 원본 PDF, 기준 Excel, 부모 감사 파일, 후보 JSONL, 검토 파일, 기준 실행 매니페스트를 해시로 고정합니다. 현재 저장 셀이 이미 반영·충돌 상태이면 호출 전에 중단합니다. 스키마와 기준 지자체도 먼저 확인합니다.
3. 같은 표 객체의 대상은 한 번에 묶습니다. 원본 PDF에서 표·머리글과 명시 단위 위치를 이미지로 잘라 전달합니다. 대상 사업·연도·재원은 알려주되 **예상 금액·과거 응답·검토 정답은 모델 입력에 넣지 않습니다.**
4. 기준 실행 매니페스트의 Codex 모델로 읽습니다. 현재 선택의 모델은 `gpt-6-luna`입니다. 모델명·provider·추론 강도를 새로 선택하거나 자동 대체하지 않습니다. 과거 CLI의 실제 추론 강도는 완전히 기록되지 않았으므로 과거 실행과 동일했다고 단정하지 않습니다.
5. 반환한 대상 ID·사업·연도·재원·단위 배율·금액·객체/근거 ID·페이지·원문 인용을 고정한 원문 사실과 대조합니다. 중복 응답은 하나로 처리하고 상충하는 응답·미반환·판독 불가는 보류합니다. 선택하지 않은 사실은 추가하지 않으며 원응답에 보존합니다.
6. **신규 후보만** 실제 `OrganizerAgent.organize` → `validate_and_reclassify(auto_reclassify=False)` → `source_validation`을 통과시킵니다. 기존 값은 정제하거나 덮어쓰지 않습니다. 계획구분·부문은 명시된 계획 절·사업 관리번호·부문 머리글과 사업목록/기존 예산의 근거가 일치할 때 채웁니다. 근거 부족·충돌은 `context_resolution`·`unfilled_context`와 검증 시트·감사 파일에 남깁니다.
7. 기존 셀과 충돌하면 보류하고, 동일 사실이면 중복 추가를 막습니다. 실제 `ExcelAgent.write`로 새 파일을 저장한 뒤 재개방하여 기존 셀 값과 자료형 및 신규 대상 셀을 대조합니다. 출처 페이지·객체/근거 ID·좌표·후보 ID를 출처계보와 감사 결과에 보존합니다.

새 경로를 점검하며 기존 `source_validation`이 정수형 **출처페이지를 측정값으로 검사하는 문제**를 재현해 제외했습니다. 금액 대조는 유지합니다. Excel 재작성에서 빈 문자열 셀이 빈 숫자형 셀로 바뀌는 경우도 별도 보존 처리했습니다. 원본 파일은 수정하지 않습니다.

## 제한·재개 정책

| 항목 | 동작 |
|---|---|
| 기본 실행 | dry-run, 모델 호출 없음 |
| 실제 호출 | `--execute`를 명시한 경우만 수행 |
| 기본 상한 | 표 4회, 누적 판독 루프 300초, 호출당 90초 |
| 전체 시간 제한 | 지정 시간과 기준 실행 시간의 20% 중 작은 값 |
| 현재 기준 시간 | 7,466초, 20% 상한 1,493.2초 |
| 제한 시간의 범위 | 이미지 준비·판독·응답 검사 루프; 후단 정제·Excel 저장은 별도 |
| 프로세스 시간 초과 | 해당 CLI와 자식 프로세스 종료; 종료 처리에 추가 수초 가능 |
| 자동 재시도 | 없음, 표당 최대 한 번 시도 |
| 재개 | `--resume`; 해시가 일치하는 성공 응답 재사용, 미시도 표만 새 호출 |
| 실패·중단 당시 호출 | 자동 재시도하지 않고 보류; 다른 성공 응답은 유지 |
| 상한 확대 | 동일 폴더에서 명시적으로 확대 가능, 기존 횟수·시간은 차감하고 변경 이력 저장 |

20%는 정확도나 소요 시간을 예측하는 수치가 아닌 운영 상한입니다. 모델 서버 내부 재시도나 토큰 과금 횟수는 측정하지 않습니다. `model_requests`는 이 실행에서 CLI 경계에 요청한 횟수이며 누적 횟수는 별도로 기록합니다. `--fixture-responses`는 외부 경계 대체 검증이므로 실제 모델 호출 수는 0으로 기록합니다.

코드·입력·fixture/실제 모드가 달라지면 기존 상태를 재사용하지 않습니다. 동시에 같은 출력 폴더를 실행하지 않습니다. 비정상 전원 종료로 `.run.lock`이 남으면 PID와 살아 있는 프로세스를 먼저 확인해야 합니다. 살아 있는 실행의 잠금 파일을 삭제하지 마세요. 이미 저장된 Excel은 덮어쓰지 않고 재개 시 새 번호를 사용합니다.

## 현재 자료로 실행

VSCode PowerShell에서 **새 작업 폴더**로 이동합니다. PDF·확정 목록은 원래 위치를 그대로 읽습니다. `.env` 수정이나 1차 `main.py` 재실행은 필요하지 않습니다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\.codex\worktrees\second-pass-reading-merge\졸업프로젝트"
$project = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$targets = "$project\output\서울_2차대상준비_20261024_225408_165448_confirmed_targets.json"
$pdf = "$project\서울특별시_탄소중립계획.pdf"
$runTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
$secondDir = ".\output\서울_2차판독_$runTag"

# 입력·호출 계획만 확인: 모델 호출 0회
py -B scripts/run_second_pass.py --targets "$targets" --input "$pdf" --output-dir "$secondDir" --dry-run
```

계획을 확인한 같은 PowerShell 세션에서 다음 명령이 실제 모델을 호출합니다. 출력 폴더의 `plan.json`이 있는 상태에서 첫 실행에는 `--resume`을 붙이지 않습니다.

```powershell
$codexPath = (Get-Command codex.cmd -ErrorAction Stop).Source
py -B scripts/run_second_pass.py --targets "$targets" --input "$pdf" --output-dir "$secondDir" --execute --codex-command "$codexPath" --max-calls 4 --max-seconds 300 --call-timeout 90
```

남은 **미시도 표**를 이어서 실행하려면 같은 `$secondDir`에 `--resume`을 붙이고 누적 운영 상한을 지정합니다. 다음 예시는 현재 22개 표 전체에 대한 시도를 허용하되 누적 1,493초 안에서만 진행합니다. 실패한 표를 다시 호출하는 명령이 아닙니다.

```powershell
py -B scripts/run_second_pass.py --targets "$targets" --input "$pdf" --output-dir "$secondDir" --execute --resume --codex-command "$codexPath" --max-calls 22 --max-seconds 1493 --call-timeout 90
```

## 출력과 상태

| 파일 | 내용 |
|---|---|
| `plan.json` | 입력 해시, 기준 모델, 표별 대상, 검토 근거, 현재 운영 상한 |
| `state.json` | 누적 호출·시간, 상한 변경, 호출 전 기록, 응답 해시, 저장 이력 |
| `images/*.png` | 원본 PDF에서 실제 전송한 표 이미지 |
| `responses/*.json` | 표별 반환 원문, 계획 지문; 실패 응답을 정답으로 복사하지 않음 |
| `recovered_001.xlsx` | 기존 18시트와 검증된 신규 예산 행; 기존 값 유지 |
| `outcome_001.json` | 대상별 승인·보류·미실행·저장 검증 상태, 실제 셀 주소, 경고 |
| `recovered_001_pipeline_audit.json` | 기존 내부 원장 보존, 신규 원문/의미 검사·출처·후단 결과 |
| `recovered_001_context_resolution.json` | 계획구분·부문의 필드별 근거·충돌·공란과 저장 셀 |
| `recovered_001_missing_candidates.jsonl` / `_coverage_summary.json` | 새 Excel을 재조회한 원문 대조 결과 |

시각자료목록의 기존 행도 그대로 보존합니다. 2차 신규 행의 객체 연결 집계는 감사 파일의 `context.second_pass.new_row_object_inventory`에 따로 기록합니다. 누적 모호성·품질 진단을 모델 정확도로 해석하지 않습니다.

종료 코드 `0`은 대상 전체의 검증·저장과 감사 저장 완료, `2`는 보류·상한에 따른 미실행 또는 오류입니다. 성공한 일부가 새 Excel에 저장돼도 보류가 남으면 `2`입니다. Excel 저장 후 감사 파일 저장만 실패한 경우 이미 검증된 Excel은 유지하고 경로·경고를 결과에 남깁니다. `130`은 사용자 중단입니다.

## 검증 범위

`tests/test_second_pass.py`는 유료 모델 경계만 대체합니다. 실제 PDF, Organizer, 의미 검증, 원문 대조, Excel 저장·재조회 및 감사 저장을 실행합니다. 호출·시간 상한, 중단/재개, 중복·충돌, 입력/응답 변조, 미승인 대상, 감사 저장 실패를 검증합니다. 기존 원문 대조·OCR·main 경로도 회귀 검증합니다.

최종 결과는 **239 passed**입니다(신규 35 + 기존 회귀 204). 실제 서울 자료의 고정 응답 연결에서 확정 24건의 셀 저장을 확인했으며 예산 행은 872→896, 기존 18시트의 모든 셀 값·자료형과 원본 입력 해시는 유지됐습니다. 제한적 원문 대조의 누락 후보는 269→245가 됐습니다. 다음 문단의 한계와 함께 해석해야 합니다.

테스트 명령은 다음과 같습니다. `.test_tmp`를 먼저 만들고 사용하지 않은 임시 하위 경로를 지정합니다.

```powershell
New-Item -ItemType Directory -Path .test_tmp -Force | Out-Null
py -B -m pytest tests/test_second_pass.py tests/test_source_coverage.py tests/test_source_coverage_v2.py tests/test_source_coverage_v3.py tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py -q --basetemp ".test_tmp/second_pass_$runTag"
```

검증 기록은 `output/second_pass_final_01.xml`, `output/second_pass_implementation_verification.json`, `output/second_pass_saved_data_fixture_v2_20261024/connection_verification.json`에 보존했습니다. `tmp2` 코드 지문과 이전 감사 worktree 지문이 작업 시작 전과 일치하고, 원본 `.env`와 새 worktree에 복사한 `.env`가 보존됐음을 해시로 확인했습니다. 기존 감사 코드에서 이번 작업으로 변경한 파일은 `utils/integration_audit.py`의 출처 페이지 제외 한 곳입니다. 그 밖의 수정은 신규 실행기·전송 경계·테스트와 안내 문서입니다.

현재 서울 24건의 고정 응답은 **연결 검증용 합성 응답**입니다. 원문 파서와 검토에서 확인한 값을 사용하므로 모델 판독 정확도 향상이나 실제 24건 복구 성공의 증거가 아닙니다. 실제 성능은 `--execute` 결과에서 재판독 성공률, 신규 셀, 보류·충돌, 경과 시간과 호출 수를 확인한 후 별도로 평가해야 합니다.
