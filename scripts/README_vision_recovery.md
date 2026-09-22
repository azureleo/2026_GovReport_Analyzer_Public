# 저장 Vision 결과의 복구 및 응답 재사용 실험

## 범위

`run_vision_recovery.py`는 기존 `main.py`를 재실행하지 않는다. 같은 원본
스냅샷으로 수정 전/후 `OrganizerAgent.organize`와 의미 검증 및
`ExcelAgent.write`를 실행하고, 저장한 업무 시트의 실제 셀을 다시 읽는다.
기존 Excel 작성기를 그대로 사용한다. 전체 PDF 파이프라인과는 다른
제한된 후단 검증이며 텍스트 추출, gap-fill, 외부 참고 CSV 조회는 실행하지 않는다.

현재 설정은 `테스트파일_2.pdf`의 11쪽 Luna/Astra 실험과 앞서 만든 464개
평가 사실에 고정되어 있다. 설정의 상대 경로는 현재 터미널 위치가 아니라
프로젝트 루트 기준이다. 기존 `outputs/vision_gold_20260919_103443`의 평가기,
`gold_spec.py`, `evaluation_data.json`이 필요하다. 임의 PDF용 범용 채점기는 아니다.
골든셋은 독립적인 사람의 최종 검수 전 초안이며 개발 표본이다.

## 구현된 후단 보완

명시 객체 지역·감축사업 식별·조직 정보 연결은 `READING_PIPELINE_ENABLED=True`인
일반 main.py와 복구 실행기에서 공통으로 적용한다. `.env` 변경은 필요 없다.
복구 실험의 기존 `VISION_RECOVERY_BINDINGS_ENABLED`는 남아 있지만 새 연결 지원은
그 플래그를 켜야만 작동하는 전용 기능이 아니다.

1. 배출전망의 빈 세부부문을 하위 부문과 일치시키지 않는다. 합계와 세부 행을 구분한다.
2. 문서 지역이 미확정일 때 `fields._reading.객체문맥`에 자료지역·지역근거·적용범위가
   모두 있는 해당 행만 지역 계약에 연결한다. 파일명/문서 제목으로 전체 행을 채우지 않는다.
3. 명시적 사업명/관리번호 없는 범례·구조 설명을 사업으로 병합하지 않는다.
   원래 판독값은 스냅샷과 시각자료목록에 보존한다.

누락된 기간/지역의 추론과 정답값으로 판독값 교정은 구현 범위에 포함하지 않는다.
기존의 근거·신뢰도·단위·참고자료·충돌 차단 정책도 유지한다.

## 명시 문맥 지원 확대 (2026-09-19)

- 감축사업 `_reading`을 숫자 측정 계약이 아닌 명시 사업 식별 계약으로 받는다.
  같은 행의 개별 사업 관리번호와 비수치 항목명은 사업명 후보로 투영할 수 있다.
  세대수·면적·성과 구간을 사업명 또는 가짜 측정값으로 만들지 않는다.
- Vision 대상에 `governance_feedback`을 추가한다. 기관구성은 인원/개수와 구성구분,
  조직기능은 원문 역할을 각각 보존한다. 과거 `other`는 명시 기관명/주체 관계 및
  구성·기능 필드가 있을 때만 이 계약의 후보가 된다. 위원회가 제목에만 나온다고
  연결하지 않는다. 새 최상위 숫자나 단위는 만들지 않는다.
- `utils/visual_reading_context.py`는 원문 복사본과 필드 매핑을 감사한다. 투영은
  승인이 아니며 후속 A/B 지역·필수 필드, 근거 객체, 신뢰도, 충돌 검증을 모두 거친다.
  조직기능의 null 수치나 조직 구성의 미기재 연도는 이 계약에서 필수가 아니지만,
  기존 근거 부족·참고자료·추정값 차단은 해제하지 않는다.
- 객체 지역 연결은 자료지역·지역근거·적용범위를 모두 요구하고 다른 지역/전국
  참고자료와 충돌하면 보류한다. 페이지나 제목으로 다른 행의 지역을 채우지 않는다.
- 일반 실행 감사 JSON의 `visual_context_records`, 재사용 결과의
  `visual_context_audit.json`에서 원문, 투영 사유, 최종 병합 상태를 확인한다.
- 기존 저장 응답에 지역 근거가 없다면 미지원 오류는 해소돼도 최종 행은 늘지
  않을 수 있다. 이 경우 보류는 정상 동작이며 프롬프트 개선의 효과는 별도 새 판독이 필요하다.

## 의미 분리·동일 사실 식별 보완

`utils/visual_fact_identity.py`의 공통 규칙을 운영 ImageAgent/Organizer와
저장 응답 복구의 식별 비교에 적용한다. `VISION_RECOVERY_BINDINGS_ENABLED`를
켜야만 작동하는 전용 규칙이 아니며 `.env` 변경은 필요 없다.

- 증가량·감축량·비율·원단위는 총량 `전망값`으로 저장하지 않는다.
- 소계·합계·머리글·담당부서는 개별 사업으로 만들지 않는다. 같은 객체의 하위
  관리번호로 확인한 상위 과제도 별도로 보류한다. 정상 사업명에 해당 단어가
  포함됐다는 이유만으로 차단하지 않는다.
- 명시 사업명 없는 측정항목을 사업명으로 만들지 않는다. 명시 사업명은 목록에
  보존할 수 있지만 세대수·면적·감축량 등 원래 관찰값은 각각 유지한다.
- 같은 사업 ID라도 측정항목·범례·축·달성도 구간이 다르면 별도 사실이다.
  같은 객체·관리번호의 사업명 자체가 다르면 자동 정정하지 않고 충돌로 보류한다.
- 같은 사실은 페이지·근거 객체·시나리오·부문/세부부문·기간·측정 역할·재원 등을
  함께 비교한다. 같은 수치만으로 중복 처리하지 않으며 작은 숫자 차이도 무시하지 않는다.
- `’24` 같은 기간 표기는 이미 명시된 `2024`와 일치할 때만 같은 연도로 비교한다.
  기간 합계와 연간 값, 총괄과 세부부문, 계와 시비는 합치지 않는다.
- 같은 객체·사업·기간에 명시된 재원 행이 있으면 재원 미연결 후보를 `미표기`로
  추가 저장하지 않는다. 실제로 재원 근거가 없는 자료의 미표기 처리는 유지한다.
- 분리·중복 판정은 `visual_semantic_audit.json`과 `backend_output.json`에 기록한다.
  판독 원문은 시각자료목록/응답 파일에 보존하며, 다른 업무 시트로 추측 이동하지 않는다.

단일/배치 Vision 공통 프롬프트에도 위 구분과 관리번호·측정항목 보존을 명시했다.
프롬프트 해시를 Vision 체크포인트 식별에 포함해 변경 전 성공 배치를 잘못 복원하지 않는다.
`reuse`는 이전 응답을 사용하므로 **코드 효과만 검증하며 새 프롬프트 효과는 측정하지 않는다.**
이름만 비슷하고 명시 ID/근거가 없는 사업명 오독은 자동 교정하지 않는다.

## 이미 성공한 재판독 응답으로 연결 수정 평가하기 (이번 실행)

`reuse`는 기존 `retry/call_*.json` 중 호출 기록이 `completed`인 응답만 사용한다.
이번 수정은 성공 응답의 근거 객체에 `final_status=extracted`, `ocr_status=completed`를
전달하지 않아 새 관찰값에 `객체 최종상태가 extracted 아님`이 남던 문제를 해결한다.
페이지·객체 ID·근거 ID·영역이 고정된 요청과 모두 일치하는 성공 응답만 재구성하며,
오래된 `병합차단사유` 문자열을 일괄 삭제하지 않는다. `extracted`는 응답 확보 상태이지
사실 정확도의 승인이 아니다. 실패/타임아웃 객체와 실제 의미·값·단위 충돌은 보류한다.

아래 블록 전체를 VSCode의 PowerShell에 붙여넣는다. `--replay-dir`에는 원래 실험의
최상위 폴더를 지정하며 끝에 `retry`를 붙이지 않는다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$sourceRun = ".\output\vision_recovery_cli_fixed_20260919_162228_443753"
$runTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
$reuseDir = ".\output\vision_response_reuse_$runTag"

py -X utf8 -u .\scripts\run_vision_recovery.py reuse --replay-dir "$sourceRun" --output-dir "$reuseDir"
$reuseExitCode = $LASTEXITCODE

Write-Host "종료 코드: $reuseExitCode"
Write-Host "결과 폴더: $reuseDir"
if ($reuseExitCode -eq 2) {
    Write-Warning "결과는 저장됐지만 검토 항목이 남았습니다. summary.md와 preservation.json을 확인하세요."
} elseif ($reuseExitCode -ne 0) {
    Write-Warning "실행 오류입니다. 터미널 오류와 생성된 경우 manifest.json을 확인하세요."
}
```

- **모델 호출 0회**. PDF 렌더링/재판독, `main.py`, CLI 실행 및 새 모델 추론은 하지 않는다.
  원본 PDF 등 입력 파일은 동일성 확인을 위해 해시만 읽는다. 네트워크와 자식 프로세스 실행은 차단한다.
- Codex CLI 절대 경로나 모델 환경변수를 다시 설정할 필요가 없으며 `.env`도 수정하지 않는다.
- 기존 응답과 실험 폴더는 읽기 전용이다. 출력은 새 형제 폴더에 저장한다.
- 예전 코드 해시와 현재 코드 해시 차이는 `manifest.json`에 기록한다. 수정 평가용인 `reuse`만
  코드 변경을 허용하며 원본 입력·실험 설정·고정 산출물의 해시 검증은 유지한다.
  예전 `retry` 응답에는 생성 당시 해시 봉인이 없었으므로 재사용 시작 시 해시를 기록하고
  종료 시 불변성을 확인한다. 이는 생성 이후의 모든 변경을 증명하는 보장은 아니다.
- 숫자가 같아도 단위/문맥이 달라지면 자동 교체하지 않는다. `merge_audit.json`의
  `difference_kind`로 `value_difference`와 `metadata_or_unit_difference`를 구분한다.
- 실패 3건은 새 응답 없이 복원할 수 없다. 남은 큐는 검토 목록일 뿐 자동 재호출하지 않는다.
  이 실험에서는 성공 응답 12개 재사용, 실패 3개 유지와 종료 코드 `2`가 예상된다.

주요 산출물:

- `summary.md`: 수정 전/후 동일 골든셋 평가와 보존 검사 요약.
- `Luna/result.xlsx`, `Astra/result.xlsx`: 기존 Organizer/Excel 작성기로 다시 저장한 결과.
- `Luna/cell_validation.json`, `Astra/cell_validation.json`: 실제 저장 셀 재확인.
- `preservation.json`: 기존 판독값, 기존 업무 행, 이미 확인한 정답 셀의 손실 여부.
- `response_audit.json`, `object_state_changes.json`: 재사용 응답과 정확히 갱신된 객체 상태.
- `Luna/visual_semantic_audit.json`, `Astra/visual_semantic_audit.json`: 잘못된 역할·중복의 분리 사유.
- `Luna/visual_context_audit.json`, `Astra/visual_context_audit.json`: 명시 문맥 투영과 후속 보류/병합 상태.
- `merge_audit.json`, `downstream_holds.json`, `remaining_queue.json`: 충돌과 미해결 항목.
- `manifest.json`: 모델 호출 0회, 실행 상태, 입력 불변성 및 코드 변경 기록.

이 단계에서 기존 폴더로 `retry`를 다시 실행하거나 `--allow-model-calls`를 붙이지 않는다.
아래의 두 단계 실행은 새 모델 호출 실험이 별도로 필요할 때만 사용한다.

의미 분리 이후에는 이전 업무 시트에 있던 오분류 행이 빠질 수 있다. 이 경우
`saved_workbook_cells`(셀 저장), `original_observations_preserved`(원문 판독 보존),
`confirmed_gold_preserved`(이미 확인된 정답 보존)는 통과해도
`business_rows_unchanged`와 종합 `saved_cells_and_preservation`은 false일 수 있다.
이를 자동 성공으로 무시하지 않고 종료 코드 2로 알린다. `preservation.json`의 변경 행을
모델별 `visual_semantic_audit.json` 및 원문과 함께 검토한다.

## 새 재판독 실험을 VSCode PowerShell에서 두 번 실행

첫 번째 블록을 한 번 붙여넣는다. 새 출력 폴더는 기존 결과와 겹치지 않는다.

```powershell
Set-Location -LiteralPath "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트"
$runTag = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
$replayDir = ".\output\vision_recovery_$runTag"
py .\scripts\run_vision_recovery.py replay --config .\data\vision_recovery_11pages.json --output-dir "$replayDir"
```

- 모델 호출 0회. 네트워크 및 자식 프로세스 실행을 감사 훅으로 차단한다.
- 원본 파일 해시 및 기존 평가와 입력의 일치 여부를 검사한다.
- 양 모델 각각 original, baseline(현재 고정 검증 설정), replay(보완 규칙 적용)를 비교한다.
- 셀 저장 실패, 재생 비결정성, 예상 밖 업무 행 손실, 확인된 정답 셀 손실이면 두 번째 실행을 차단한다.
- 명시적으로 분리한 비사업 범례/구조 행 제거는 예상된 변화로 기록한다.
- 재판독 목록에는 판독 누락/필드 오류만 포함한다. 정확히 판독됐지만 후단에서 막힌 것만으로는 재호출하지 않는다.

첫 명령의 종료 코드가 0이고 `선택 재판독 가능`이 출력되면 같은 터미널에서 두 번째 블록을 실행한다.

```powershell
py .\scripts\run_vision_recovery.py retry --replay-dir "$replayDir" --allow-model-calls
```

**두 번째 명령은 실제 Codex Vision 호출을 허용한다.** Luna/Astra 모두 Medium으로,
각 모델 자신의 누락·오류가 있는 객체만 호출한다. `.env`는 읽지 않으며 설정 JSON의
모델과 command가 기준이다. 기존 Codex CLI 로그인과 PATH 설정이 필요하다.

원하면 두 번째 명령 대신 아래 명령으로 호출 없이 목록/예산만 볼 수 있다.

```powershell
py .\scripts\run_vision_recovery.py retry --replay-dir "$replayDir" --dry-run
```

터미널을 새로 열었다면 `$replayDir`을 첫 실행 때 출력된 실제 폴더 경로로 다시 지정한다.
`python` 실행 별칭 대신 이전에 프로젝트 실행에 사용했던 `py`를 사용한다.

## 재판독 정책과 시간

- 기본 예산: 두 모델 합계 최대 20객체, 객체당 1회, 요청당 120초, 호출 단계 누적 1,800초.
- 실제 후보가 20개를 넘으면 일부를 조용히 생략하지 않고 첫 단계에서 차단한다.
- 재귀 분할, JSON 재요청, 대체 모델, quota 대기, 캐시 복원 없이 제한된 기존 Vision 호출을 사용한다.
- 이 표본은 페이지마다 물리 시각 객체 하나이다. 머리글·단위·각주를 잃지 않도록 페이지 문맥을 함께 렌더링한다.
- 실제 시각 객체가 여러 개인 페이지는 전면 이미지를 한 객체 근거로 귀속시키지 않고 별도 보류한다.
- 호출 프롬프트에는 골든셋 정답, 기대 숫자, 이전 오답을 넣지 않는다. 객체/페이지 선택만 평가 결과를 이용한다.
- 타임아웃/파싱 실패 시 기존 관찰값은 유지한다. 기존 식별자가 같은데 새 값/의미가 다르면 충돌 보류한다.
- 새 식별자의 관찰값만 추가하고 다시 같은 후단으로 전달한다. 기존 판독은 덮어쓰지 않는다.
- 코드, 규칙, 원본, 실험 설정, 재생 결과, 큐가 첫 실행 후 변경되면 새 replay를 요구한다.
- 같은 replay의 `retry` 폴더가 이미 있으면 다시 호출하지 않는다. 중단 후에도 자동 재시작하지 않는다.
  중단 기록과 저장된 부분 결과를 검토한 후 별도 재개 계획을 잡는다.
- 30분은 호출 단계 예산이며 로딩·Excel 작성까지 포함한 전체 완료 시간 보장은 아니다.

예산/모델/CLI 경로를 변경하려면 `data/vision_recovery_11pages.json`을 편집하고 **새 replay**부터 실행한다.
이전 설정과 시간 조건이 달라진 복구 결과를 최초 A/B 점수와 동일 조건의 모델 순위로 해석하면 안 된다.

## 출력 확인

첫 실행 폴더:

- `summary.md`, `summary.json`: 세 상태별 비교와 검증 조건.
- `Luna|Astra/baseline/result.xlsx`: 보완 규칙을 끈 재생 결과.
- `Luna|Astra/replay/result.xlsx`: 보완 규칙을 켠 재생 결과.
- `Luna|Astra/replay/cell_validation.json`: 실제 업무 셀 주소와 저장 값, 정제 데이터 일치 여부.
- `Luna|Astra/*_evaluation.json`: 동일 평가기로 비교한 골든셋 결과.
- `retry_queue.json`: 재판독 객체 목록. 수동 변경하면 해시 검사에서 차단된다.
- `downstream_holds.json`: 후단 보류 및 재판독 객체를 안전하게 지정할 수 없는 항목.
- `preservation.json`: 변경·제거된 업무 행과 기존 정답 셀 손실 여부.
- `manifest.json`: 원본·코드·설정·산출물 해시와 재판독 허용 여부.

두 번째 실행은 같은 폴더 아래 `retry/`에 결과를 저장한다:

- `Luna|Astra/result.xlsx`, `evaluation.json`, `cell_validation.json`.
- `call_journal.json`: 시작/실패/완료/예산 소진과 소요 시간.
- `call_*.json`: 객체별 파싱 응답 및 관찰값, 호출 통계.
- `merge_audit.json`: 추가·동일·충돌·범위 밖 관찰값 처리 기록.
- `*_combined_snapshot.json`: 기존 관찰값과 새로 추가된 관찰값. 부분 성공도 호출마다 저장한다.
- `remaining_queue.json`: 아직 해결되지 않은 판독 대상. 자동 반복 실행하지 않는다.

최종 업무 셀 채점은 이전 평가기가 지원하는 일부 식별자 범위에 한정된다.
미확인을 전부 실제 누락으로 단정하거나 저장 성공률을 사실 정확도로 해석하지 않는다.
전체 업무 시트의 저장 값 보존 검사는 별도로 전수 실행한다.

## 종료 코드

- `0`: 실행·검증 완료. replay의 경우 다음 선택 재판독이 가능하다는 뜻이며 정확도 100%가 아니다.
- `2`: 파일은 생성됐지만 검증 차이, 보류, 충돌, 미해결 판독 또는 예산 문제가 남음.
- `1`: 입력 불일치, 실행 오류, 기존 출력 폴더 존재 등으로 실행 불가.

## 회귀 테스트 (모델 호출 없음)

```powershell
py -m pytest tests/test_visual_reading_context.py tests/test_visual_fact_identity.py tests/test_vision_response_reuse.py tests/test_vision_recovery_workflow.py tests/test_reading_pipeline_integration.py tests/test_visual_merge_ab.py tests/test_vision_review_budget.py tests/test_evidence_merge.py -q -p no:cacheprovider
```

검증 모드가 사용하는 Python에는 기존 requirements 및 pytest가 설치되어 있어야 한다.
이 실행기는 사용자 Python, 패키지, `.env` 또는 로그인 상태를 변경하지 않는다.
