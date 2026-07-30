"""골든셋 채점 결과를 Markdown으로 렌더링한다."""
from __future__ import annotations

from typing import Any


def _비율문자(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _표행(cells: list[Any]) -> str:
    return "| " + " | ".join(_비율문자(cell) for cell in cells) + " |\n"


def 마크다운(result: dict[str, Any]) -> str:
    lines = [
        f"# 골든셋 채점 리포트 ({result['라벨']})\n\n",
        "## 실행 정보\n\n",
        f"- 파이프라인 출력: `{result['파이프라인출력']}`\n",
        f"- 골든셋: `{result['골든셋']}`\n",
        f"- 실행시각: {result['실행시각']}\n",
        f"- 채점제외 행 수: {result['채점제외행수']}\n",
        f"- 형식 오류 생략 시트: {', '.join(result['생략시트']) if result['생략시트'] else '없음'}\n",
        f"- 형식 경고: {len(result.get('형식경고', []))}건\n",
        f"- 의미완화 매칭: {result['의미완화매칭수']}건(02_지역여건)\n",
        f"- 문자유사 매칭: {result['문자유사매칭수']}건(02_지역여건)\n",
        f"- 스케일동치 값 쌍: {result['스케일동치쌍수']}건(06_감축목표)\n",
        "- 값일치율 정의: 양쪽 값이 모두 존재하는 비교 쌍 중 일치한 값의 비율입니다.\n\n",
        "## 독립 평가 지표\n\n",
        f"- 셀 정확도(골든 누락 셀 포함): {_비율문자(result.get('독립평가지표', {}).get('cell_accuracy'))}\n",
        f"- 행 리콜: {_비율문자(result.get('독립평가지표', {}).get('row_recall'))}\n",
        f"- 행 정밀도: {_비율문자(result.get('독립평가지표', {}).get('row_precision'))}\n\n",
        "## 시트별 표\n\n",
        "| 시트 | 상태 | 골든 행수 | 출력 행수 | 매칭 수 | 엄격 | 완화 | 리콜 | 정밀도 | 값일치율 | 값일치율(스케일동치 포함) | 스케일동치 쌍 | 골든만 있는 값 | 출력만 있는 값 | 의미완화 | 문자유사 |\n",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n",
    ]
    for sheet_name, sheet in result["시트별"].items():
        lines.append(_표행([sheet_name, sheet["상태"], sheet["골든행수"], sheet["출력행수"], sheet["매칭수"], sheet["엄격매칭수"], sheet["완화매칭수"], sheet["리콜"], sheet["정밀도"], sheet["값일치율"], sheet["값일치율(스케일동치 포함)"], sheet["스케일동치쌍수"], sheet["골든만있는값"], sheet["출력만있는값"], sheet["의미완화매칭수"], sheet["문자유사매칭수"]]))
    for title, key in (("출처유형별 분해 — 전체", "출처유형별_전체"), ("출처유형별 분해 — 수치 시트", "출처유형별_수치시트")):
        lines.extend([f"\n## {title}\n\n", "| 구분 | 골든 행수 | 매칭 수 | 리콜 | 값비교수 | 값일치율 |\n", "|---|---:|---:|---:|---:|---:|\n"])
        for name, stat in result[key].items():
            lines.append(_표행([name, stat["골든행수"], stat["매칭수"], stat["리콜"], stat["값비교수"], stat["값일치율"]]))
        inclusive = result[f"{key}_의미완화포함"]
        for name in ("텍스트 유래", "시각 유래"):
            stat = inclusive[name]
            lines.append(_표행([f"{name}(의미완화 포함)", stat["골든행수"], stat["매칭수"], stat["리콜"], stat["값비교수"], stat["값일치율"]]))
        char_inclusive = result[f"{key}_문자유사포함"]["시각 유래"]
        lines.append(_표행(["시각 유래(문자유사 포함)", char_inclusive["골든행수"], char_inclusive["매칭수"], char_inclusive["리콜"], char_inclusive["값비교수"], char_inclusive["값일치율"]]))
    lines.extend(["\n## 미매칭 상세\n\n", "### 골든 미매칭\n\n"])
    for item in result["미매칭상세"]["골든"]:
        lines.append(f"- {item['시트']} {item['행번호']}행 | 키={item['키']} | 출처={item.get('골든_출처유형', '')} | 페이지={item.get('골든_출처페이지', '')}\n")
    lines.append("\n### 출력 미매칭\n\n")
    for item in result["미매칭상세"]["출력"]:
        lines.append(f"- {item['시트']} {item['행번호']}행 | 키={item['키']}\n")
    lines.append("\n## 값 불일치 상세\n\n")
    for item in result["값불일치상세"]:
        lines.append(f"- {item['시트']} | {item['키']} | {item['필드']}: 골든={item['골든값']} / 출력={item['출력값']} ({item['매칭방식']})\n")
    lines.append("\n## 의미완화 매칭 쌍\n\n")
    for item in result["의미완화매칭쌍"]:
        golden = item["골든"]
        output = item["출력"]
        lines.append(
            f"- 골든({golden['지표범주']}/{golden['지표세부범주']}/{golden['지표명']}/{golden['연도']}/{golden['값']}) "
            f"⇔ 출력({output['지표범주']}/{output['지표세부범주']}/{output['지표명']}/{output['값']})\n"
        )
    lines.append("\n## 문자유사 매칭 쌍\n\n")
    for item in result["문자유사매칭쌍"]:
        golden = item["골든"]
        output = item["출력"]
        lines.append(
            f"- {item['유사도']} | "
            f"골든({golden['지표범주']}/{golden['지표세부범주']}/{golden['지표명']}/{golden['연도']}/{golden['값']}/{golden['단위']}) "
            f"⇔ 출력({output['지표범주']}/{output['지표세부범주']}/{output['지표명']}/{output['연도']}/{output['값']}/{output['단위']})\n"
        )
    lines.append("\n## 문자유사 관찰(비매칭)\n\n")
    for item in result["문자유사관찰쌍"]:
        golden = item["골든"]
        output = item["출력"]
        lines.append(
            f"- {item['유사도']} | "
            f"골든({golden['지표범주']}/{golden['지표세부범주']}/{golden['지표명']}/{golden['연도']}/{golden['값']}/{golden['단위']}) "
            f"⇔ 출력({output['지표범주']}/{output['지표세부범주']}/{output['지표명']}/{output['연도']}/{output['값']}/{output['단위']})\n"
        )
    lines.append("\n## 스케일동치 값 쌍\n\n")
    for item in result["스케일동치쌍"]:
        lines.append(
            f"- {item['시트']} | {item['키']} | {item['필드']}: "
            f"골든={item['골든값']} / 출력={item['출력값']} (배율={item['배율']})\n"
        )
    lines.extend([
        "\n## 출처페이지 참고 통계\n\n",
        f"- 비교 가능한 매칭 쌍: {result['출처페이지통계']['비교쌍수']}\n",
        f"- 교집합 있음: {result['출처페이지통계']['교집합수']}\n",
        f"- 교집합 비율: {_비율문자(result['출처페이지통계']['교집합비율'])}\n",
    ])
    if result["형식오류"]:
        lines.append("\n## 형식 오류\n\n")
        for error in result["형식오류"]:
            lines.append(f"- {error}\n")
    if result.get("형식경고"):
        lines.append("\n## 형식 경고\n\n")
        for warning in result["형식경고"]:
            lines.append(f"- {warning}\n")
    return "".join(lines)
