# main 기반 V9 선별 통합 결과 — 2026-10-10

## 1. 사용 버전·작업 경계

- 저장소: `https://github.com/azureleo/2026_GovReport_Analyzer`
- main 기준: `baceb021820df6c5b3952ce89ee9804af5b7f138`
- V9 비교 기준: `a2cb6bc00da38ca59b44f64aa94de02999da7f15`
- 시작 시 원격 두 SHA를 조회했고 요청에 적힌 버전과 같았습니다.
- 별도 브랜치: `codex/main-v9-selective-integration-20261010`
- worktree: 기존 작업 폴더 아래 `tmp/main_v9_integration_20261010`
- HEAD는 기준 main입니다. 이 문서의 구현은 별도 worktree의 **미커밋 변경**이며 원격 main/V9에는 반영하지 않았습니다.

기존 `codex/perf-v8-fast-path` 개인 작업본의 변경은 보존했습니다. 전체 merge·cherry-pick·기존 파일 일괄 교체는 하지 않았습니다. main의 기존 함수를 확장하고 필요한 모듈만 추가했습니다. `.env`, 모델명, provider, reasoning, `utils/llm_client.py` 및 운영 호출 재시도 기본값은 변경하지 않았습니다. AGENTS.md는 해당 체크아웃·상위 경로에서 발견되지 않았습니다.

## 2. 기능별 판단·연결 상태

표의 기본값은 이번 통합본 기준입니다. 업무 반영, 진단 전용, 선택 기능을 구분합니다.

| 기능 | main → V9 확인 결과·판단 | 활성화·호출/소비 | 최종 확인 위치 |
|---|---|---|---|
| 문서 객체·물리 객체 | main에 없음, V9 추가. 기반 선별 이식 | `build_document_objects` → `consolidate_document_objects`; 기본 파싱 단계, 추가 모델 호출 없음 | 감사 `document_objects`, Excel16 객체 목록 |
| 객체 근거 매칭 | V9 추가. 보완 이식 | `audit_object_evidence` → `match_object_evidence`; 명시 ID/페이지/표 근거 검사, 매칭 결과로 임의 ID를 만들어 덮어쓰지 않음 | 감사 `object_evidence_audit` |
| 복수 헤더·표 청킹 | V9 추가. main 기본 입력 보존하며 이식 | `EXTRACTION_OBJECT_BATCHING_ENABLED=0`; ON이면 `render_table_object_chunks`/Extractor 객체 배치에서 헤더 반복·셀 분할 | 추출 입력, 업무 시트, `extraction_records` |
| 문자·셀 상한 | V9 추가. 선택 제한 이식 | `MAX_BATCH_CHARS=0`, `MAX_BATCH_TABLE_CELLS=0`는 제한 없음. 추출 배치 생성 시 적용; 분할 불가 단일 요소는 사유 기록 후 보류 | 감사 입력 크기·셀 수·제한 실패 |
| 03/04 축 키·공란율 | main 일반 정제 존재, V9 계약 확장. 보완 이식 | `_axis_contract`, 정제 후 `_key_blank_stats`; 원문 토큰 없는 추정 금지 | 업무 축, Excel17, 원행 주석·통계 |
| 05 전망범위·동일 행 키 | main에 범위 없음, V9 추가/수정. 전 경로 보완 | 헤더·프롬프트·정규화·dedup·Vision·semantic routing·RunState·두 scorer | Excel05 `전망범위`, 감사 불확실성·충돌, 평가 보고서 |
| 추출 원천·행 병합 | main에 객체 계보 없음, V9 추가. 충돌 보존으로 보완 | `_attach_sources`, `merge_extraction_rows`, Organizer 근거 병합; 출처 우선순위만으로 다른 값 삭제 금지 | 업무 출처 열, 감사 `merge_conflicts`·원행 주석 |
| 의미 계약 격리 | main에 원장 없음, V9 추가. 영속화까지 이식 | 기본 정제 탈락행 기록; 광범위 가드는 `SEMANTIC_CONTRACT_GUARDS_ENABLED=0` | 감사 `_semantic_contract_quarantine`/`semantic_contract_review`, Excel17 요약 |
| 목차·페이지 렌더 | main 키워드 페이지 렌더 존재, V9 객체별 조건 추가. 선별 이식 | ImageAgent 확정 목차 렌더 후보 제외; 혼합 표/embedded 이미지 페이지는 유지. 기존 페이지 문맥 렌더 유지 | `vision_execution.index_pages_excluded`, 렌더 변형 집계 |
| Vision 후보·배치 지표 | main triage 일부 존재, V9 확대. main 흐름에서 보완 | 기존 이미지 실행 시 후보·full/embedded 렌더·예정/완료/부분/실패 배치·로컬 경과 시간 | 감사 `vision_execution`, Excel17 실패 요약, run outcome |
| 실행 원장·체크포인트 | main에 없음, V9 추가. 선택 이식 | `RUN_STATE_ENABLED=0`; ON이면 `RunState.create` → 추출 배치 등록/복원 → finalize | `.cache/run_state/<fingerprint>/`, 항상 저장되는 outcome/감사 |
| 교차 백엔드·합의쌍 | main에 없음, V9 추가. 독립 도구 이식 | `cross_backend_probe.py`, `d30_agreed_pair_accuracy.py`; 저장된 Excel 사용, main 자동 실행 아님 | 지정 평가 디렉터리의 보고서 |
| 고정 채점·시각 fixture | V9 추가. config 결합·미확정 정답·크롭 무결성 보완 | `benchmark_contract_schema.py`, `benchmark_golden_score.py`, `score_against_golden.py`, `build_visual_regression_fixture.py` | 골든 SHA/계약 버전 평가 JSON·문서, fixture manifest |
| Directory 선택 OCR → 본문 | main에 없음. V9는 로더/객체 병합에서 업무 추출 연결 누락. 연결 구현 | `SELECTIVE_OCR_ENABLED=0`; ON이면 loader → 대응/병합 → 변경 객체만 관련 시트 `extract_objects` → 정제 | 업무 Excel, OCR 원장, 객체 목록, run outcome |
| UnlimitedOCR 실험 하네스 | main에 없음, V9 독립 실험. 선별 이식·보완 | `졸업프로젝트_실험1/experiment.py`; 저장 raw 또는 명시 서버 호출. 본체 필수 의존성 아님 | 엔진별 objects/DocumentObject JSONL, 평가 JSON/MD/XLSX |
| 최종 LLM 검수·원문 진단 | main 최종 LLM은 요약 평가만 수행. 그 역할 유지 | Excel 저장 후 LLM 진단, 자동 재수정 없음. `source_validation`은 원문 페이지/숫자 존재 검사일 뿐 사실 정확도 판정 아님 | 로그·run outcome, 감사 `source_validation` |

객체 목록 추가·행 수 증가·내부 품질 점수 상승은 정확도 향상 근거가 아닙니다. 기본 raw 텍스트 배치의 `batch-*` 근거는 배치 출처이며 개별 native 표와의 정확한 대응을 보장하지 않습니다. 명시 연결이 없으면 인벤토리의 본문 연결을 추정해서 늘리지 않습니다.

### 별도 판단·미이식 항목

| 항목 | 직접 확인한 상태·결정 |
|---|---|
| reading 스키마 / text_optimization / 전용 Vision 검수 예산 | 두 기준 커밋에 없음. 개인 작업본에 있던 기능을 main 기능으로 가정하지 않았음 |
| V9 LLM cache/singleflight·quota 대기·capacity/model fallback | V9 추가 기능. 이번에는 main 호출기를 그대로 유지하고 선택 체크포인트만 이식. 모델 fallback·장시간 quota 대기를 통째로 도입하지 않음 |
| GapFillAgent | main 파일/설정은 구 5분류용이고 Supervisor 미호출. V9는 새 도메인 경로로 연결됨. 이번 12개 핵심 후보와 분리하여 보류 |
| 참조자료 CSV 보강 | main에 없음, V9 부록3·4 매칭 추가. 별도 참조 데이터/실측 없이 기본 활성화하지 않음 |
| targeted/closed-loop 보조검수 | main에 없음. V9는 기본 비활성이고 targeted 분기는 후보 전용이며 판정 설정이 있어도 자동병합으로 연결되지 않는 사례 확인. 이번에 이식하지 않음; 채택 시 해당 연결부터 수정/검증 필요 |
| 객체별 OCR context crop/VLM fallback | 관련 V9 준비 함수는 로더 모듈에 존재하지만 Directory 경로가 VLM을 자동 실행하지 않음. 본체 기본 Vision은 기존 페이지 렌더/embedded 흐름 유지. 독립 실험 기능과 업무 연결 완료를 혼동하지 않음 |

따라서 이번 결과는 V9 전체와 기능적으로 동일한 통합본이 아닙니다. main에 있던 정상 경로의 손실과 V9 추가 기능 미채택을 구분했습니다.

## 3. 재현한 문제와 수정 흐름

### 저장 OCR의 실제 업무 반영

native 추출 **뒤의 변경 객체 선택 재추출**을 선택했습니다. 추출 전에 OCR 전체를 병합하는 방식은 native 기본 입력·라우팅·체크포인트를 모두 바꾸고 OCR 지연을 모든 시트에 전파합니다. 변경 객체 방식은 기존 main 흐름을 유지하고 필요한 시트만 추가 호출할 수 있습니다.

```text
원본 파싱/업무 추출
  → DirectoryOCRBackend.load(document, triage)
  → 문서·페이지·좌표 검증 + 개별 객체 대응
  → native 알려진 값 보존 / 빈 셀의 안전한 보완 / 충돌 보류
  → select_supplement_objects: 변경된 객체만
  → ExtractorAgent.extract_objects: 근거가 있는 관련 시트만
  → merge_extraction_rows: 동일 사실 출처 합침, 다른 값은 양쪽 보존
  → Organizer + 선택적 의미 검증
  → 실제 ExcelAgent.write + 감사 JSON + 실행 결과 JSON
```

Supervisor가 `organizer.get_excel_ready()`의 이전 정제본 대신 **최종 병합·검증된 final_data**를 Excel에 전달합니다. 로드·읽기·업무 추출·본문 반영을 별도 집계합니다. `triage.selection_status`와 실제 `final_status`도 분리했습니다. 저장 OCR의 판독 성공은 읽을 수 있는 보완 객체가 생성됐다는 뜻이며, OCR 모델의 사실 정확도를 검증했다는 뜻이 아닙니다.

- 후보 없으면 파일을 읽지 않음. 페이지 단위 파일 읽기와 객체 단위 적용 분리.
- 깨진 JSONL, 문서 해시 불일치, 페이지/좌표 오류, 대응 모호성, 결과 누락을 진단·검토 객체로 보존.
- 검증 metadata 없는 옛 입력은 경고/신뢰 수준으로 관리하며 위치·대응 근거까지 없으면 기본 보류. 전체 거부 정책 아님.
- hybrid export의 native 엔진을 UnlimitedOCR로 바꾸지 않음. 엔진 목록·파일/줄·객체 별칭·근거ID·페이지·좌표를 행 계보와 감사에 보존.
- 알려진 native 셀과 충돌하는 표는 자동 덮어쓰기하지 않음. 한 표의 대응·헤더·기존 셀이 맞고 빈 셀만 채우는 경우 안전하게 보완.
- 부분 응답의 정상 행은 보존하되 전체 성공으로 캐시하지 않음. 재시도 중 이미 성공한 OCR 객체 응답은 실행 내에서 재사용. 다른 실행은 체크포인트 fingerprint로 검증.

### 전망·축·시각 연결

범위·시나리오·세부부문·단위가 다르면 같은 수치여도 별개입니다. 빈 범위는 단일 호환 후보일 때만 흡수하며 가정·방법 설명의 충돌도 보류합니다. 입력 순서에 따른 결과 차이를 검사했습니다. `관리권한 외/없음/제외`를 관리권한으로 확정하지 않습니다.

03/04의 유효 축은 자동 이동하지 않습니다. 축이 비었거나 잘못됐다는 사실만으로 05나 BAU로 바꾸지 않습니다. 미래 연도와 명시 전망 시나리오의 근거가 있을 때만 이동하고, 그 뒤 출처 시트의 지역전체/관리권한 범위를 부여합니다. 과거 실적·목표·무효 원축은 보류합니다.

Vision에서 명시 목표를 상위 전망 분류보다 우선하고, 관리권한의 04 축을 보존했습니다. 혼합 BAU·정책반영 제목은 개별 행의 BAU 근거로 쓰지 않습니다. legacy ghg_data도 현황/전망/목표를 구분합니다. 배치 일부 image_index 누락·중복·오류와 후속 timeout이 있으면 정상 판독을 보존하고 미완료 상태를 전달합니다.

### 영속 원장·최종 검증 시트

`persist_audit`는 run-state OFF에서도 격리 원행·충돌 원행·범위/축 주석·내부 문맥 원장을 저장합니다. 감사 실패는 outcome의 경고에 남고 Excel 저장 성공은 유지합니다. 검증 결과의 `대상시트키/문제내용/권장조치`가 Excel17의 `대상시트/사유`로 변환되지 않아 설명이 비던 추가 연결 문제도 수정했습니다.

## 4. 설정·스키마·의존성

- 기본 비활성: 객체 배치, 선택 OCR, 자동 의미 이동, 광범위 의미 가드, run-state/resume. 원천 출처 열은 기본 ON.
- CLI: `--selective-ocr --ocr-results-dir`, `--object-batching`, `--batch-max-chars`, `--batch-max-table-cells`, `--semantic-routing audit|apply`, `--run-state-dir`, `--resume`.
- OCR 결과 디렉터리만 지정해도 자동 ON되지 않음. `--no-images`는 기존 Vision만 끄며 선택 OCR ON 설정까지 끄는 옵션은 아님.
- `OCR_OBJECT_MATCH_MIN_IOU`, `OCR_ALLOW_LEGACY_UNLOCATED`, `OCR_REPLACE_MIN_CELLS`, `OCR_REPLACE_CELL_RATIO`, `OCR_NATIVE_CONFIDENCE_THRESHOLD`는 설정 가능. 현재 기본값을 검증된 최적값으로 주장하지 않음.
- 기존 업무 헤더를 삭제하지 않음. 05 `전망범위` 추가, 업무 출처 열 추가, Excel16 상태 열 추가, Excel17 검증리포트 추가. 현재 총 18시트.
- `PROVENANCE_ENABLED=0`은 부가 출처 열을 숨길 뿐 감사 원장을 끄지 않음. 큰 원행/계보의 완전한 기록은 JSON이 기준; Excel 문자열 한도 적용.
- 본체 requirements의 모델/GPU 의존성은 추가하지 않음. 기존 PyMuPDF/openpyxl/Pillow/python-dotenv 사용. 테스트에 pytest 필요. 서버 호출 하네스는 별도 requirements의 requests 사용.
- 기존 `config.py`의 provider 기본값은 Gemini지만 main CLI의 기본 안내/검사 문구는 Codex로 표기되는 불일치가 있음. 모델 설정 변경 금지 범위에 따라 실제 provider 기본값을 바꾸지 않았고 문서에 이를 명시. 실제 실행에서는 기존 사용자의 provider 환경설정/CLI 선택을 유지하고 확인해야 함.
- 기존 `.env`를 복사·수정하지 않음. 새 `.env.example`은 새 기능 설명만 포함. 중첩 worktree 실제 실행에서는 상위 `.env` 탐색에 유의.
- 실행 fingerprint는 PDF·OCR 디렉터리 내용·프롬프트·구현·배치 설정·실행 정보 해시를 포함. 코드가 바뀐 옛 실행을 무조건 복원하지 않음. 필드가 없는 옛 행의 정규화/채점 호환과 실행 캐시의 안전한 재사용은 별개.

## 5. 직접 실행한 검증

### 새 기능 통합·회귀

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
py -m pytest tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py 졸업프로젝트_실험1/tests -q --basetemp .pytest_local_check
```

외부 모델 경계만 fixture/mock으로 대체했습니다. 실제 PDF/회전 표·객체 생성·정제·의미 라우팅·충돌 병합·RunState·Excel 작성/재개방·두 채점기·하네스 평가를 실행했습니다. main CLI도 새 프로세스에서 옵션을 읽어 실제 업무 Excel까지 연결하는 테스트가 포함됩니다. 실행 수·시간은 아래 최종 기록을 참고하세요.

최종 직접 실행: **144 passed in 5.56s**. 기반27 + 본체 end-to-end15 + 전망/평가61 + OCR 로더/하네스41입니다. 확정 목차 제외/혼합 데이터 페이지 유지, 부분 Vision 응답 보존도 포함합니다. `main.py --help`와 `git diff --check`는 통과했고, AST 비교에서 기존 모델/provider/timeout/CLI 관련 설정7개가 변경되지 않았으며 main LLM 호출 파일도 동일함을 확인했습니다.

이 환경에서 실제 사용한 실행 준비는 다음과 같습니다. 이 PATH는 프로젝트 설치본이 아닌 기존의 확인된 테스트 의존성 디렉터리이므로 다른 환경에는 그대로 복사하지 마세요.

```powershell
$projectRoot = "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
Set-Location -LiteralPath "$projectRoot\tmp\main_v9_integration_20261010"
$testPython = "$projectRoot\졸업프로젝트_실험1\.venv\Scripts\python.exe"
$env:PYTHONPATH = "$projectRoot\tmp\pipeline_connection_deps"
$env:PYTHON_DOTENV_DISABLED = "1"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:TEMP = "$projectRoot\tmp\pipeline_connection_temp"
$env:TMP = $env:TEMP
& $testPython -m pytest tests/test_extraction_foundation.py tests/test_main_v9_end_to_end.py tests/test_main_v9_forecast_contract.py tests/test_v9_ocr_loader_contract.py 졸업프로젝트_실험1/tests -q --basetemp .pytest_final_delivery_02 --junitxml=integration_tests_final.xml
```

시험 범위: 범위/시나리오 분리, 빈 범위 모호성·입력 순열, 축 부정값, 목표/현황/전망 구분, 다중 헤더·HTML spans, 문자/셀 제한, 같은 페이지 다른 표, 후보 없음, 손상 JSONL·결과 누락, 문서/좌표/회전, hybrid native 출처, 멱등성·부분 체크포인트·변경 OCR fingerprint, 실제 본문 셀, run-state OFF 감사, 감사 실패 Excel 보존, 구/신 골든 양방향 호환·범위 충돌 우회 차단, fixture 크롭 변조 탐지.

### 기준 main의 이미 존재하던 실패

이식 전에 기준 main에서 전체 tests 수집을 실행했으나 `test_supervisor_summary_context.py`가 존재하지 않는 `_apply_context_summary_evidence`를 import해 수집 실패했습니다. 해당 파일을 제외한 기존 8개는 **4 passed / 4 failed**였습니다. 통합본 전체 tests 실행에도 같은 네 실패가 남아 있습니다.

최종 통합본 실행은 `pytest tests --ignore=tests/test_supervisor_summary_context.py -q --tb=short --basetemp .pytest_main_all_final --junitxml=integration_tests_all.xml` → **124 passed / 4 failed (8.92s)**입니다. 전체가 녹색이라고 보고하지 않습니다. 서로 겹치는 실행이므로 이 수를144개에 더해 총 테스트 수로 표현하지 않습니다.

- 세 실패: fake CLI 명령이 공백/한글 경로의 Python 실행 경로를 따옴표 없이 조합하여 CLI 탐색 실패. 실제 유료 CLI를 호출한 결과가 아님.
- 한 실패: legacy `energy` 기대값과 현재 16시트 내부 키 `regional_conditions`의 불일치.
- 수집 실패 파일은 구 5분류 summary 계약을 기대하며 main 코드와 이미 불일치. 임의 삭제하거나 기대값을 바꿔 성공으로 처리하지 않음.

이번 검증 환경의 Python launcher 경고도 그대로 기록했습니다. 사용한 venv는 실제 테스트를 실행했지만 시스템 Python 경로 경고가 있어 `py`가 아니라 확인된 Python 실행 파일을 사용했습니다. 사용자 재실행에서는 프로젝트 의존성이 설치된 정상 Python을 선택해야 합니다. sandbox 임시 폴더 제한 때문에 worktree 내부 `--basetemp`와 workspace TEMP/TMP를 사용했습니다.

### 동일 fixture A/B — 연결 결과, 정확도 실측 아님

`scripts/offline_connection_demo.py`는 동일 생성 PDF·동일 저장 OCR·동일 고정 모델 응답을 사용하고 OCR 연결만 OFF/ON으로 비교합니다. OFF의05는 비어 있고, ON에서는 **987.6 → Excel05 전망값 셀**이 저장됩니다. 출처·범위·엔진·OCR 여부·계보와 감사 원장도 읽어 확인했습니다. 외부 모델 경계는18회 대19회이며 추가1회는 선택 객체의05 재추출입니다. 실제 원격 호출은0회이며 토큰/추론 시간은 미측정입니다. 약0.15초 내외의 두 실행은 해당 컴퓨터의 mock 로컬 연결 시간일 뿐 운영 속도가 아닙니다.

`scripts/run_ocr_fixture_demo.py`는 독립 하네스 동일 PDF/동일 합성 골든의 native·저장OCR·hybrid 비교입니다. 모든 객체 리콜/정밀도1.0, 중복0; 전체 정답 셀 복원율은 native0.625, 저장OCR/hybrid1.0입니다. 저장OCR은 손작성 정답 fixture이므로 모델 우월성이나 실제 OCR 정확도로 해석할 수 없습니다. PDF·골든 SHA와 지표의 반복 일치를 확인했습니다. 비정상 좌표/rowspan 객체2개 거절, 표본 밖 페이지·OCR 누락 진단도 저장했습니다. 서버 시간/VRAM은 null입니다.

생성 산출물은 버전 관리에서 제외된 `output/offline_connection_demo_20261010_final/`, `output/ocr_fixture_demo_20261010/`에 있습니다. 기존 파일을 덮어쓰지 않도록 새 출력 이름을 사용하세요. 본체 demo는 존재하는 출력 폴더를 거절합니다.

## 6. 결과 파일 위치와 후속 검증

- `<출력명>.xlsx`: 업무 16시트, 객체목록16, 검증17.
- `<출력명>_pipeline_audit.json`: 입력/Excel 연결, 격리 원행, 충돌, 출처 계보, 추출/OCR/Vision 실패, 키 통계, 원문 존재 진단.
- `<출력명>_run_outcome.json`: Excel/감사 저장 여부·실제 경로·경고·미완료 배치·OCR 단계별 집계.
- `.cache/run_state/<fingerprint>/`: ON일 때만 manifest/배치 checkpoint/행 원장. 감사 파일과 다른 목적.
- 하네스 `run/outputs/<engine>/`: 객체 JSONL 및 본체 가져오기용 DocumentObject JSONL. `run/reports/`: 평가 JSON/MD/XLSX.

미검증: 실제 UnlimitedOCR 서버/GPU 동작, 실제 모델 정확도/속도/토큰, 서울 전체 PDF의 실측, 사람 검수된 확정 전망범위 골든 기반 성능. 이번 작업에서는 서버/유료 호출을 하지 않았고 사람 확인 대기 서울 골든을 복사·수정·확정하지 않았습니다.

권장 후속 순서: 사람 확인된 **같은 범위·같은 골든 버전**의 소표본 확정 → 원래 모델/provider/reasoning 유지한 OCR OFF/ON 실측 → 셀 정확도·행 정밀도·리콜·충돌/중복·추가 호출·총 시간을 함께 비교 → 전체 PDF 회귀 → 이후 별도 승인 범위에서 cache/quota/gap-fill/보조검수/참조자료 기능 선택. 행/객체 증가나 서로 다른 골든 점수만으로 성능 향상을 주장하지 않습니다.
