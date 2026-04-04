"""
Gemini API 공통 래퍼 (google-genai SDK)

모든 에이전트가 이 모듈을 통해 Gemini를 호출합니다.
- call_text()  : JSON 강제 출력 모드 (response_mime_type=application/json)
- call_vision(): 이미지+텍스트, JSON 강제 출력
- Rate limit / 503 자동 재시도 포함
"""

import base64
import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types
from google.api_core import exceptions as google_exceptions

import config

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=config.GEMINI_API_KEY)

# 텍스트 전용: JSON 강제 출력 (잘린 JSON 방지)
_TEXT_CONFIG = types.GenerateContentConfig(
    max_output_tokens=config.MAX_TOKENS,
    temperature=0.1,
    response_mime_type="application/json",
)

# 비전용: JSON 강제 출력
_VISION_CONFIG = types.GenerateContentConfig(
    max_output_tokens=config.MAX_TOKENS,
    temperature=0.1,
    response_mime_type="application/json",
)


def _handle_retry(e: Exception, attempt: int, max_retries: int, label: str) -> bool:
    """재시도 여부 결정 및 대기. True 반환 시 계속 재시도."""
    if attempt >= max_retries:
        return False
    if isinstance(e, google_exceptions.ResourceExhausted):
        wait = 30 * attempt
        logger.warning(f"{label} Rate limit. {wait}초 대기 ({attempt}/{max_retries})")
        time.sleep(wait)
    elif isinstance(e, google_exceptions.ServiceUnavailable):
        wait = 15 * attempt
        logger.warning(f"{label} 503 서버 과부하. {wait}초 대기 ({attempt}/{max_retries})")
        time.sleep(wait)
    else:
        logger.warning(f"{label} 오류: {e}. 5초 후 재시도 ({attempt}/{max_retries})")
        time.sleep(5)
    return True


def call_text(prompt: str, system: str = "", max_retries: int = config.MAX_RETRIES) -> str:
    """
    텍스트 프롬프트로 Gemini 호출 (JSON 강제 출력 모드).

    Returns:
        JSON 문자열 (파싱 실패 시 "{}")
    """
    full_text = f"[지침]\n{system}\n\n[요청]\n{prompt}" if system else prompt
    contents = [types.Content(role="user", parts=[types.Part(text=full_text)])]

    for attempt in range(1, max_retries + 1):
        try:
            response = _client.models.generate_content(
                model=config.MODEL,
                contents=contents,
                config=_TEXT_CONFIG,
            )
            return response.text
        except Exception as e:
            if not _handle_retry(e, attempt, max_retries, "Gemini"):
                break

    logger.error("Gemini 최대 재시도 초과.")
    return "{}"


def call_vision(image_b64: str, prompt: str, system: str = "", max_retries: int = config.MAX_RETRIES) -> str:
    """
    이미지 + 텍스트로 Gemini Vision 호출 (JSON 강제 출력 모드).

    Returns:
        JSON 문자열 (파싱 실패 시 "{}")
    """
    image_bytes = base64.b64decode(image_b64)
    full_prompt = f"[지침]\n{system}\n\n[요청]\n{prompt}" if system else prompt

    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(inline_data=types.Blob(mime_type="image/png", data=image_bytes)),
                types.Part(text=full_prompt),
            ],
        )
    ]

    for attempt in range(1, max_retries + 1):
        try:
            response = _client.models.generate_content(
                model=config.MODEL,
                contents=contents,
                config=_VISION_CONFIG,
            )
            return response.text
        except Exception as e:
            if not _handle_retry(e, attempt, max_retries, "Vision"):
                break

    logger.error("Vision 최대 재시도 초과.")
    return "{}"


def _find_json_end(text: str, start: int) -> int:
    """
    start 위치의 '{' 에서 시작하는 JSON 객체의 닫는 '}' 위치를 반환.
    괄호 깊이를 추적하므로 }}]} 같은 후행 쓰레기에 영향받지 않음.
    """
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def parse_json(text: str) -> Any:
    """
    Gemini JSON 모드 응답 파싱.
    JSON 모드 사용 시 이미 순수 JSON이 오지만, 안전을 위해 코드블록 제거도 처리.
    }}]} 같은 후행 쓰레기는 괄호 깊이 추적으로 무시.
    """
    if not text or text.strip() in ("{}", ""):
        return {}

    text = text.strip()

    # 마크다운 코드블록 제거 (혹시 모를 경우 대비)
    if text.startswith("```"):
        lines = text.split("\n")
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 잘린 JSON 복구 시도: 괄호 깊이 추적으로 정확한 끝 위치 계산
        start = text.find("{")
        if start >= 0:
            end = _find_json_end(text, start)
            if end >= 0:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            # 마지막 수단: rfind (end 찾기 실패 시)
            end_fallback = text.rfind("}") + 1
            if end_fallback > start:
                try:
                    return json.loads(text[start:end_fallback])
                except json.JSONDecodeError:
                    pass

    logger.warning(f"JSON 파싱 실패 (응답 앞 200자): {text[:200]}")
    return {}
