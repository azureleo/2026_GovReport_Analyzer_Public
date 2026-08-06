# Unlimited-OCR 결합 실험 1

현재 탄소중립 보고서 추출 프로젝트를 직접 변경하기 전에
[baidu/Unlimited-OCR](https://github.com/baidu/Unlimited-OCR)의 효과를 재현 가능한 조건에서 비교하는 독립 실험 하네스입니다.

## 실험 목적

다음 세 결과를 동일한 페이지 표본과 동일한 골든셋으로 비교합니다.

1. `pymupdf`: 현재 프로젝트가 사용하는 native text 및 표 추출 기준선
2. `unlimited_ocr`: 페이지 이미지에 대한 Unlimited-OCR 결과
3. `hybrid`: native text를 유지하면서 누락·부분·복잡표 페이지의 표와 그림만 OCR로 보완한 결과

Unlimited-OCR가 출력한 원문 문자열은 바로 본 프로젝트의 16개 시트에 넣지 않습니다. 먼저 공통 객체로 변환하고,
규칙 기반 중복 제거와 보수적 병합을 거친 뒤 현재 프로젝트의 `DocumentObject` JSONL로 내보냅니다.

```text
기존 결과 XLSX의 21_원문객체인벤토리
                 │
                 ▼
     층화 표본 선정 및 300 DPI 렌더링
          ┌──────┴────────┐
          ▼               ▼
 PyMuPDF 기준선       Unlimited-OCR
          └──────┬────────┘
                 ▼
       규칙 기반 하이브리드 병합
                 ▼
      사람 검수 골든셋 기반 비교
                 ▼
 JSON + Markdown + XLSX 실험 보고서
```

## 폴더 구조

```text
졸업프로젝트_실험1/
├─ experiment.py                 # 실험 CLI
├─ requirements.txt              # Windows 실험 하네스 의존성
├─ scripts/
│  ├─ check_uocr_server.ps1      # SGLang 서버 상태 확인
│  └─ start_uocr_server.sh       # WSL2용 서버 시작 예시
├─ tests/                        # 파서·병합·평가 단위 테스트
└─ uocr_experiment/
   ├─ sampling.py                # 층화 표본과 페이지 렌더링
   ├─ baseline.py                # PyMuPDF 기준선
   ├─ uocr_client.py             # OpenAI 호환 SGLang 클라이언트
   ├─ uocr_parser.py             # 좌표 태그 및 Markdown 파서
   ├─ hybrid.py                  # 결정론적 병합
   ├─ evaluation.py              # 골든셋 평가
   └─ project_bridge.py          # 현 프로젝트 DocumentObject 어댑터
```

실행 결과는 기본적으로 `runs/<실험명>` 아래에만 생성되며 현재 프로젝트의 코드나 캐시를 덮어쓰지 않습니다.

## 1. Windows 실험 환경

Python 3.11을 권장합니다.

```powershell
cd "C:\Users\정현택\OneDrive\바탕 화면\과제\4-1\졸업프로젝트\졸업프로젝트_실험1"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest -q
```

## 2. Unlimited-OCR 환경

최신 CUDA, PyTorch, SGLang 의존성이 Windows native 환경과 충돌할 수 있으므로 **WSL2/Ubuntu에 별도 설치**하는 구성을 권장합니다.
Unlimited-OCR 저장소의 설치 지침과 모델 다운로드 명령을 우선 적용하세요.

```bash
git clone https://github.com/baidu/Unlimited-OCR.git
cd Unlimited-OCR
# 이후 설치와 모델 다운로드는 저장소 README의 현재 명령을 사용
```

SGLang OpenAI 호환 서버를 사용하는 경우 모델 경로를 지정하고 이 실험에 포함된 시작 스크립트를 실행할 수 있습니다.

```bash
export UOCR_MODEL_PATH=/absolute/path/to/model
export UOCR_PORT=10000
bash /mnt/c/Users/<사용자>/.../졸업프로젝트_실험1/scripts/start_uocr_server.sh
```

공식 저장소가 별도 서버 시작 명령을 제공하면 그 명령을 우선 사용하고, 포트만 `10000`으로 맞추면 됩니다.
Windows PowerShell에서 연결을 확인합니다.

```powershell
.\scripts\check_uocr_server.ps1
```

공식 반복 억제 logit processor 문자열도 WSL2에서 한 번 내보냅니다. 출력 경로는 Windows에서 접근할 수 있는
이 실험 폴더의 `processor.txt`로 지정하세요.

```bash
bash scripts/export_logit_processor.sh /mnt/c/Users/<사용자>/.../졸업프로젝트_실험1/processor.txt
```

RTX 5070 Ti 16GB에서는 `--concurrency 1`로 시작하세요. OOM이 발생하면 표본 렌더링 DPI를 300에서 200으로 낮추고,
서버의 메모리 비율 및 모델 양자화 설정은 Unlimited-OCR 공식 지침에 맞춰 조정합니다.

### RTX 50 계열 CUDA 확인

RTX 5070 Ti처럼 compute capability가 SM 12.x인 GPU는 CUDA 12.9 이상 PyTorch가 필요합니다. SGLang 설치 과정에서
`torch+cu128`이 선택되었다면 다음과 같이 공식 권장 버전의 CUDA 13.0 wheel로 교체합니다.

```bash
cd ~/Unlimited-OCR
source .venv/bin/activate

uv pip install --python .venv/bin/python --reinstall \
  torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 \
  --index-url https://download.pytorch.org/whl/cu130
```

검증 결과에서 `torch.version.cuda`가 `13.0`, GPU capability가 `(12, 0)`으로 나와야 합니다.

```bash
python - <<'PY'
import torch
print(torch.__version__)
print(torch.version.cuda)
print(torch.cuda.get_device_name(0))
print(torch.cuda.get_device_capability(0))
print(torch.zeros(1, device="cuda"))
PY
```

로컬 CUDA Toolkit이 없어 `CUDA_HOME` 관련 DeepGEMM 오류가 발생하면 실행 시 JIT DeepGEMM을 비활성화합니다.

```bash
export SGLANG_ENABLE_JIT_DEEPGEMM=0
```

현재 제공되는 일부 SGLang 개발 wheel은 위 환경변수를 확인하기 전에 번들 `deep_gemm`을 import하는 문제가 있습니다.
로그가 계속 `_find_cuda_home()`의 `AssertionError`에서 끝나면 선택 모듈을 가상환경 안에서 가역적으로 격리합니다.

```bash
DEEPGEMM_DIR=".venv/lib/python3.12/site-packages/deep_gemm"
mv "$DEEPGEMM_DIR" "${DEEPGEMM_DIR}.disabled"
export SGLANG_ENABLE_JIT_DEEPGEMM=0

python - <<'PY'
from sglang.srt.layers.deep_gemm_wrapper.configurer import ENABLE_JIT_DEEPGEMM
print("ENABLE_JIT_DEEPGEMM:", ENABLE_JIT_DEEPGEMM)
PY
```

`False`가 출력되어야 합니다. 원상복구는 다음과 같습니다.

```bash
mv ".venv/lib/python3.12/site-packages/deep_gemm.disabled" \
   ".venv/lib/python3.12/site-packages/deep_gemm"
```

이는 CUDA GPU 실행을 끄는 설정이 아니라, 별도 CUDA Toolkit을 요구하는 선택적 JIT DeepGEMM 경로만 끄는 설정입니다.

## 3. 권장 실험 절차

### 3.1 층화 표본 생성

기존 결과의 `21_원문객체인벤토리`가 있으면 다음 비율을 목표로 표본을 만듭니다.

- 미확인 30%
- 부분 연결 20%
- 복잡표 20%
- 그림·차트 20%
- 확인 대조군 10%

한 페이지가 여러 층에 포함될 수 있으므로 실제 층별 합계는 표본 수보다 클 수 있습니다.

```powershell
python experiment.py prepare `
  --pdf "..\서울특별시_탄소중립계획.pdf" `
  --inventory "..\탄소중립_추출결과_20260728_105418.xlsx" `
  --workspace "runs\seoul_uocr_v1" `
  --sample-size 50 `
  --seed 42 `
  --dpi 300
```

인벤토리가 없으면 표·그림 캡션과 PDF 이미지 유무로 후보 페이지를 생성합니다. 특정 페이지만 빠르게 확인하려면
`--pages "101,120-125,148"`을 사용합니다.

### 3.2 현재 방식 기준선

```powershell
python experiment.py baseline --manifest "runs\seoul_uocr_v1\sample_manifest.json"
```

### 3.3 Unlimited-OCR 실행

```powershell
python experiment.py uocr `
  --manifest "runs\seoul_uocr_v1\sample_manifest.json" `
  --server-url "http://127.0.0.1:10000" `
  --image-mode gundam `
  --processor-file ".\processor.txt" `
  --concurrency 1 `
  --timeout 900
```

`--model`을 생략하면 `/v1/models`가 반환한 첫 모델 ID를 사용합니다. 모델 저장소가 제공하는 logit processor가 필요한 경우
신뢰할 수 있는 로컬 파일만 `--processor-file`에 지정하세요. 해당 소스가 서버 요청에 포함되므로 외부에서 받은 파일은 사용하지 않습니다.
공식 설정과 동일하게 `ngram_size=35`, `window_size=128`을 전송합니다.

공식 `infer.py`로 이미 페이지별 Markdown을 만들었다면 GPU 서버 호출을 생략할 수 있습니다. 파일 이름에는 페이지 번호가
포함되어야 합니다. 예: `page_0101.md`.

```powershell
python experiment.py import-uocr `
  --manifest "runs\seoul_uocr_v1\sample_manifest.json" `
  --raw-dir "C:\path\to\official_infer_outputs"
```

### 3.4 병합 및 현재 프로젝트 객체 변환

```powershell
python experiment.py hybrid --manifest "runs\seoul_uocr_v1\sample_manifest.json"
python experiment.py export-project `
  --manifest "runs\seoul_uocr_v1\sample_manifest.json" `
  --engine hybrid
```

병합 규칙은 다음과 같습니다.

- 검색 가능한 PDF의 native text는 유지
- PyMuPDF에 없는 OCR 표는 추가
- 어려운 페이지에서 OCR 표의 유효 셀이 20% 이상 많고 최소 5셀 증가할 때만 교체
- 중복 표·그림은 텍스트 유사도와 bbox IoU로 제거
- native text가 100자 미만인 스캔성 페이지에서만 OCR text 추가

`project_document_objects.jsonl`은 현재 프로젝트 `DocumentObject`와 같은 필드 구조입니다. 이 실험에서 성능 향상이 확인되기 전에는
본 파이프라인에 자동 주입하지 않는 것이 의도된 안전 장치입니다.

### 3.5 골든셋 작성과 평가

먼저 후보 통합 템플릿을 생성합니다.

```powershell
python experiment.py golden-template --manifest "runs\seoul_uocr_v1\sample_manifest.json"
```

`golden_template.jsonl`에서 사람이 각 객체를 원문과 대조합니다.

- 정답 객체는 `"include": true`
- 오검출은 `"include": false`
- 정답 유형·텍스트·표 셀·bbox는 `golden_*` 필드에서 수정

검수 후 평가합니다.

```powershell
python experiment.py evaluate `
  --manifest "runs\seoul_uocr_v1\sample_manifest.json" `
  --golden "runs\seoul_uocr_v1\golden_template.jsonl"
```

결과:

- `reports/evaluation.json`: 기계 처리용 전체 지표
- `reports/evaluation.md`: 요약
- `reports/evaluation.xlsx`: 객체·병합 판정·매칭 상세

골든셋 없이 `evaluate`를 실행할 수도 있지만 이때 출력되는 객체 수와 페이지 커버리지는 **정확도가 아닙니다**.

## 4. 한 번에 실행

SGLang 서버가 준비된 뒤 아래 명령으로 전체 단계를 실행할 수 있습니다.

```powershell
python experiment.py run `
  --pdf "..\서울특별시_탄소중립계획.pdf" `
  --inventory "..\탄소중립_추출결과_20260728_105418.xlsx" `
  --workspace "runs\seoul_uocr_v1" `
  --sample-size 50 `
  --server-url "http://127.0.0.1:10000" `
  --processor-file ".\processor.txt" `
  --concurrency 1 `
  --timeout 900
```

기존 Markdown을 사용할 때는 `--raw-dir`을 추가합니다.

## 5. 채택 기준

객체 수가 많다는 이유만으로 Unlimited-OCR를 채택하지 않습니다. 최소한 다음을 함께 봅니다.

- 미확인·부분 페이지의 표 객체 리콜 증가
- 표 행 리콜 및 셀 값 정확도 증가
- 오검출로 인한 객체 정밀도 하락 폭
- bbox IoU와 캡션 연결 품질
- 페이지당 처리 시간과 최대 VRAM

권장 초기 기준은 **표 행 리콜 또는 셀 정확도 +5%p 이상**, 객체 정밀도 하락 **2%p 이내**입니다. 기준을 충족할 때만
현재 프로젝트의 문서 객체 생성 단계에 선택적으로 연결합니다.

## 6. 안전성과 재현성

- manifest에 PDF 및 인벤토리 SHA-256, 표본 페이지, seed, DPI를 기록합니다.
- 좌표 태그는 `eval()`이 아닌 `ast.literal_eval()`로 읽습니다.
- 원본 PDF와 기존 프로젝트 코드는 수정하지 않습니다.
- OCR 원문과 병합 판정은 모두 JSONL로 남깁니다.
- 민감 문서는 외부 서버로 전송하지 말고 로컬 WSL2 서버만 사용합니다.
