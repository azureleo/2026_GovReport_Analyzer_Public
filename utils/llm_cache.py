from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypedDict

import config


class _CacheFile(TypedDict):
    response: str


@dataclass(frozen=True, slots=True)
class LLMCacheRequest:
    call_kind: str
    provider: str
    model: str
    system: str
    prompt: str
    images_b64: tuple[str, ...] = ()


# 실행 단위 캐시 통계. Supervisor가 시작 시 reset_cache_stats()로 초기화하고
# 종료 시 get_cache_stats()로 hit/miss/write/disabled 요약을 출력한다.
_CACHE_STATS: dict[str, int] = {
    "hit": 0,
    "miss": 0,
    "write": 0,
    "rejected": 0,
    "disabled": 0,
    "coalesced": 0,
    "singleflight_bypass": 0,
}
_CACHE_WAIT_SECONDS = 0.0
# 병렬 호출 시 여러 스레드가 통계를 증가시키므로 보호한다.
_CACHE_STATS_LOCK = threading.Lock()


@dataclass(slots=True)
class _InflightRequest:
    event: threading.Event
    owner_thread_id: int
    response: str | None = None
    error: BaseException | None = None


_INFLIGHT: dict[str, _InflightRequest] = {}
_INFLIGHT_LOCK = threading.Lock()


def _bump_stat(key: str) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[key] += 1


def reset_cache_stats() -> None:
    global _CACHE_WAIT_SECONDS
    with _CACHE_STATS_LOCK:
        for key in _CACHE_STATS:
            _CACHE_STATS[key] = 0
        _CACHE_WAIT_SECONDS = 0.0


def get_cache_stats() -> dict[str, int | float]:
    with _CACHE_STATS_LOCK:
        return {
            **_CACHE_STATS,
            "singleflight_wait_seconds": round(_CACHE_WAIT_SECONDS, 3),
        }


def _add_wait_seconds(seconds: float) -> None:
    global _CACHE_WAIT_SECONDS
    with _CACHE_STATS_LOCK:
        _CACHE_WAIT_SECONDS += max(0.0, float(seconds))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_root() -> Path:
    configured = getattr(config, "LLM_CACHE_DIR", ".cache/llm_responses") or ".cache/llm_responses"
    return Path(configured)


def _is_enabled() -> bool:
    return bool(getattr(config, "LLM_CACHE_ENABLED", True))


def _singleflight_enabled() -> bool:
    return bool(getattr(config, "LLM_SINGLEFLIGHT_ENABLED", True))


def _request_key(request: LLMCacheRequest) -> str:
    payload = {
        "version": getattr(config, "LLM_CACHE_VERSION", "carbon-report-llm-cache-v1"),
        "call_kind": request.call_kind,
        "provider": request.provider,
        "model": request.model,
        "system_sha256": _sha256_text(request.system),
        "prompt_sha256": _sha256_text(request.prompt),
        "image_sha256": [_sha256_text(image) for image in request.images_b64],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _cache_path(request: LLMCacheRequest) -> Path:
    key = _request_key(request)
    return _cache_root() / key[:2] / f"{key}.json"


def _singleflight(
    request: LLMCacheRequest,
    producer: Callable[[], str],
) -> str:
    """동시에 들어온 동일 요청은 한 호출만 실행하고 나머지는 결과를 공유한다."""
    if not _singleflight_enabled():
        return producer()

    key = _request_key(request)
    thread_id = threading.get_ident()
    with _INFLIGHT_LOCK:
        state = _INFLIGHT.get(key)
        if state is None:
            state = _InflightRequest(threading.Event(), thread_id)
            _INFLIGHT[key] = state
            leader = True
        elif state.owner_thread_id == thread_id:
            # 같은 producer 안에서 같은 요청을 재귀 호출하면 대기 교착이 생기므로 우회한다.
            _bump_stat("singleflight_bypass")
            return producer()
        else:
            leader = False

    if not leader:
        started = time.perf_counter()
        state.event.wait()
        _add_wait_seconds(time.perf_counter() - started)
        _bump_stat("coalesced")
        if state.error is not None:
            raise state.error
        if state.response is None:
            raise RuntimeError("동일 LLM 요청 단일화 결과가 비어 있습니다")
        return state.response

    try:
        state.response = producer()
        return state.response
    except BaseException as exc:
        state.error = exc
        raise
    finally:
        state.event.set()
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(key) is state:
                _INFLIGHT.pop(key, None)


def _read_response(request: LLMCacheRequest) -> str | None:
    path = _cache_path(request)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    response = payload.get("response", "")
    if not isinstance(response, str):
        return None
    if not _is_valid_json_response(response):
        _bump_stat("rejected")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return response


def _write_response(request: LLMCacheRequest, response: str) -> bool:
    if not _is_valid_json_response(response):
        _bump_stat("rejected")
        return False

    path = _cache_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: _CacheFile = {"response": response}
    encoded = json.dumps(payload, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    return True


def _is_valid_json_response(response: str) -> bool:
    """실패 문구나 잘린 응답이 다음 실행에서 정상 응답처럼 재사용되지 않게 한다."""
    stripped = (response or "").strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    # 공급자 fail-soft 경로는 일시 오류를 빈 객체로 반환한다. 이를 캐시하면
    # 다음 실행에서도 정상 호출 없이 데이터가 계속 누락되므로 저장하지 않는다.
    if parsed == {}:
        return False
    return isinstance(parsed, (dict, list))


def cached_response(request: LLMCacheRequest, producer: Callable[[], str]) -> str:
    if not _is_enabled():
        _bump_stat("disabled")
        return _singleflight(request, producer)

    def load_or_produce() -> str:
        # 단일 실행 잠금을 얻은 뒤 다시 읽어 직전 완료 요청과의 cache stampede도 막는다.
        cached = _read_response(request)
        if cached is not None:
            _bump_stat("hit")
            return cached

        _bump_stat("miss")
        response = producer()
        try:
            if _write_response(request, response):
                _bump_stat("write")
        except OSError:
            return response
        return response

    return _singleflight(request, load_or_produce)


def cached_response_if_present(request: LLMCacheRequest) -> str | None:
    """회로 차단 중에도 이미 성공한 원 모델 캐시는 우선 재사용한다."""
    if not _is_enabled():
        return None
    cached = _read_response(request)
    if cached is not None:
        _bump_stat("hit")
    return cached
