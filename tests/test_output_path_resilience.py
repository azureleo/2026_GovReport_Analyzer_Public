from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
from openpyxl import load_workbook

import config
import main
from utils import excel_writer


def test_main_creates_missing_output_parent_before_supervisor(monkeypatch, tmp_path, capsys) -> None:
    # Given: 출력 부모 디렉터리가 아직 없는 CLI 실행
    input_path = tmp_path / "input.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    output_path = tmp_path / "missing" / "out.xlsx"

    class FakeSupervisor:
        last_run_outcome = {"status": "complete", "exit_code": 0, "file_saved": True}
        def run(self, *, input_path, output_path, hwp_path, max_pipeline_retries, include_images):
            Path(output_path).write_text("saved", encoding="utf-8")
            return Path(output_path)

    monkeypatch.setattr(config, "LLM_PROVIDER", "codex")
    monkeypatch.setattr(sys, "argv", ["main.py", str(input_path), "-o", str(output_path), "--agent", "codex", "--no-images"])
    monkeypatch.setattr("agents.supervisor.Supervisor", FakeSupervisor)

    # When: main이 파이프라인을 시작하면
    exit_code = main.main()

    # Then: Supervisor 진입 전에 부모를 만들고 정상 저장까지 진행한다.
    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == "saved"
    assert "결과 파일 저장" in capsys.readouterr().out


def test_main_rejects_unwritable_output_parent_before_supervisor(monkeypatch, tmp_path, capsys) -> None:
    # Given: 출력 경로의 부모가 디렉터리가 아니라 파일인 CLI 실행
    input_path = tmp_path / "input.pdf"
    input_path.write_bytes(b"%PDF-1.4\n")
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    output_path = blocked_parent / "out.xlsx"
    supervisor_called = False

    class FakeSupervisor:
        def __init__(self):
            nonlocal supervisor_called
            supervisor_called = True

    monkeypatch.setattr(config, "LLM_PROVIDER", "codex")
    monkeypatch.setattr(sys, "argv", ["main.py", str(input_path), "-o", str(output_path), "--agent", "codex"])
    monkeypatch.setattr("agents.supervisor.Supervisor", FakeSupervisor)

    # When: main이 출력 경로를 사전 검증하면
    exit_code = main.main()

    # Then: Supervisor/LLM 단계 전에 한국어 오류와 exit 1로 중단한다.
    captured = capsys.readouterr().out
    assert exit_code == 1
    assert not supervisor_called
    assert "출력 경로를 쓸 수 없습니다" in captured


def test_write_excel_falls_back_when_first_save_fails(monkeypatch, tmp_path, capsys) -> None:
    # Given: 원본 저장 경로가 잠겨 첫 wb.save가 PermissionError를 내는 상황
    output_path = tmp_path / "result.xlsx"
    original_save = openpyxl.workbook.workbook.Workbook.save
    saved_paths: list[Path] = []

    class FixedDatetime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, fmt: str) -> str:
            return "20260705_121314"

    def flaky_save(self, filename) -> None:
        path = Path(filename)
        saved_paths.append(path)
        if len(saved_paths) == 1:
            raise PermissionError("locked")
        original_save(self, filename)

    monkeypatch.setattr(excel_writer, "datetime", FixedDatetime)
    monkeypatch.setattr(openpyxl.workbook.workbook.Workbook, "save", flaky_save)

    # When: 엑셀 저장을 수행하면
    result_path = excel_writer.write_excel(
        {"document_meta": [{"지자체명": "서울특별시", "계획명": "계획"}]},
        output_path,
    )

    # Then: 1회 대체 경로로 재저장하고 해당 경로를 반환한다.
    fallback_path = tmp_path / "result_20260705_121314.xlsx"
    assert saved_paths == [output_path, fallback_path]
    assert result_path == fallback_path
    assert fallback_path.exists()
    assert "대체 경로에 저장했습니다" in capsys.readouterr().out
    workbook = load_workbook(fallback_path)
    assert workbook["00_문서메타"]["A2"].value == "서울특별시"
