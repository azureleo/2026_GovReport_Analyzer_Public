"""2차 판독용 단일 Codex 호출. 자동 재시도/모델 대체 없이 프로세스 트리까지 제한."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile


def _codex_command(executable=None):
    path = Path(executable or shutil.which("codex") or "")
    if not path.is_file():
        raise ValueError("Codex 실행 파일을 찾지 못했습니다. --codex-command 절대경로를 지정하세요.")
    path = path.resolve()
    if path.suffix.lower() in {".cmd", ".bat"}:
        # npm shim을 cmd 문자열로 조립하지 않고 설치된 JS 진입점을 직접 실행한다.
        script = path.parent / "node_modules/@openai/codex/bin/codex.js"
        node = path.parent / "node.exe"
        if not node.is_file():
            node = Path(shutil.which("node") or "")
        if not script.is_file() or not node.is_file():
            raise ValueError("npm Codex shim의 Node/JS 경로를 확인할 수 없습니다.")
        return [str(node.resolve()), str(script.resolve())]
    return [str(path)]


def terminate_tree(process):
    if os.name == "nt":
        killer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def bounded_process(command, prompt, *, cwd, timeout):
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=str(cwd), **kwargs)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        terminate_tree(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        raise
    if process.returncode:
        raise RuntimeError(f"Codex exit={process.returncode}: {stderr[-1600:]}")
    return stdout


class CodexReader:
    def __init__(self, model, workspace, executable=None):
        self.model = model
        self.workspace = Path(workspace).resolve()
        self.command = _codex_command(executable)

    def __call__(self, prompt, image_path, timeout, group):
        with tempfile.TemporaryDirectory(prefix="carbon-second-pass-") as folder:
            # 검토 목록/기존 Excel이 없는 임시 요청 폴더에서 실행한다.
            # CLI의 read-only는 파일 접근 차단의 보안 경계라는 뜻은 아니다.
            request_dir = Path(folder)
            image = request_dir / "source_table.png"
            shutil.copyfile(image_path, image)
            last = Path(folder) / "last_message.txt"
            command = self.command + ["exec", "--sandbox", "read-only", "--cd", str(request_dir),
                "--skip-git-repo-check",
                "--ephemeral", "--color", "never", "--model", self.model,
                "--output-last-message", str(last), "--image", str(image), "-"]
            stdout = bounded_process(command, prompt, cwd=request_dir, timeout=timeout)
            text = last.read_text(encoding="utf-8") if last.is_file() else stdout
            if len(text) > 2_000_000:
                raise ValueError("2차 응답 크기 상한 초과")
            return text
