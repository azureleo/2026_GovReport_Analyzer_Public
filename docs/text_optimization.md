# 텍스트 입력 최적화와 독립 A/B

2026-09-20. 기존 `main.py`와 동일한 Extractor에 연결했다. 별도의 판독 모델이나 새 전체 실행기는 필요하지 않다. 원본 PDF·골든셋·기존 실행 산출물·`.env`는 수정하지 않았다.

## 적용 범위

| 기능 | 동작 |
|---|---|
| 입력 감사 | 시트·페이지·키워드 근거, 원문 해시, 중복 블록 좌표·텍스트를 기록 |
| 안전한 입력 정리 | 괘선 기반 HTML 표 안에 동일 토큰열이 있고 그 표 좌표 안에 포함된 유일한 본문 블록만 호출용 사본에서 제거 |
| 목차 배정 | 데이터 없는 확정 목차만 후보 제외. 남은 원문이 목차 제목/쪽번호 외 내용을 포함하면 유지 |
| 머리말 | 반복되는 상하단 블록을 기록만 함. 문서 빈도만으로 제외하지 않음 |
| 묶음 호출 | 기존 2~3시트 그룹을 사용하되 같은 페이지 배정 조합끼리만 묶음. 시트별 요청 페이지를 확장하지 않음 |
| 부분 복구 | 성공 시트/정상 빈 배열 보존. 누락 키·잘못된 배열은 실패. 실패 시트만 기본 1회 단독 복구하며 추가 분할은 금지 |
| 재개 | 복구 호출 전에 시트별 상태를 체크포인트에 저장. 중단 후 성공 시트를 재요청하지 않음 |
| 상위 실패 | RunState가 없어도 재귀 자식의 미해결 상태를 부모 `partial`에 전달 |
| 실행 감사 | 계획·실제 추출 진입 호출·지연·부모/자식·원시 병합 행 수를 `*_text_audit.json`에 저장 |

숫자 일부 일치(12와 120), 표 밖의 같은 표현, 반복 출현하는 본문, 단위·출처·각주·캡션·절 제목·표 헤더는 중복 삭제 근거가 아니다. text 정렬 전략으로 만들어진 가상 표도 제외한다. HTML 표 자체는 유지한다. 시트별 필드 명세·가이드라인·판독 규칙은 축약하지 않는다.

네이티브 표가 존재하는 목차는 실제 데이터가 섞였을 가능성이 있어 제외하지 않는다. 서울 파일의 표/그림 목차도 텍스트 정렬 기반 네이티브 표로 파싱되어 이 안전장치로 유지된다. Vision의 캡션 프록시 제외와 텍스트 페이지 전체 제외는 다른 계약이다.

최종 시트 행 수도 감사 파일에 저장하지만, Vision·정리·검증·병합이 개입하므로 특정 호출의 최종 채택 수나 정확도라고 해석하지 않는다. `calls`는 추출 함수 진입 수이고 내부 전송 재시도는 기존 `llm_call_stats`를 함께 봐야 한다. 부모/자식 경과 시간은 중첩되므로 합산하지 않는다.

## 기본값과 활성화

```dotenv
TEXT_ROUTING_MODE=audit
TEXT_INPUT_MODE=audit
TEXT_CLUSTER_MEMBER_RETRIES=1
EXTRACTION_SHEET_CLUSTERING=0
```

새 두 모드는 `off`, `audit`, `optimize`를 지원한다. 기본 audit는 후보 변경을 기록하지만 실제 호출 입력/배정은 그대로 둔다. 잘못된 모드는 오류로 중단한다. `.env`에 없는 새 키는 위 기본값을 사용한다. 묶음 기능은 기존 환경값을 존중하며, 이번 작업에서 자동으로 켜지 않았다. 실제 응답 품질 비교 전 기본값을 공격적으로 변경하지 않는다.

정책·그룹 구성은 실행 fingerprint에 포함되어 다른 설정의 체크포인트를 잘못 복원하지 않는다.

## 1. 모델 호출 없는 전체 사전 검증

프로젝트 폴더 PowerShell:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/preflight_text.py ".\서울특별시_탄소중립계획.pdf" `
  --golden ".\data\golden\서울특별시_골든셋_v1_초벌.xlsx" `
  --output ".\output\text_preflight_$stamp.json"
```

원문 무변경, audit/기준 큐 동일성, 본문 배정 보존, 묶음 시트-페이지 동일성 및 골든 원문 문구 후보 보존을 확인한다. 기본·audit·최적화·묶음 네 계획을 같은 PDF 파싱 결과에서 만든다. 계획의 root_tasks는 최초 작업 수이며 실제 재시도·지역명 추출 호출 수가 아니다. 문자 수는 토큰 수가 아니다.

`verify_routing_coverage.py`도 기본적으로 이번 정책을 검사한다. 기존 weak/strong 키워드 실험은 `--policy legacy-keywords`로 분리했다. 골든 문구가 없거나 PDF에서 찾지 못한 셀, 문서 메타 시트의 앵커 0개 등은 검증 범위 밖이다. 후보 보존 통과는 최종 셀 정확도 통과가 아니다.

## 2. 입력 최적화만 비교

두 군 모두 묶음을 끄고, A는 원래 입력·B는 안전한 입력 최적화를 사용한다. Vision, 하이브리드 검수, gap fill, 시트별 폐루프는 양쪽 모두 끈다. 최초 추출과 같은 결정론적 후단을 비교하는 실험이며, 전체 운용 구성의 최종 성능과는 다르다.

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/run_text_optimization_ab.py ".\output\pdf\서울_신규검증표본_6쪽_86_159_225_459_463_475.pdf" `
  --experiment inputs --agent codex --agent-model gpt-5.6-luna `
  --output-dir ".\output\text_input_ab_$stamp" --dry-run
```

`--dry-run`은 명령과 `contract.json`만 만들며 PDF 판독 호출은 하지 않는다. 실제 실행은 **새 `$stamp`와 새 출력 폴더**를 사용하고 `--dry-run`을 제거한다. 기존 폴더 재사용은 차단된다. 입력 PDF가 이미지뿐이면 텍스트 최적화의 실호출 표본으로 적합하지 않다. 텍스트·표가 있는 고정 표본을 사용한다.

## 3. 묶음 여부만 비교

입력 최적화 자체를 먼저 검토한 뒤 같은 입력 정책으로 묶음 여부만 바꾼다.

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/run_text_optimization_ab.py ".\output\pdf\서울_신규검증표본_6쪽_86_159_225_459_463_475.pdf" `
  --experiment clustering --routing-mode optimize --input-mode optimize `
  --agent codex --agent-model gpt-5.6-luna `
  --output-dir ".\output\text_cluster_ab_$stamp" --dry-run
```

입력 변경을 적용하지 않은 묶음 비교는 `--routing-mode audit --input-mode audit`를 쓴다. 위와 동일하게 실호출은 새 출력 폴더에서 `--dry-run`을 제거한다. 기본 `--retries 1`은 감독관의 전체 재실행 상한이며, 개별 CLI·JSON 복구 횟수와 다르다.

호환성을 위해 B군 파일명은 inputs 실험에서도 `clustered_sheets.xlsx`를 유지한다. 실제 의미는 `contract.json`의 experiment/arms를 확인한다.

## 4. 최종 정확도와 실행 조건

서울 전체와 같은 평가 범위를 실제 실행한다면 `--golden ".\data\golden\서울특별시_골든셋_v1_초벌.xlsx"`를 추가할 수 있다. 기존 공식 채점기로 두 군을 채점하여 셀 정확도·행 정밀도·행 재현율과 A에서 매칭됐지만 B에서 누락된 골든 행을 `summary.json`에 기록한다. 형식 오류나 분모 불일치가 있으면 비교 불가로 표시한다. 작은 발췌본에 전체 골든셋을 적용한 수치는 발췌본 정확도로 해석하면 안 된다. 범위가 일치하는 정답 표본이 필요하다.

- 모델·추론 설정·동시성·제한 시간·원문·코드·가이드라인을 고정한다. 실행 도중 코드를 수정하지 않는다.
- 두 군은 별도 run_state, resume/retry_failed_only=0, 캐시 off로 실행한다. `--use-cache`는 탐색용이며 공정한 속도 비교가 아니다.
- 실행 후 실제 manifest의 입력/코드/프롬프트/설정을 비교한다. 허용 변수 외 차이가 있으면 speedup을 산출하지 않고 종료 코드 2로 알린다.
- 품질 점수, 전체 행 수, 숫자 셀 수 또는 A/B 간 일치율만으로 정확도 향상이라고 판단하지 않는다.
- 골든의 기존 매칭 손실과 값 불일치 상세도 별도로 확인한다. 총점 상승이 개별 정답 손실을 상쇄한 것으로 처리하지 않는다.
- 장시간 전체 모델 실행은 이 구현 검증에서 수행하지 않았다. 소표본 실호출에서 정확도 저하가 없는지 확인한 뒤 전체 실행을 권장한다.

## 변경 파일

`utils/text_optimization.py`, `agents/extractor_agent.py`, `agents/supervisor.py`, `utils/run_state.py`, `config.py`, `scripts/preflight_text.py`, `scripts/verify_routing_coverage.py`, `scripts/run_text_optimization_ab.py`, `tests/test_text_optimization.py`.
