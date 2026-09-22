# 판독 형식 호환성 A단계

운영 main.py와 분리된 오프라인 실험입니다. 원시 판독, 기존 Organizer/Excel,
설정은 변경하지 않습니다. 필요한 패키지는 openpyxl입니다. .env는 읽지 않습니다.

```powershell
py scripts/run_reading_compatibility_a.py "경로/raw_reading.json" `
  --metadata "경로/metadata_links.json" `
  --schema "경로/backend_schema.json" `
  --record-ids data/reading_compatibility_smoke_ids.json `
  --output-dir output/reading_a_new_run
```

기존 출력 폴더는 덮어쓰지 않습니다. --record-ids 생략 시 전체 점검입니다.
지정해도 전체 관계/문맥을 보존하고 후보 대상만 제한합니다. 동봉된 ID 8개는
이번 개발 입력의 동작/회귀 검사이며 독립 정확도 평가 표본이 아닙니다.

문자열 문맥 원문 보존, 정확한 시트/상태 별칭, 열거된 동일 배율 단위 표기만
변환합니다. 숫자/부호 변경, 단위 환산, 메타데이터 의미 결합은 하지 않습니다.
고정 실험 adapter_v1의 normalize/make_candidates만 호출합니다.
명시 시트/분류와 다른 후보는 격리하며 분기 자체를 수정하지 않습니다.
동일 필드 메타데이터 경쟁은 보고만 하고 정답을 선택하지 않습니다.

산출물: compatible_reading_a.json, compatibility_report.json,
candidate_validation.json. JSON 경로별 before/after/규칙과 입력/코드 해시를 보존합니다.
accepted_candidates는 제한된 게이트를 통과한 후보이지 최종 승인 행이 아닙니다.
run_reading_compatibility_a.py는 Organizer/의미 guard/Excel을 실행하지 않습니다.
후단 셀 검증은 아래 별도 스크립트를 사용합니다.
원문 정확도를 채점하지 않으며 성공 종료도 의미 오류가 없다는 뜻이 아닙니다.

```powershell
py -m unittest tests.test_reading_compatibility
```

## 기존 후단 및 실제 Excel 저장 검증

```powershell
py scripts/verify_reading_excel_a.py --offline-dotenv-shim
```

기본 입력은 output/reading_compatibility_a_smoke_20260907/candidate_validation.json의
accepted_candidates입니다. 원시 8개 중 통과 후보만 사용합니다.
출력은 output/reading_excel_a_날짜시간 폴더에 자동 생성됩니다.
입력을 바꾸려면 --stage-a-dir, 새 출력 경로를 지정하려면 --output-dir을 사용합니다.

--offline-dotenv-shim은 python-dotenv가 없는 환경에서도 실행하도록 명시적으로
load_dotenv만 no-op으로 제공합니다. .env를 읽지 않으며 원래 실험과 같은 제한된
프로필을 적용합니다. 운영 환경 전체와 동일한 실행은 아닙니다.
16개 organize_sheet 호출 → apply_semantic_contract_guards → ExcelAgent.write를
실제로 실행합니다. 전체 organize/최종 합계 재계산은 호출하지 않습니다.
PDF·CSV·이미지 읽기 및 네트워크/서브프로세스 호출은 차단합니다.

산출물: backend_input.json, after_sheet_cleaning.json, backend_output.json,
semantic_guard_records.json, mapped_reading_a.xlsx, cell_validation.json,
execution_manifest.json, backend_schema.json.
정리 전후 차이와 정리 결과→저장 셀 차이를 분리합니다. 숫자 문자열은 숫자와
동일하다고 처리하지 않습니다. 출처페이지는 원시 판독의 발췌본 번호 그대로이며
원본 PDF 번호로 자동 변환하지 않습니다. Excel은 개발 검증용입니다.

종료 코드: 0=엄격 비교 일치, 2=파일 생성 후 차이 발견, 1=실행 오류.
2는 충돌을 숨기지 않기 위한 결과이며 Excel 생성 실패를 의미하지 않습니다.
기존 폴더를 지정하면 덮어쓰기 없이 중단합니다.

```powershell
py -m unittest tests.test_reading_compatibility tests.test_reading_cell_validation
```
