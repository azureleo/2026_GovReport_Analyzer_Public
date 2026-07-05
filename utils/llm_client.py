"""
LLM/로컬 에이전트 공통 래퍼.

기본 실행 경로는 Gemini API가 아니라 로컬 에이전트 CLI입니다.
- call_text()  : 텍스트 프롬프트를 Codex/Claude Code 같은 로컬 에이전트에 전달
- call_vision(): 이미지 파일을 임시로 저장한 뒤 로컬 에이전트에 첨부/경로 전달
- call_vision_batch(): 여러 이미지를 한 번에 첨부해 전수 분석 호출 수를 줄임
- parse_json() : 에이전트 응답에서 JSON만 안전하게 파싱

환경변수/CLI(main.py)로 선택 가능한 백엔드:
- LLM_PROVIDER=codex  : `codex exec` 사용 (기본값)
- LLM_PROVIDER=claude : `claude -p` 사용
- LLM_PROVIDER=auto   : codex → claude → gemini 순으로 사용 가능한 백엔드 선택
- LLM_PROVIDER=gemini : 기존 Gemini API 백엔드(명시 선택 시에만)
"""

from __future__ import annotations

import base64
import json
import logging
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
    tokens = shlex.split(command or "")
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
    }
    provider = aliases.get(provider, provider)

    if provider == "auto":
        if _command_exists(getattr(config, "CODEX_COMMAND", "codex")):
            return "codex"
        if _command_exists(getattr(config, "CLAUDE_COMMAND", "claude")):
            return "claude"
        if getattr(config, "GEMINI_API_KEY", ""):
            return "gemini"
        raise RuntimeError(
            "사용 가능한 로컬 에이전트를 찾지 못했습니다. "
            "Codex CLI 또는 Claude Code를 설치하거나 LLM_PROVIDER를 명시하세요."
        )

    if provider == "codex" and not _command_exists(getattr(config, "CODEX_COMMAND", "codex")):
        raise RuntimeError("Codex CLI를 찾을 수 없습니다. CODEX_COMMAND 또는 --agent claude를 설정하세요.")
    if provider == "claude" and not _command_exists(getattr(config, "CLAUDE_COMMAND", "claude")):
        raise RuntimeError("Claude Code CLI를 찾을 수 없습니다. CLAUDE_COMMAND 또는 --agent codex를 설정하세요.")
    if provider == "gemini" and not getattr(config, "GEMINI_API_KEY", ""):
        raise RuntimeError("Gemini 백엔드를 사용하려면 GEMINI_API_KEY가 필요합니다.")
    if provider not in {"codex", "claude", "gemini"}:
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


def _stage_model(stage: str | None) -> str:
    return _stage_config_value("STAGE_MODELS", stage)


def _model_identity(provider: str, stage: str | None = None) -> str:
    stage_model = _stage_model(stage)
    if provider == "gemini":
        return stage_model or str(getattr(config, "MODEL", ""))
    if provider == "codex":
        return ":".join([
            str(getattr(config, "CODEX_COMMAND", "codex")),
            stage_model or str(getattr(config, "LOCAL_AGENT_MODEL", "")),
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
    timeout = int(getattr(config, "LOCAL_AGENT_TIMEOUT", 900))
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
    timeout = int(getattr(config, "LOCAL_AGENT_TIMEOUT", 900))
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
    with tempfile.TemporaryDirectory(prefix="carbon-agent-") as workdir_name:
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
    consecutive_timeouts = 0
    timeout_threshold = int(getattr(config, "LLM_TIMEOUT_AS_QUOTA_THRESHOLD", 2))
    while True:
        attempt += 1
        try:
            result = fn()
            consecutive_timeouts = 0
            return result
        except subprocess.TimeoutExpired as exc:
            consecutive_timeouts += 1
            _inc_stat("timeouts")
            wait_enabled = getattr(config, "LLM_QUOTA_WAIT_ENABLED", True)
            # 연속 타임아웃이 임계값 이상이면 throttling으로 보고 quota처럼 대기-재개한다.
            if wait_enabled and consecutive_timeouts >= timeout_threshold:
                poll = int(getattr(config, "LLM_QUOTA_WAIT_POLL_SECONDS", 600))
                cap = int(getattr(config, "LLM_QUOTA_WAIT_MAX_SECONDS", 21600))
                if quota_waited + poll > cap:
                    logger.error(
                        "%s 반복 타임아웃 대기 누적 %s 가 상한 %s 초과. 중단합니다.",
                        label, _fmt_duration(quota_waited), _fmt_duration(cap),
                    )
                    raise LLMCallError(f"{label} 반복 타임아웃(throttling 추정) 상한 초과") from exc
                quota_waited += poll
                _inc_stat("retries")
                attempt -= 1  # throttling 대기는 일반 재시도 예산을 소모하지 않는다.
                logger.warning(
                    "%s 연속 %s회 타임아웃 → throttling 추정. %s 후 자동 재개(누적 대기 %s).",
                    label, consecutive_timeouts, _fmt_duration(poll), _fmt_duration(quota_waited),
                )
                _sleep_with_heartbeat(poll, label)
            elif attempt >= max_retries:
                logger.error("%s 타임아웃 최대 재시도 초과: %s", label, exc)
                raise LLMCallError(f"{label} 타임아웃 최대 재시도 초과") from exc
            else:
                wait = min(5 * attempt, 30)
                logger.warning("%s 타임아웃. %s초 후 재시도 (%s/%s)", label, wait, attempt, max_retries)
                _inc_stat("retries")
                time.sleep(wait)
        except LLMQuotaExceededError as exc:
            consecutive_timeouts = 0
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
            consecutive_timeouts = 0  # 비-타임아웃 오류는 연속 타임아웃 카운트를 끊는다.
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
        def produce_gemini_vision() -> str:
            if stage_model:
                return _call_gemini_vision(image_b64, prompt, system, max_retries=max_retries, model=stage_model)
            return _call_gemini_vision(image_b64, prompt, system, max_retries=max_retries)

        return cached_response(
            request,
            lambda: _record_call("vision", provider, produce_gemini_vision),
        )

    return cached_response(
        request,
        lambda: _record_call(
            "vision",
            provider,
            lambda: _retry_local_call(
                lambda: _call_local_agent(prompt, system, image_b64=image_b64, provider=provider, model=stage_model or None),
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
        def produce_gemini_vision_batch() -> str:
            if stage_model:
                return _call_gemini_vision_batch(images_b64, prompt, system, max_retries=max_retries, model=stage_model)
            return _call_gemini_vision_batch(images_b64, prompt, system, max_retries=max_retries)

        return cached_response(
            request,
            lambda: _record_call("vision_batch", provider, produce_gemini_vision_batch),
        )

    return cached_response(
        request,
        lambda: _record_call(
            "vision_batch",
            provider,
            lambda: _retry_local_call(
                lambda: _call_local_agent(prompt, system, images_b64=images_b64, provider=provider, model=stage_model or None),
                max_retries=max_retries,
                label=f"{provider} vision batch",
            ),
        ),
    )


def _get_gemini_modules():
    """Gemini 백엔드는 명시적으로 선택된 경우에만 SDK를 지연 import한다."""
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        from google.api_core import exceptions as google_exceptions  # type: ignore
    except ImportError as exc:  # pragma: no cover - 선택 백엔드 미설치 환경용
        raise RuntimeError(
            "Gemini 백엔드를 사용하려면 google-genai 패키지를 별도로 설치하세요."
        ) from exc
    return genai, types, google_exceptions


def _gemini_client():
    genai, _, _ = _get_gemini_modules()
    return genai.Client(api_key=config.GEMINI_API_KEY)


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
        temperature=0.1,
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
        temperature=0.1,
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
        temperature=0.1,
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
