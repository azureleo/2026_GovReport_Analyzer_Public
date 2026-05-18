"""
에이전트 4: 엑셀 작성 에이전트

정제된 데이터를 받아 탄소_출력파일.xlsx 와 동일한 5-시트 구조의
엑셀 파일을 생성합니다.
"""

from pathlib import Path
from utils.excel_writer import write_excel


class ExcelAgent:
    """
    에이전트 4: 엑셀 작성 에이전트
    """

    def __init__(self):
        self._output_path: Path | None = None

    def write(self, excel_ready_data: dict, output_path: str | Path) -> Path:
        """
        Args:
            excel_ready_data: 에이전트3에서 반환한 엑셀 준비 데이터
            output_path: 저장 경로

        Returns:
            저장된 파일의 Path 객체
        """
        output_path = Path(output_path)
        print(f"[에이전트4 엑셀작성] 엑셀 파일 생성 중: {output_path}")

        counts = {k: len(v) for k, v in excel_ready_data.items() if isinstance(v, list)}
        print(f"  - 작성 데이터: {counts}")

        self._output_path = write_excel(excel_ready_data, output_path)
        print(f"[에이전트4 엑셀작성] 저장 완료: {self._output_path}")
        return self._output_path

    def report(self) -> str:
        if self._output_path and self._output_path.exists():
            size_kb = self._output_path.stat().st_size / 1024
            return (
                f"[에이전트4 엑셀작성] 완료\n"
                f"  - 출력 파일: {self._output_path}\n"
                f"  - 파일 크기: {size_kb:.1f} KB"
            )
        return "[에이전트4 엑셀작성] 파일 미생성"
