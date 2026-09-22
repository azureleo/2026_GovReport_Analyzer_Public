import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from utils import local_process, llm_client


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Reparented zombies are terminated, even if init has not reaped them yet.
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split(") ", 1)[1].startswith("Z"))


def _assert_stopped(pids):
    deadline = time.monotonic() + 5
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [pid for pid in pids if _alive(pid)]


@pytest.mark.parametrize("parent_exits", [False, True])
def test_timeout_cleans_descendants_even_after_intermediate_parent_exits(tmp_path, parent_exits):
    # The grandchild inherits output handles. Old subprocess.run would leave it
    # alive (and on Windows wait indefinitely on its captured output pipes).
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path('grandchild.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, 'grandchild.py'])\n"
        + ("" if parent_exits else "time.sleep(30)\n"), encoding="utf-8",
    )
    root = tmp_path / "root.py"
    root.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "pathlib.Path('root.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8",
    )
    # An unrelated process must survive this invocation's timeout.
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            local_process.run_local_command([sys.executable, str(root)], "test", cwd=tmp_path, timeout=2)
        assert time.monotonic() - started < 9
        assert b"started" in caught.value.output
        assert caught.value.timeout == 2
        pids = [int((tmp_path / name).read_text()) for name in ("root.pid", "child.pid", "grandchild.pid")]
        _assert_stopped(pids)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_success_cleans_background_descendants(tmp_path):
    script = tmp_path / "root.py"
    script.write_text(
        "import pathlib, subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path('child.pid').write_text(str(p.pid))\n"
        "print('done')\n", encoding="utf-8",
    )
    result = local_process.run_local_command([sys.executable, str(script)], "", cwd=tmp_path, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    _assert_stopped([int((tmp_path / "child.pid").read_text())])


def test_large_prompt_and_output_do_not_deadlock(tmp_path):
    prompt = "서울특별시\n" * 50000
    result = local_process.run_local_command(
        [sys.executable, "-c", "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'x'*200000)"],
        prompt, cwd=tmp_path, timeout=10,
    )
    assert result.stdout == prompt
    assert len(result.stderr) == 200000


@pytest.mark.parametrize("message,error", [
    ("selected model is at capacity", llm_client.LLMCapacityError),
    ("quota exceeded", llm_client.LLMQuotaExceededError),
    ("unexpected failure", llm_client.LLMCallError),
])
def test_exit_code_classification_is_preserved(tmp_path, message, error):
    with pytest.raises(error, match=message):
        llm_client._run_command(
            [sys.executable, "-c", f"import sys; print({message!r}, file=sys.stderr); sys.exit(7)"],
            "", cwd=tmp_path, timeout=5,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job fail-closed path")
def test_assignment_failure_never_launches_real_command(tmp_path, monkeypatch):
    def refuse(self, process):
        raise OSError("assignment denied")
    monkeypatch.setattr(local_process._WindowsJob, "assign", refuse)
    with pytest.raises(OSError, match="assignment denied"):
        local_process.run_local_command(
            [sys.executable, "-c", "from pathlib import Path; Path('launched').touch()"],
            "", cwd=tmp_path, timeout=5,
        )
    assert not (tmp_path / "launched").exists()


def test_timeout_still_obeys_existing_retry_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client.config, "LOCAL_AGENT_TIMEOUT_RETRIES", 1)
    monkeypatch.setattr(llm_client.config, "LOCAL_AGENT_TIMEOUT_RETRY_DELAY_SECONDS", 0)
    calls = []
    def invoke():
        calls.append(1)
        return llm_client._run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], "", cwd=tmp_path, timeout=0.2,
        )
    with pytest.raises(llm_client.LLMTimeoutError, match="2회"):
        llm_client._retry_local_call(invoke, max_retries=5, label="test")
    assert len(calls) == 2


def test_timeout_is_bounded_when_cli_never_reads_large_stdin(tmp_path):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        local_process.run_local_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "x" * 2000000, cwd=tmp_path, timeout=0.5,
        )
    assert time.monotonic() - started < 7


@pytest.mark.skipif(os.name != "nt", reason="Windows .cmd launcher contract")
def test_cmd_launcher_preserves_stdin_and_space_containing_arguments(tmp_path):
    launcher = tmp_path / "fake launcher.cmd"
    launcher.write_text(
        f'@echo off\n"{sys.executable}" "%~dp0cli.py" %*\n', encoding="utf-8",
    )
    (tmp_path / "cli.py").write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['--model', 'test-model', '--image', 'image with spaces.png', '-']\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8",
    )
    result = local_process.run_local_command(
        [str(launcher), "--model", "test-model", "--image", "image with spaces.png", "-"],
        "서울특별시", cwd=tmp_path, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "서울특별시"


def test_interrupt_cleans_owned_process_before_propagating(tmp_path, monkeypatch):
    original_wait = local_process.subprocess.Popen.wait
    processes = []
    def interrupt_once(process, *args, **kwargs):
        if not processes:
            processes.append(process)
            raise KeyboardInterrupt
        return original_wait(process, *args, **kwargs)
    monkeypatch.setattr(local_process.subprocess.Popen, "wait", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        local_process.run_local_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], "", cwd=tmp_path, timeout=5,
        )
    assert processes[0].poll() is not None
