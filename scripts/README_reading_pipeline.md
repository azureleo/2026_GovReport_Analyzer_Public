# 기존 main.py의 A/B 판독 연결 통합

## 2026-09-20 전체 실행 회귀 보완

- 단일 연간 값은 `연도=기간시작=기간종료`인 유효 정수 연도일 때 기간 합계 표시 없이도 통과한다. 다년 기간, 연도 누락/충돌, 누적·합계 문맥은 이 예외를 사용하지 않는다. 기존 기간 합계 조건은 유지한다.
- 점검실적의 복합 서술형 수치는 사업명·점검연도·실적 문맥·수치가 있는 근거/이행실적을 확인하고 `정보유형=사업실적` 및 원문을 보존한다. 한 숫자/달성률을 추정하지 않으며 분야별·전체 집계를 개별 사업으로 전환하지 않는다. 점검실적 envelope의 `문맥`/`문맥 구분`, `근거문구`/`근거 문구`는 명시 별칭으로 처리하고 서로 다르면 보류한다.
- 지역 별칭은 `서울`, `서울시`, `서울특별시`의 정확한 동치만 허용한다. 수도권·서울 외곽·다지역 표기는 같은 지역으로 간주하지 않는다. 원문은 감사 기록에 남는다.

저장된 `*_visual_merge_input.json.gz`를 기존 Organizer 및 Excel 작성기로 재실행할 수 있다. 아래 명령은 모델을 부르지 않는다. PDF는 결정론적 시트 분류/원문 검증을 위해 로컬 파싱하며, API·CLI 호출은 차단한다. 출력 폴더가 이미 있으면 중단한다.

```powershell
$runTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/replay_reading_pipeline.py `
  ".\output\서울_luna_medium_full_20260920_135538_visual_merge_input.json.gz" `
  --source ".\서울특별시_탄소중립계획.pdf" `
  --output-dir ".\output\reading_replay_$runTag"
```

산출물: `reading_replayed.xlsx`, `reading_pipeline.json`, `prepared_data.json`, `replay_summary.json`.
이는 새로운 PDF 전체 모델 실행이나 당시 Supervisor 실행 기록의 완전 복제가 아니다. 같은 저장 입력과 같은 로컬 설정으로 후단 변경 효과를 비교한다. 골든 평가에서는 확장 열/레거시 계약 호환성을 별도로 확인해야 한다.

빠른 회귀: `py -m pytest tests/test_reading_full_run_regressions.py tests/test_reading_pipeline_integration.py tests/test_reading_financial_period.py tests/test_reading_context_facts.py -q`

2026-09-18. 별도의 PDF 실행기를 만들지 않고 기존 `main.py → Supervisor → Organizer → Excel` 경로에 연결했다. 외부 Astra 채팅의 `raw_reading.json`을 다시 판독하거나 기존 산출물을 변경하지 않는다.

## 적용 내용

- `utils/reading_pipeline.py`: 기존 시트별 행과 선택적인 `_reading` 문맥을 A 형식 변환 및 B 의미 연결의 입력으로 연결한다. 의미 규칙을 별도로 복제하지 않고 `convert`, `bind_candidates`, `apply_contract`를 호출한다.
- 문맥 없는 기존 행: 동일 배율·동일 의미의 단위 별칭, 재원 미표기, 명시 기간 합계만 처리한다. 기존 행 전체에 실험용 필수조건을 강제하지 않는다.
- `_reading`이 있는 행: 명시 대상 시트, 역할별 메타데이터, 근거·적용범위가 있는 객체 지역, 계획 구분, 정성/조직/성과집계/감축원단위를 기존 B 규칙으로 검사한다. 지역·연도·금액을 문서명/제목만으로 채우지 않는다. 측정값 충돌과 부적합 입력은 원행을 감사 기록에 남기고 업무 행에서는 보류한다.
- 텍스트 단일/묶음 추출의 기존 호출에 필요한 문맥 필드를 안내한다. 관련 없는 문서 메타 시트에는 안내를 추가하지 않는다. 성공한 문맥은 감사 기록에 보존하고, 폐루프 후 전체 정리에서 재해석하지 않는다.
- Vision은 기존 지원 대상 시트의 `fields._reading`을 정리 단계에 전달한다. 명시 문맥이 있으면 기존 제목/지역 추정이나 키워드 재분류보다 해당 계약을 우선한다. 기간 합계는 B에서 연도 조건을 검사한다. 일반 연간 예산에 기간합계 필드를 필수로 요구하지 않는다.
- 기존 실험용 `verify_reading_excel_a.py`는 이미 A/B를 통과한 입력을 검사하므로 이 통합 스위치를 자체 프로세스에서 끈다. 기존 A/B 실험 명령은 그대로 사용한다.

## 유지한 제한

- 새 모델 호출 단계는 없다. 다만 기존 호출의 입력/출력 문맥이 늘 수 있고, 보류가 늘면 기존 gap-fill 정책이 반응할 수 있으므로 전체 시간·호출 수가 동일하다고 보장하지 않는다. 모델·재시도·동시 실행 설정과 `.env`는 바꾸지 않았다.
- Vision의 근거 ID, 신뢰도, 숫자 검증, 참고자료/축 추정 차단 정책을 유지한다. 2026-09-19부터 감축사업의 명시 `_reading` 식별과 `governance_feedback`의 조직 구성·기능을 추가 지원한다. 구조 자료의 일괄 승인이 아니라 정확한 객체와 명시 문맥 계약을 충족한 조직 행만 연결한다. 원단위 등 나머지 Vision 대상 확대는 하지 않았다.
- 근거 문맥이 없는 과거 출력만으로 새 지역·계획 구분을 소급 생성하지 않는다. 실제 모델이 올바른 문맥을 반환하는지는 새 PDF 실행과 원문 평가가 필요하다.
- 이 연결 통과는 PDF 사실 정확도나 최종 셀 정확도 점수가 아니다. 전체 PDF의 시간 단축이나 정확도 향상은 아직 측정하지 않았다.

## 빠른 연결 검증 — 모델 호출 없음

프로젝트 루트의 VSCode PowerShell에서 실행한다. pytest 등은 기존 requirements에 포함되어 있다.

```powershell
py -m pytest tests/test_reading_pipeline_integration.py tests/test_visual_reading_context.py -q
```

추출 응답/PDF 읽기를 고정한 테스트다. 실제 `main.main()`과 `Supervisor.run()`, 정리 코드 및 Excel 작성을 실행하고 저장한 셀을 다시 읽는다. 예산 계획 구분·성과집계 0건·기관 인원·감축원단위, Vision 기간 합계, 근거/신뢰도 차단, 충돌 보류, 프롬프트/체크포인트 구분을 검사한다. 실제 API/CLI 호출은 금지한 대체 함수로 검출한다.

## 기존 PDF 실행

기존 `py main.py ...` 명령을 그대로 사용한다. `READING_PIPELINE_ENABLED`의 기본값은 `1`이며 새 실행기나 Astra 전용 명령은 필요 없다. 다른 모델/캐시/재시도 조건은 사용자가 선택한 기존 조건을 유지한다.

명시적으로 켜려면 같은 터미널에서 실행 전에:

```powershell
$env:READING_PIPELINE_ENABLED = "1"
```

기존 경로와 비교할 때는 별도 출력 이름을 사용하고 `"0"`으로 끈다. 환경변수는 현재 터미널과 자식 프로세스에만 적용되며 `.env` 수정은 필요 없다. 껐을 때도 이미 추가된 Excel 열과 기존 Organizer 수정은 유지된다. 이 스위치는 전체 과거 버전으로 되돌리는 스위치가 아니다.

시작 화면에는 `A/B 판독 연결 규칙: 활성`이 표시된다. 종료 시 Excel 옆에 `<출력파일명>_reading_pipeline.json`이 생성된다. `enabled`, `applied_rows`, `held_rows`, `records`를 확인한다. `records`는 원행·변경·보류 사유를 담는다. 적용 건수는 연결 단계 감사 항목 수이며, 재정리/시각 병합의 후속 차단이나 중복 제거가 있으므로 최종 저장 행 수와 같다고 해석하지 않는다. `accuracy_evaluated`는 `false`다.

`visual_context_records`에는 감축사업 식별·조직 문맥의 투영 전 원문, 변경 사유,
최종 병합 상태를 담는다. 객체 지역 근거는 복구 전용 플래그 없이 지원하지만,
파일명·제목으로 지역을 채우거나 미지원 오류만 없애기 위해 계약을 생략하지 않는다.
새 `utils/visual_reading_context.py`도 배포 시 포함해야 한다.

프롬프트와 연결 설정은 체크포인트 식별에 반영되며 규칙 JSON도 실행 구현 해시에 포함된다. 이번 변경 전 체크포인트가 새 실행에서 자동 재사용된다고 기대하지 않는다. 기존 실행 중인 프로세스에는 이 수정이 소급 반영되지 않는다.

## 배포 파일

통합 모듈과 수정한 `main.py`, `config.py`, `agents/{extractor,image,organizer}_agent.py`, `agents/supervisor.py`, `utils/run_state.py`가 필요하다. 기존 실험 모듈 `utils/reading_{compatibility,adapter_v1,position,semantic_binding,financial_period,context_facts}.py` 및 `data/reading_mapping_rules_v1.json`도 함께 유지해야 한다. 테스트와 이 안내문도 함께 버전 관리하는 것을 권장한다.

## 구현 시 확인한 결과

- 신규 통합 테스트 37개 통과. 관련 기존 테스트를 합쳐 258개 및 unittest 하위 검증 15개 통과.
- 테스트 실행기는 별도 번들 Python 3.12와 `tmp/reading_integration_test_deps`의 임시 의존성을 사용했다. 사용자 Python/가상환경/requirements/`.env`는 변경하지 않았다.
- 고정 응답으로 실제 Excel을 작성·재열람한 연결 회귀검증이며, 전체 PDF 실행/실제 모델 정확도/추가 토큰/총 실행시간 측정은 아니다.
