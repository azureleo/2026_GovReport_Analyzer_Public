"""
LLM/로컬 에이전트 공통 래퍼.

기본 실행 경로는 Gemini API가 아니라 로컬 에이전트 CLI입니다.
- call_text()  : 텍스트 프롬프트를 선택된 LLM 백엔드에 전달
- call_vision(): 이미지 파일을 선택된 LLM 백엔드에 전달
- call_vision_batch(): 여러 이미지를 한 번에 첨부해 전수 분석 호출 수를 줄임
- parse_json() : 에이전트 응답에서 JSON만 안전하게 파싱

환경변수/CLI(main.py)로 선택 가능한 백엔드:
- LLM_PROVIDER=codex  : `codex exec` 사용 (기본값)
- LLM_PROVIDER=claude : `claude -p` 사용
- LLM_PROVIDER=auto   : codex → claude → gemini → openai 순으로 사용 가능한 백엔드 선택
- LLM_PROVIDER=gemini : 기존 Gemini API 백엔드(명시 선택 시에만)
- LLM_PROVIDER=openai : OpenAI Responses API 사용
"""
# noqa: SIZE_OK — 클라우드 SDK와 로컬 에이전트 백엔드 선택·재시도 계약을 한 파일에 보존하는 기존 래퍼.

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Sequence

import config
from utils.llm_cache import LLMCacheRequest, cached_response

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LLMCallError(RuntimeError):
    """LLM/로컬 에이전트 호출 실패."""


class LLMQuotaExceededError(LLMCallError):
    """계정 quota, 세션 한도, rate limit처럼 즉시 회복되지 않는 실패."""


class LLMTimeoutError(LLMCallError):
    """로컬 에이전트가 제한 시간 안에 응답하지 못한 실패."""


_QUOTA_ERROR_MARKERS = (
    "session limit",
    "you've hit your session limit",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "한도",
    "할당량",
)


_TRANSIENT_ERROR_MARKERS = (
    "503",
    "unavailable",
    "service unavailable",
    "high demand",
    "temporarily",
    "temporary",
    "server error",
    "internal error",
    "deadline",
    "timeout",
)


_OPENAI_QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "billing",
    "exceeded your current quota",
    "quota exceeded",
    "payment",
    "credit",
)


_LLM_STATS_LOCK = threading.Lock()
_LLM_STATS = {
    "calls": {},
    "failures": 0,
    "retries": 0,
    "quota_wait_seconds": 0.0,
    "timeouts": 0,
}


def reset_llm_stats() -> None:
    """현재 실행의 LLM 호출/대기 통계를 초기화한다."""
    with _LLM_STATS_LOCK:
        _LLM_STATS["calls"] = {}
        _LLM_STATS["failures"] = 0
        _LLM_STATS["retries"] = 0
        _LLM_STATS["quota_wait_seconds"] = 0.0
        _LLM_STATS["timeouts"] = 0


def get_llm_stats() -> dict[str, Any]:
    """현재 실행의 LLM 호출/대기 통계 스냅샷을 반환한다."""
    with _LLM_STATS_LOCK:
        calls = dict(_LLM_STATS["calls"])
        return {
            "calls": calls,
            "total_calls": sum(calls.values()),
            "failures": int(_LLM_STATS["failures"]),
            "retries": int(_LLM_STATS["retries"]),
            "quota_wait_seconds": float(_LLM_STATS["quota_wait_seconds"]),
            "timeouts": int(_LLM_STATS["timeouts"]),
        }


def _inc_stat(name: str, amount: int = 1) -> None:
    with _LLM_STATS_LOCK:
        _LLM_STATS[name] = int(_LLM_STATS[name]) + amount


def _add_wait_seconds(seconds: float) -> None:
    with _LLM_STATS_LOCK:
        _LLM_STATS["quota_wait_seconds"] = float(_LLM_STATS["quota_wait_seconds"]) + seconds


def _record_call(kind: str, provider: str, producer) -> str:
    with _LLM_STATS_LOCK:
        calls = dict(_LLM_STATS["calls"])
        key = f"{kind}:{provider}"
        calls[key] = calls.get(key, 0) + 1
        _LLM_STATS["calls"] = calls
    try:
        return producer()
    except (LLMCallError, OSError, RuntimeError, subprocess.SubprocessError):
        _inc_stat("failures")
        raise

_JSON_ONLY_INSTRUCTION = """
당신은 지자체 탄소중립 계획 문서에서 구조화 데이터를 추출하는 로컬 에이전트입니다.
중요 규칙:
1. 파일을 수정하지 마세요.
2. 외부 API 호출이나 네트워크 검색을 하지 마세요.
3. 주어진 프롬프트와 첨부 이미지/파일 경로만 근거로 판단하세요.
4. 최종 응답은 반드시 유효한 JSON만 출력하세요. 마크다운 코드블록, 설명, 주석은 금지입니다.
""".strip()


def _split_command(command: str) -> list[str]:
    """환경변수에 들어간 실행 명령을 안전하게 토큰화한다."""
    tokens = shlex.split(command or "", posix=os.name != "nt")
    if os.name == "nt":
        tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"} else token for token in tokens]
    if not tokens:
        raise RuntimeError("로컬 에이전트 실행 명령이 비어 있습니다.")
    return tokens


def _command_exists(command: str) -> bool:
    try:
        executable = _split_command(command)[0]
    except RuntimeError:
        return False
    return shutil.which(executable) is not None


def _stage_config_value(mapping_name: str, stage: str | None) -> str:
    if not stage:
        return ""
    mapping = getattr(config, mapping_name, {}) or {}
    if not isinstance(mapping, dict):
        return ""
    return str(mapping.get(stage, "") or "").strip()


def _resolve_provider(stage: str | None = None) -> str:
    stage_provider = _stage_config_value("STAGE_PROVIDERS", stage).lower()
    provider = stage_provider or (getattr(config, "LLM_PROVIDER", "codex") or "codex").strip().lower()
    aliases = {
        "local": "codex",
        "local-agent": "codex",
        "claude-code": "claude",
        "gemini-api": "gemini",
        "gpt": "openai",
        "openai-api": "openai",
    }
    provider = aliases.get(provider, provider)

    if provider == "auto":
        if _command_exists(getattr(config, "CODEX_COMMAND", "codex")):
            return "codex"
        if _command_exists(getattr(config, "CLAUDE_COMMAND", "claude")):
            return "claude"
        if getattr(config, "GEMINI_API_KEY", ""):
            return "gemini"
        if getattr(config, "OPENAI_API_KEY", ""):
            return "openai"
        raise RuntimeError(
            "사용 가능한 로컬 에이전트를 찾지 못했습니다. "
            "Codex CLI 또는 Claude Code를 설치하거나 API 백엔드를 설정하세요."
        )

    if provider == "codex" and not _command_exists(getattr(config, "CODEX_COMMAND", "codex")):
        raise RuntimeError("Codex CLI를 찾을 수 없습니다. CODEX_COMMAND 또는 --agent claude를 설정하세요.")
    if provider == "claude" and not _command_exists(getattr(config, "CLAUDE_COMMAND", "claude")):
        raise RuntimeError("Claude Code CLI를 찾을 수 없습니다. CLAUDE_COMMAND 또는 --agent codex를 설정하세요.")
    if provider == "gemini" and not getattr(config, "GEMINI_API_KEY", ""):
        raise RuntimeError("Gemini 백엔드를 사용하려면 GEMINI_API_KEY가 필요합니다.")
    if provider == "openai" and not getattr(config, "OPENAI_API_KEY", ""):
        raise RuntimeError("OpenAI 백엔드를 사용하려면 OPENAI_API_KEY가 필요합니다.")
    if provider not in {"codex", "claude", "gemini", "openai"}:
        raise RuntimeError(f"지원하지 않는 LLM_PROVIDER 값입니다: {provider}")
    return provider


def _agent_prompt(
    prompt: str,
    system: str = "",
    image_path: Path | None = None,
    image_paths: Sequence[Path] | None = None,
) -> str:
    parts = [_JSON_ONLY_INSTRUCTION]
    if system:
        parts.append(f"[시스템 지침]\n{system.strip()}")
    paths = list(image_paths or ([] if image_path is None else [image_path]))
    if paths:
        path_lines = "\n".join(f"- 이미지 {idx}: {path}" for idx, path in enumerate(paths, start=1))
        parts.append(
            "[이미지 입력]\n"
            f"{path_lines}\n"
            "첨부 이미지를 순서대로 판독해 아래 요청의 JSON 스키마에 맞춰 답하세요."
        )
    parts.append(f"[요청]\n{prompt.strip()}")
    return "\n\n".join(parts)


def _tail(text: str, limit: int = 1200) -> str:
    text = text or ""
    return text[-limit:]


def _is_quota_error_message(message: str) -> bool:
    normalized = message.casefold()
    return any(marker in normalized for marker in _QUOTA_ERROR_MARKERS)


def _is_transient_error_message(message: str) -> bool:
    normalized = message.casefold()
    return any(marker in normalized for marker in _TRANSIENT_ERROR_MARKERS)


def _is_openai_quota_error_message(message: str) -> bool:
    normalized = message.casefold()
    return any(marker in normalized for marker in _OPENAI_QUOTA_ERROR_MARKERS)


def _stage_model(stage: str | None) -> str:
    return _stage_config_value("STAGE_MODELS", stage)


def _gemini_default_model(stage: str | None) -> str:
    """Gemini 백엔드의 스테이지별 기본 모델.

    캐시 키(_model_identity)와 실제 호출 모델이 항상 같은 값을 쓰도록, gemini 기본
    모델 결정은 이 함수만 거친다. vision 단계의 pro 기본값 근거는
    config.GEMINI_VISION_MODEL 주석 참조.
    """
    if stage == "vision":
        vision_model = str(getattr(config, "GEMINI_VISION_MODEL", "") or "").strip()
        if vision_model:
            return vision_model
    return str(getattr(config, "MODEL", ""))


def _codex_default_model(stage: str | None) -> str:
    """codex 백엔드의 스테이지별 기본 모델.

    캐시 키(_model_identity)와 실제 --model 인자가 항상 같은 값을 쓰도록, codex 기본
    모델 결정은 이 함수만 거친다. vision 단계의 gpt-5.6-luna 기본값 근거는
    config.CODEX_VISION_MODEL 주석 참조.
    """
    if stage == "vision":
        vision_model = str(getattr(config, "CODEX_VISION_MODEL", "") or "").strip()
        if vision_model:
            return vision_model
    return str(getattr(config, "LOCAL_AGENT_MODEL", "") or "")


def _model_identity(provider: str, stage: str | None = None) -> str:
    stage_model = _stage_model(stage)
    if provider == "gemini":
        return stage_model or _gemini_default_model(stage)
    if provider == "openai":
        return stage_model or str(getattr(config, "OPENAI_MODEL", ""))
    if provider == "codex":
        return ":".join([
            str(getattr(config, "CODEX_COMMAND", "codex")),
            stage_model or _codex_default_model(stage),
        ])
    if provider == "claude":
        return ":".join([
            str(getattr(config, "CLAUDE_COMMAND", "claude")),
            stage_model or str(getattr(config, "LOCAL_AGENT_MODEL", "")),
        ])
    return stage_model or str(getattr(config, "LOCAL_AGENT_MODEL", ""))


def _run_command(command: Sequence[str], prompt: str, *, cwd: Path, timeout: int) -> str:
    """로컬 CLI를 실행하고 stdout을 반환한다."""
    logger.debug("로컬 에이전트 실행: %s", " ".join(shlex.quote(part) for part in command))
    completed = subprocess.run(
        list(command),
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=str(cwd),
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        message = (
            "로컬 에이전트 실행 실패 "
            f"(exit={completed.returncode})\n"
            f"STDERR:\n{_tail(completed.stderr)}\n"
            f"STDOUT:\n{_tail(completed.stdout)}"
        )
        if _is_quota_error_message(message):
            raise LLMQuotaExceededError(message)
        raise LLMCallError(message)
    return completed.stdout.strip()


def _run_codex(
    prompt: str,
    *,
    image_paths: Sequence[Path] | None = None,
    cwd: Path,
    model: str | None = None,
) -> str:
    timeout = int(getattr(config, "LOCAL_AGENT_TIMEOUT", 300))
    command = _split_command(getattr(config, "CODEX_COMMAND", "codex"))

    with tempfile.TemporaryDirectory(prefix="carbon-codex-") as tmpdir:
        output_path = Path(tmpdir) / "last_message.txt"
        command += [
            "exec",
            "--sandbox",
            "read-only",
            # 추출은 레포 파일 접근이 불필요하다. AGENTS.md 자동 로드를 피하려고
            # 프로젝트 루트가 아닌 중립 작업 디렉터리에서 실행한다.
            # 그 임시 디렉터리는 git repo/신뢰 디렉터리가 아니므로, codex가
            # "Not inside a trusted directory" 로 거부하지 않도록 git 체크를 건너뛴다.
            "--skip-git-repo-check",
            "--cd",
            str(cwd),
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
        ]
        effective_model = model if model is not None else getattr(config, "LOCAL_AGENT_MODEL", "")
        if effective_model:
            command += ["--model", effective_model]
        for path in list(image_paths or []):
            command += ["--image", str(path)]
        command.append("-")

        stdout = _run_command(command, prompt, cwd=cwd, timeout=timeout)
        if output_path.exists():
            message = output_path.read_text(encoding="utf-8").strip()
            if message:
                return message
        return stdout


def _claude_minimal_flags(image_paths: Sequence[Path]) -> list[str]:
    """
    추출은 단발 JSON 작업이라 레포/ MCP/스킬/프로젝트 메모리가 불필요하다.
    호출당 세션 오버헤드(프로젝트 CLAUDE.md, MCP 서버 스키마, 스킬, 설정/훅,
    동적 시스템 프롬프트 섹션)를 끈다. 추출 출력에는 영향이 없다.

    텍스트 호출은 도구 자체를 비활성화하고, 이미지 호출은 로컬 이미지 판독에
    필요한 Read만 허용한다.
    """
    if not getattr(config, "CLAUDE_MINIMAL_SESSION", True):
        return []
    tools = "Read" if image_paths else ""
    return [
        "--tools",
        tools,
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--exclude-dynamic-system-prompt-sections",
    ]


def _run_claude(
    prompt: str,
    *,
    image_paths: Sequence[Path] | None = None,
    cwd: Path,
    model: str | None = None,
) -> str:
    # Claude Code는 버전별 CLI 옵션 차이가 있어 가장 보편적인 print 모드를 사용한다.
    # 이미지가 있으면 prompt에 cwd 내부 임시 파일 경로가 포함되어 Claude가 읽을 수 있다.
    timeout = int(getattr(config, "LOCAL_AGENT_TIMEOUT", 300))
    base_command = _split_command(getattr(config, "CLAUDE_COMMAND", "claude"))
    paths = list(image_paths or [])
    command = [*base_command, "-p", "--output-format", "text"]
    effective_model = model if model is not None else getattr(config, "LOCAL_AGENT_MODEL", "")
    if effective_model:
        command += ["--model", effective_model]
    command += _claude_minimal_flags(paths)
    try:
        return _run_command(command, prompt, cwd=cwd, timeout=timeout)
    except LLMQuotaExceededError:
        raise
    except LLMCallError:
        # 일부 Claude Code 버전은 print 모드에서 stdin 대신 prompt positional arg를 기대한다.
        # 긴 문서 프롬프트는 argv 한도를 넘을 수 있으므로 짧은 경우에만 호환 폴백을 시도한다.
        if len(prompt) > 100_000:
            raise
        fallback = [*command, prompt]
        logger.debug("Claude stdin 실행 실패, positional prompt 폴백 시도")
        return _run_command(fallback, "", cwd=cwd, timeout=timeout)


def _call_local_agent(
    prompt: str,
    system: str,
    *,
    image_b64: str | None = None,
    images_b64: Sequence[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    provider = provider or _resolve_provider()
    images = list(images_b64 or ([] if image_b64 is None else [image_b64]))
    # 호출마다 깨끗한 작업 디렉터리를 쓴다. 프로젝트 CLAUDE.md/AGENTS.md 자동 로드를
    # 막고, 이미지는 이 디렉터리 안에 기록해 에이전트가 cwd 내부에서 읽게 한다.
    # Codex/Claude 종료 직후 훅이 .omx 같은 상태 디렉터리를 늦게 쓸 수 있어,
    # 정리 경합은 성공 응답을 버리는 호출 실패로 취급하지 않는다.
    with tempfile.TemporaryDirectory(prefix="carbon-agent-", ignore_cleanup_errors=True) as workdir_name:
        workdir = Path(workdir_name)
        image_paths: list[Path] = []
        for idx, image in enumerate(images, start=1):
            path = workdir / f"input_{idx:03d}.png"
            path.write_bytes(base64.b64decode(image))
            image_paths.append(path)
        image_path = image_paths[0] if len(image_paths) == 1 else None

        prepared = _agent_prompt(prompt, system, image_path=image_path, image_paths=image_paths)
        if provider == "codex":
            return _run_codex(prepared, image_paths=image_paths, cwd=workdir, model=model)
        if provider == "claude":
            return _run_claude(prepared, image_paths=image_paths, cwd=workdir, model=model)
        raise RuntimeError(f"지원하지 않는 로컬 에이전트입니다: {provider}")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}분 {sec}초"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분"


def _parse_quota_reset_seconds(message: str) -> int | None:
    """
    한도 초과 메시지에서 회복까지 남은 시간(초)을 best-effort로 파싱한다.

    지원 형태:
    - "retry after 1234" / "retry-after: 1234"            → 초
    - "try again in 3h 20m", "in 45 minutes", "in 30s"    → 시/분/초 합산
    - "resets in 2 hours 5 minutes"                        → 동일
    파싱 실패 시 None.
    """
    text = (message or "").lower()

    m = re.search(r"retry[\s\-]?after[:\s]+(\d+)", text)
    if m:
        return int(m.group(1))

    m = re.search(r"(?:again|retry|reset[s]?|available)\s+in\s+(.+?)(?:[.\n,;]|$)", text)
    segment = m.group(1) if m else ""
    if segment:
        total = 0
        found = False
        for value, unit in re.findall(
            r"(\d+)\s*(h|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)",
            segment,
        ):
            found = True
            v = int(value)
            if unit.startswith("h"):
                total += v * 3600
            elif unit.startswith("m"):
                total += v * 60
            else:
                total += v
        if found:
            return total
    return None


def _sleep_with_heartbeat(total_seconds: float, label: str) -> None:
    """대기 중 주기적으로 '아직 살아 있음'을 로깅하며 sleep."""
    _add_wait_seconds(total_seconds)
    heartbeat = max(30, int(getattr(config, "LLM_QUOTA_WAIT_HEARTBEAT_SECONDS", 300)))
    remaining = int(total_seconds)
    while remaining > 0:
        chunk = min(heartbeat, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            logger.warning("%s 할당량 회복 대기 중... 약 %s 남음", label, _fmt_duration(remaining))


def _retry_local_call(fn, *, max_retries: int, label: str) -> str:
    attempt = 0
    quota_waited = 0.0
    timeout_retries = max(0, int(getattr(config, "LOCAL_AGENT_TIMEOUT_RETRIES", 1)))
    timeout_max_attempts = min(max(1, int(max_retries)), timeout_retries + 1)
    while True:
        attempt += 1
        try:
            return fn()
        except subprocess.TimeoutExpired as exc:
            _inc_stat("timeouts")
            if attempt >= timeout_max_attempts:
                logger.error(
                    "%s 타임아웃 상한 도달(%s회). 상위 배치 분할/실패 격리로 넘깁니다.",
                    label,
                    timeout_max_attempts,
                )
                raise LLMTimeoutError(
                    f"{label} 타임아웃 상한 도달({timeout_max_attempts}회)"
                ) from exc
            wait = max(
                0,
                int(getattr(config, "LOCAL_AGENT_TIMEOUT_RETRY_DELAY_SECONDS", 5)),
            )
            logger.warning(
                "%s 타임아웃. %s초 후 제한 재시도 (%s/%s)",
                label,
                wait,
                attempt,
                timeout_max_attempts,
            )
            _inc_stat("retries")
            if wait:
                time.sleep(wait)
        except LLMQuotaExceededError as exc:
            if not getattr(config, "LLM_QUOTA_WAIT_ENABLED", True):
                raise
            parsed = _parse_quota_reset_seconds(str(exc))
            poll = int(getattr(config, "LLM_QUOTA_WAIT_POLL_SECONDS", 120))
            wait = (parsed + 30) if parsed is not None else poll
            cap = int(getattr(config, "LLM_QUOTA_WAIT_MAX_SECONDS", 1800))
            if quota_waited + wait > cap:
                logger.error(
                    "%s quota/세션 한도 대기 누적 %s 가 상한 %s 초과. 배치 실패로 격리합니다.",
                    label, _fmt_duration(quota_waited), _fmt_duration(cap),
                )
                raise
            quota_waited += wait
            attempt -= 1
            _inc_stat("retries")
            logger.warning(
                "%s quota/세션 한도 감지. %s 후 자동 재개(누적 대기 %s).",
                label, _fmt_duration(wait), _fmt_duration(quota_waited),
            )
            _sleep_with_heartbeat(wait, label)
        except (LLMCallError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if attempt >= max_retries:
                logger.error("%s 최대 재시도 초과: %s", label, exc)
                raise LLMCallError(f"{label} 최대 재시도 초과") from exc
            wait = min(5 * attempt, 30)
            logger.warning("%s 오류: %s. %s초 후 재시도 (%s/%s)", label, exc, wait, attempt, max_retries)
            _inc_stat("retries")
            time.sleep(wait)


def call_text(
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> str:
    """텍스트 프롬프트를 단계별 백엔드 오버라이드까지 반영해 전달한다."""
    provider = _resolve_provider(stage)
    stage_model = _stage_model(stage)
    request = LLMCacheRequest(
        call_kind="text",
        provider=provider,
        model=_model_identity(provider, stage),
        system=system,
        prompt=prompt,
    )
    if provider == "gemini":
        def produce_gemini_text() -> str:
            if stage_model:
                return _call_gemini_text(prompt, system, max_retries=max_retries, model=stage_model)
            return _call_gemini_text(prompt, system, max_retries=max_retries)

        return cached_response(
            request,
            lambda: _record_call("text", provider, produce_gemini_text),
        )

    if provider == "openai":
        return cached_response(
            request,
            lambda: _record_call(
                "text",
                provider,
                lambda: _call_openai_text(
                    prompt,
                    system,
                    max_retries=max_retries,
                    model=stage_model or None,
                ),
            ),
        )

    return cached_response(
        request,
        lambda: _record_call(
            "text",
            provider,
            lambda: _retry_local_call(
                lambda: _call_local_agent(prompt, system, provider=provider, model=stage_model or None),
                max_retries=max_retries,
                label=provider,
            ),
        ),
    )


def call_vision(
    image_b64: str,
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> str:
    """이미지 + 텍스트 프롬프트를 단계별 백엔드 오버라이드까지 반영해 전달한다."""
    provider = _resolve_provider(stage)
    stage_model = _stage_model(stage)
    request = LLMCacheRequest(
        call_kind="vision",
        provider=provider,
        model=_model_identity(provider, stage),
        system=system,
        prompt=prompt,
        images_b64=(image_b64,),
    )
    if provider == "gemini":
        gemini_vision_model = stage_model or _gemini_default_model(stage)

        def produce_gemini_vision() -> str:
            return _call_gemini_vision(image_b64, prompt, system, max_retries=max_retries, model=gemini_vision_model)

        return cached_response(
            request,
            lambda: _record_call("vision", provider, produce_gemini_vision),
        )

    if provider == "openai":
        return cached_response(
            request,
            lambda: _record_call(
                "vision",
                provider,
                lambda: _call_openai_vision(
                    image_b64,
                    prompt,
                    system,
                    max_retries=max_retries,
                    model=stage_model or None,
                ),
            ),
        )

    return cached_response(
        request,
        lambda: _record_call(
            "vision",
            provider,
            lambda: _retry_local_call(
                lambda: _call_local_agent(
                    prompt,
                    system,
                    image_b64=image_b64,
                    provider=provider,
                    model=(stage_model or (_codex_default_model(stage) if provider == "codex" else "")) or None,
                ),
                max_retries=max_retries,
                label=f"{provider} vision",
            ),
        ),
    )


def call_vision_batch(
    images_b64: Sequence[str],
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> str:
    """여러 이미지 + 텍스트 프롬프트를 단계별 백엔드 오버라이드까지 반영해 전달한다."""
    if not images_b64:
        return "{}"
    if len(images_b64) == 1:
        return call_vision(images_b64[0], prompt, system=system, max_retries=max_retries, stage=stage)

    provider = _resolve_provider(stage)
    stage_model = _stage_model(stage)
    request = LLMCacheRequest(
        call_kind="vision_batch",
        provider=provider,
        model=_model_identity(provider, stage),
        system=system,
        prompt=prompt,
        images_b64=tuple(images_b64),
    )
    if provider == "gemini":
        gemini_vision_model = stage_model or _gemini_default_model(stage)

        def produce_gemini_vision_batch() -> str:
            return _call_gemini_vision_batch(images_b64, prompt, system, max_retries=max_retries, model=gemini_vision_model)

        return cached_response(
            request,
            lambda: _record_call("vision_batch", provider, produce_gemini_vision_batch),
        )

    if provider == "openai":
        return cached_response(
            request,
            lambda: _record_call(
                "vision_batch",
                provider,
                lambda: _call_openai_vision_batch(
                    images_b64,
                    prompt,
                    system,
                    max_retries=max_retries,
                    model=stage_model or None,
                ),
            ),
        )

    return cached_response(
        request,
        lambda: _record_call(
            "vision_batch",
            provider,
            lambda: _retry_local_call(
                lambda: _call_local_agent(
                    prompt,
                    system,
                    images_b64=images_b64,
                    provider=provider,
                    model=(stage_model or (_codex_default_model(stage) if provider == "codex" else "")) or None,
                ),
                max_retries=max_retries,
                label=f"{provider} vision batch",
            ),
        ),
    )


def _get_openai_client_class():
    """OpenAI 백엔드는 명시적으로 선택된 경우에만 SDK를 지연 import한다."""
    try:
        module = importlib.import_module("openai")
    except ImportError as exc:  # pragma: no cover - 선택 백엔드 미설치 환경용
        raise RuntimeError(
            "OpenAI 백엔드를 사용하려면 openai 패키지를 설치하세요: pip install openai"
        ) from exc
    try:
        return module.OpenAI
    except AttributeError as exc:  # pragma: no cover - 비정상 SDK 설치 환경용
        raise RuntimeError("OpenAI SDK에서 OpenAI 클라이언트를 찾지 못했습니다.") from exc


def _openai_client():
    client_class = _get_openai_client_class()
    return client_class(api_key=config.OPENAI_API_KEY)


def _openai_instructions(system: str = "") -> str:
    parts = [_JSON_ONLY_INSTRUCTION]
    if system:
        parts.append(system.strip())
    return "\n\n".join(parts)


def _openai_response_text(response: Any) -> str:
    """Responses API 결과에서 SDK 버전 차이를 흡수해 텍스트를 꺼낸다."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    for item in output or []:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        for part in content or []:
            text = getattr(part, "text", None)
            if text is None and isinstance(part, dict):
                text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text.strip()
    raise LLMCallError("OpenAI 응답에서 텍스트를 찾지 못했습니다.")


def _openai_max_retries(max_retries: int) -> int:
    return max(max_retries, int(getattr(config, "OPENAI_MAX_RETRIES", max_retries)))


def _is_openai_transient_error(e: Exception) -> bool:
    status_code = getattr(e, "status_code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    message = str(e)
    normalized = message.casefold()
    return (
        _is_transient_error_message(message)
        or "rate_limit" in normalized
        or "rate limit" in normalized
        or "connection" in normalized
    )


def _handle_openai_retry(e: Exception, attempt: int, max_retries: int, label: str) -> bool:
    if attempt >= max_retries or _is_openai_quota_error_message(str(e)):
        return False

    base = max(1, int(getattr(config, "OPENAI_RETRY_BASE_SECONDS", 10)))
    max_wait = max(base, int(getattr(config, "OPENAI_RETRY_MAX_SECONDS", 60)))
    if _is_openai_transient_error(e):
        wait = min(max_wait, base * (2 ** (attempt - 1)))
        wait += random.uniform(0, min(2.0, base / 4))
        logger.warning(
            "%s 일시 오류: %s. %.1f초 후 재시도 (%s/%s)",
            label,
            e,
            wait,
            attempt,
            max_retries,
        )
    else:
        wait = 5
        logger.warning("%s 오류: %s. %s초 후 재시도 (%s/%s)", label, e, wait, attempt, max_retries)
    time.sleep(wait)
    return True


def _should_fail_soft_openai(e: Exception | None) -> bool:
    if e is None or _is_openai_quota_error_message(str(e)):
        return False
    return bool(getattr(config, "OPENAI_FAIL_SOFT_ON_TRANSIENT", True)) and _is_openai_transient_error(e)


def _empty_json_after_openai_failure(label: str, last_error: Exception | None) -> str:
    logger.error(
        "%s 일시 오류 최대 재시도 초과. 해당 호출은 빈 JSON으로 처리하고 파이프라인을 계속합니다: %s",
        label,
        last_error,
    )
    return "{}"


def _openai_request_args(
    prompt: str,
    system: str = "",
    images_b64: Sequence[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for image in list(images_b64 or []):
        content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{image}",
        })
    content.append({"type": "input_text", "text": prompt})

    return {
        "model": model or getattr(config, "OPENAI_MODEL", "gpt-5.4-mini"),
        "instructions": _openai_instructions(system),
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": int(getattr(config, "OPENAI_MAX_OUTPUT_TOKENS", 32768)),
        "text": {"format": {"type": "json_object"}},
    }


def _call_openai_response(
    prompt: str,
    system: str,
    *,
    images_b64: Sequence[str] | None = None,
    max_retries: int = config.MAX_RETRIES,
    label: str = "OpenAI",
    model: str | None = None,
) -> str:
    max_retries = _openai_max_retries(max_retries)
    client = _openai_client()
    request_args = _openai_request_args(prompt, system, images_b64=images_b64, model=model)

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.responses.create(**request_args)
            return _openai_response_text(response)
        except Exception as e:  # noqa: BLE001 - SDK 예외 범위가 넓고 버전별 타입이 다르다.
            last_error = e
            if not _handle_openai_retry(e, attempt, max_retries, label):
                break

    logger.error("%s 최대 재시도 초과.", label)
    if last_error is not None and _is_openai_quota_error_message(str(last_error)):
        raise LLMQuotaExceededError(f"{label} quota/billing 한도 초과") from last_error
    if _should_fail_soft_openai(last_error):
        return _empty_json_after_openai_failure(label, last_error)
    if last_error is not None:
        raise LLMCallError(f"{label} 최대 재시도 초과") from last_error
    raise LLMCallError(f"{label} 호출 실패")


def _call_openai_text(
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    model: str | None = None,
) -> str:
    return _call_openai_response(
        prompt,
        system,
        max_retries=max_retries,
        label="OpenAI",
        model=model,
    )


def _call_openai_vision(
    image_b64: str,
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    model: str | None = None,
) -> str:
    return _call_openai_response(
        prompt,
        system,
        images_b64=[image_b64],
        max_retries=max_retries,
        label="OpenAI vision",
        model=model,
    )


def _call_openai_vision_batch(
    images_b64: Sequence[str],
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    model: str | None = None,
) -> str:
    return _call_openai_response(
        prompt,
        system,
        images_b64=images_b64,
        max_retries=max_retries,
        label="OpenAI vision batch",
        model=model,
    )


def _import_gemini_dependency(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - 선택 백엔드 미설치 환경용
        missing = exc.name or module_name
        raise RuntimeError(
            f"Gemini 백엔드를 사용하려면 누락 모듈 '{missing}'을 설치하세요."
        ) from exc


def _get_gemini_modules():
    """Gemini 백엔드는 명시적으로 선택된 경우에만 SDK를 지연 import한다."""
    genai = _import_gemini_dependency("google.genai")
    types = _import_gemini_dependency("google.genai.types")
    google_exceptions = _import_gemini_dependency("google.api_core.exceptions")
    return genai, types, google_exceptions


def _gemini_client():
    genai, types, _ = _get_gemini_modules()
    timeout_ms = max(1, int(getattr(config, "GEMINI_REQUEST_TIMEOUT_SECONDS", 180))) * 1000
    return genai.Client(
        api_key=config.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _handle_gemini_retry(e: Exception, attempt: int, max_retries: int, label: str) -> bool:
    """Gemini API 재시도 여부 결정 및 대기. True 반환 시 계속 재시도."""
    _, _, google_exceptions = _get_gemini_modules()
    if attempt >= max_retries:
        return False
    if isinstance(e, google_exceptions.ResourceExhausted):
        wait = 30 * attempt
        logger.warning("%s Rate limit. %s초 대기 (%s/%s)", label, wait, attempt, max_retries)
        time.sleep(wait)
    elif isinstance(e, google_exceptions.ServiceUnavailable):
        wait = 15 * attempt
        logger.warning("%s 503 서버 과부하. %s초 대기 (%s/%s)", label, wait, attempt, max_retries)
        time.sleep(wait)
    else:
        logger.warning("%s 오류: %s. 5초 후 재시도 (%s/%s)", label, e, attempt, max_retries)
        time.sleep(5)
    return True


def _call_gemini_text(prompt: str, system: str = "", max_retries: int = config.MAX_RETRIES, model: str | None = None) -> str:
    """레거시 Gemini API 텍스트 호출."""
    _, types, google_exceptions = _get_gemini_modules()
    full_text = f"[지침]\n{system}\n\n[요청]\n{prompt}" if system else prompt
    contents = [types.Content(role="user", parts=[types.Part(text=full_text)])]
    api_config = types.GenerateContentConfig(
        max_output_tokens=config.MAX_TOKENS,
        temperature=float(getattr(config, "LLM_TEMPERATURE", 0.0)),
        response_mime_type="application/json",
    )
    client = _gemini_client()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model or config.MODEL,
                contents=contents,
                config=api_config,
            )
            return response.text
        except Exception as e:  # noqa: BLE001 - SDK 예외 범위가 넓다.
            last_error = e
            if not _handle_gemini_retry(e, attempt, max_retries, "Gemini"):
                break

    logger.error("Gemini 최대 재시도 초과.")
    if last_error is not None and isinstance(last_error, google_exceptions.ResourceExhausted):
        raise LLMQuotaExceededError("Gemini quota/rate limit 초과") from last_error
    if last_error is not None:
        raise LLMCallError("Gemini 최대 재시도 초과") from last_error
    raise LLMCallError("Gemini 호출 실패")


def _call_gemini_vision(
    image_b64: str,
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    model: str | None = None,
) -> str:
    """레거시 Gemini API Vision 호출."""
    _, types, google_exceptions = _get_gemini_modules()
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
    api_config = types.GenerateContentConfig(
        max_output_tokens=config.MAX_TOKENS,
        temperature=float(getattr(config, "LLM_TEMPERATURE", 0.0)),
        response_mime_type="application/json",
    )
    client = _gemini_client()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model or config.MODEL,
                contents=contents,
                config=api_config,
            )
            return response.text
        except Exception as e:  # noqa: BLE001 - SDK 예외 범위가 넓다.
            last_error = e
            if not _handle_gemini_retry(e, attempt, max_retries, "Vision"):
                break

    logger.error("Vision 최대 재시도 초과.")
    if last_error is not None and isinstance(last_error, google_exceptions.ResourceExhausted):
        raise LLMQuotaExceededError("Gemini Vision quota/rate limit 초과") from last_error
    if last_error is not None:
        raise LLMCallError("Gemini Vision 최대 재시도 초과") from last_error
    raise LLMCallError("Gemini Vision 호출 실패")


def _call_gemini_vision_batch(
    images_b64: Sequence[str],
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    model: str | None = None,
) -> str:
    """Gemini API에 여러 이미지를 한 요청의 inline image parts로 전달한다."""
    _, types, google_exceptions = _get_gemini_modules()
    image_parts = [
        types.Part.from_bytes(data=base64.b64decode(image), mime_type="image/png")
        for image in images_b64
    ]
    full_prompt = f"[지침]\n{system}\n\n[요청]\n{prompt}" if system else prompt
    contents = [
        types.Content(
            role="user",
            parts=[*image_parts, types.Part.from_text(text=full_prompt)],
        )
    ]
    api_config = types.GenerateContentConfig(
        max_output_tokens=config.MAX_TOKENS,
        temperature=float(getattr(config, "LLM_TEMPERATURE", 0.0)),
        response_mime_type="application/json",
    )
    client = _gemini_client()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model or config.MODEL,
                contents=contents,
                config=api_config,
            )
            return response.text
        except Exception as e:  # noqa: BLE001 - SDK 예외 범위가 넓다.
            last_error = e
            if not _handle_gemini_retry(e, attempt, max_retries, "Vision batch"):
                break

    logger.error("Vision batch 최대 재시도 초과.")
    if last_error is not None and isinstance(last_error, google_exceptions.ResourceExhausted):
        raise LLMQuotaExceededError("Gemini Vision batch quota/rate limit 초과") from last_error
    if last_error is not None:
        raise LLMCallError("Gemini Vision batch 최대 재시도 초과") from last_error
    raise LLMCallError("Gemini Vision batch 호출 실패")


def _find_json_end(text: str, start: int) -> int:
    """
    start 위치의 JSON 객체/배열 시작 문자에서 닫는 위치를 반환한다.
    괄호 깊이를 추적하므로 후행 쓰레기에 영향받지 않는다.
    """
    opener = text[start]
    closer = "}" if opener == "{" else "]"
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
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _strip_code_block(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json(text: str) -> Any:
    """
    로컬 에이전트/LLM 응답 파싱.

    JSON만 출력하도록 요청하지만, 안전을 위해 코드블록 제거와 앞뒤 설명 제거를 처리한다.
    객체뿐 아니라 배열 최상위 JSON도 허용한다.
    """
    if not text or text.strip() in ("{}", ""):
        return {}

    text = _strip_code_block(text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [idx for idx, ch in enumerate(text) if ch in "{["]
        for start in starts:
            end = _find_json_end(text, start)
            if end >= 0:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
            end_fallback = max(text.rfind("}"), text.rfind("]")) + 1
            if end_fallback > start:
                try:
                    return json.loads(text[start:end_fallback])
                except json.JSONDecodeError:
                    continue

    logger.warning("JSON 파싱 실패 (응답 앞 200자): %s", text[:200])
    return {}

_JSON_RETRY_SUFFIX = "\n\n[재요청] 직전 응답이 유효한 JSON이 아니었습니다. 설명·코드블록 없이 유효한 JSON만 다시 출력하세요."


def _json_parse_ok(raw_text: str, parsed: Any) -> bool:
    stripped = (raw_text or "").strip()
    if not stripped:
        return False
    if isinstance(parsed, (dict, list)) and parsed:
        return True
    body = _strip_code_block(stripped)
    if parsed == {} and body == "{}":
        return True
    if parsed == [] and body == "[]":
        return True
    return False


def call_text_json(
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> tuple[Any, bool]:
    """
    call_text 후 JSON 파싱까지 수행한다.

    JSON 파싱 실패가 의심되면 같은 프롬프트에 재요청 지시문을 덧붙여 1회 재호출한다.
    반환값은 (파싱 결과, 파싱 성공 여부)다.
    """
    raw = call_text(prompt, system=system, max_retries=max_retries, stage=stage)
    parsed = parse_json(raw)
    if _json_parse_ok(raw, parsed):
        return parsed, True
    retry_raw = call_text(f"{prompt}{_JSON_RETRY_SUFFIX}", system=system, max_retries=max_retries, stage=stage)
    retry_parsed = parse_json(retry_raw)
    return retry_parsed, _json_parse_ok(retry_raw, retry_parsed)


def call_vision_json(
    image_b64: str,
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> tuple[Any, bool]:
    """call_vision 후 JSON 파싱 실패 시 1회 재요청한다."""
    raw = call_vision(image_b64, prompt, system=system, max_retries=max_retries, stage=stage)
    parsed = parse_json(raw)
    if _json_parse_ok(raw, parsed):
        return parsed, True
    retry_raw = call_vision(image_b64, f"{prompt}{_JSON_RETRY_SUFFIX}", system=system, max_retries=max_retries, stage=stage)
    retry_parsed = parse_json(retry_raw)
    return retry_parsed, _json_parse_ok(retry_raw, retry_parsed)


def call_vision_batch_json(
    images_b64: Sequence[str],
    prompt: str,
    system: str = "",
    max_retries: int = config.MAX_RETRIES,
    stage: str | None = None,
) -> tuple[Any, bool]:
    """call_vision_batch 후 JSON 파싱 실패 시 1회 재요청한다."""
    raw = call_vision_batch(images_b64, prompt, system=system, max_retries=max_retries, stage=stage)
    parsed = parse_json(raw)
    if _json_parse_ok(raw, parsed):
        return parsed, True
    retry_raw = call_vision_batch(images_b64, f"{prompt}{_JSON_RETRY_SUFFIX}", system=system, max_retries=max_retries, stage=stage)
    retry_parsed = parse_json(retry_raw)
    return retry_parsed, _json_parse_ok(retry_raw, retry_parsed)
