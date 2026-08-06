#!/usr/bin/env bash
set -euo pipefail

: "${UOCR_MODEL_PATH:?UOCR_MODEL_PATH에 Unlimited-OCR 모델 경로를 지정하세요.}"

HOST="${UOCR_HOST:-0.0.0.0}"
PORT="${UOCR_PORT:-10000}"
MEM_FRACTION="${UOCR_MEM_FRACTION:-0.8}"
ATTENTION_BACKEND="${UOCR_ATTENTION_BACKEND:-fa3}"
# RTX 50 계열에서 CUDA Toolkit(nvcc) 없이 PyTorch wheel만 사용할 때
# DeepGEMM JIT 초기화가 CUDA_HOME을 요구하며 종료되는 것을 방지한다.
export SGLANG_ENABLE_JIT_DEEPGEMM="${SGLANG_ENABLE_JIT_DEEPGEMM:-0}"

python -m sglang.launch_server \
  --model "$UOCR_MODEL_PATH" \
  --served-model-name Unlimited-OCR \
  --attention-backend "$ATTENTION_BACKEND" \
  --page-size 1 \
  --context-length 32768 \
  --enable-custom-logit-processor \
  --disable-overlap-schedule \
  --skip-server-warmup \
  --host "$HOST" \
  --port "$PORT" \
  --mem-fraction-static "$MEM_FRACTION"
