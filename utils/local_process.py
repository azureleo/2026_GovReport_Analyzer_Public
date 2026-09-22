"""Bounded CLI execution with per-invocation process-tree ownership.

Windows CLI launchers (.cmd -> node -> executable) must be terminated together.
A gated Python helper joins a kill-on-close Job before it can start the CLI.
File-backed output avoids waiting forever for inherited stdout/stderr pipes.
This module is also the helper entry point; keep it standard-library only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

logger = logging.getLogger(__name__)
_CLEANUP_TIMEOUT = 5


class _WindowsJob:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io_counters", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 4),
                ("page_faults", wintypes.DWORD),
                ("total_processes", wintypes.DWORD),
                ("active_processes", wintypes.DWORD),
                ("terminated_processes", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._accounting_type = Accounting
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self._api, name)
            function.argtypes = args
            function.restype = result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        # The helper is blocked on its gate, so it cannot spawn before assignment.
        if not self._api.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self, *, deadline: float | None = None) -> None:
        if self._handle:
            # Termination is asynchronous. Wait (bounded) for all owned children
            # to exit before deleting prompt/image files or starting a retry.
            if deadline is None:
                deadline = time.monotonic() + _CLEANUP_TIMEOUT
            try:
                if not self._api.TerminateJobObject(self._handle, 1):
                    raise self._ctypes.WinError(self._ctypes.get_last_error())
                while True:
                    info = self._accounting_type()
                    if not self._api.QueryInformationJobObject(
                        self._handle, 1, self._ctypes.byref(info), self._ctypes.sizeof(info), None
                    ):
                        raise self._ctypes.WinError(self._ctypes.get_last_error())
                    if not info.active_processes:
                        break
                    if time.monotonic() >= deadline:
                        logger.error("CLI job cleanup exceeded %ss (%s processes)", _CLEANUP_TIMEOUT, info.active_processes)
                        break
                    time.sleep(0.01)
            finally:
                self._api.CloseHandle(self._handle)
                self._handle = None


def _cleanup(process: subprocess.Popen | None, job: _WindowsJob | None) -> None:
    deadline = time.monotonic() + _CLEANUP_TIMEOUT
    try:
        if job is not None:
            job.close(deadline=deadline)
        elif process is not None and os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        if process is not None:
            # Assignment failure is fail-closed: only the gated helper exists.
            if process.poll() is None:
                process.kill()
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.error("CLI process cleanup did not finish within %ss (pid=%s)", _CLEANUP_TIMEOUT, process.pid)


def run_local_command(
    command: list[str], prompt: str, *, cwd: Path, timeout: float,
) -> subprocess.CompletedProcess:
    """Return UTF-8 output or raise TimeoutExpired after bounded tree cleanup.

    No unbounded communicate()/Popen context-manager wait is used. Jobs/groups
    contain only this call's children, never unrelated Codex or Node processes.
    """
    with tempfile.TemporaryDirectory(prefix="carbon-process-") as directory:
        input_path = Path(directory) / "stdin.txt"
        input_path.write_bytes(prompt.encode("utf-8"))
        with input_path.open("rb") as input_file, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = None
            job = None
            timed_out = False
            try:
                if os.name == "nt":
                    job = _WindowsJob()
                    process = subprocess.Popen(
                        [sys.executable, "-I", str(Path(__file__).resolve()), str(input_path), *command],
                        stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                        cwd=str(cwd), creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    job.assign(process)
                    process.stdin.write(b"1")
                    process.stdin.close()
                else:
                    process = subprocess.Popen(
                        command, stdin=input_file, stdout=stdout, stderr=stderr,
                        cwd=str(cwd), start_new_session=True,
                    )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    logger.warning("CLI timeout after %ss; terminating owned process tree (pid=%s)", timeout, process.pid)
            finally:
                _cleanup(process, job)
            stdout.seek(0)
            stderr.seek(0)
            output, errors = stdout.read(), stderr.read()
            if timed_out:
                raise subprocess.TimeoutExpired(command, timeout, output=output, stderr=errors)
            return subprocess.CompletedProcess(
                command, process.returncode,
                output.decode("utf-8", errors="replace"),
                errors.decode("utf-8", errors="replace"),
            )


def _gated_cli() -> int:
    # EOF/cancel before assignment must never launch the real command.
    if sys.stdin.buffer.read(1) != b"1":
        return 125
    with open(sys.argv[1], "rb") as prompt:
        payload = prompt.read()
    # Do not let every descendant inherit an open prompt file. Keep the CLI's
    # original pipe-based stdin contract; only this disposable helper writes it.
    return subprocess.run(sys.argv[2:], input=payload, check=False).returncode


if __name__ == "__main__":
    sys.exit(_gated_cli())
