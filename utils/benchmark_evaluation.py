"""버전 고정 골든 평가. 모델 호출/업무 값 수정 없이 저장 Excel만 채점한다."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import time

from utils.integration_audit import document_hash, save_json
from utils.quality_adapters import canonical, norm, number, pages, present, unit_norm
from utils.quality_evaluation import money_scale, same_city

ROOT = Path(__file__).resolve().parents[1]
VERSION = "golden-v2-identity-unit-scope-1"
EXCLUDED = {"지자체명", "출처페이지", "데이터상태", "derivation_type", "근거ID"}


def _unavailable(reason, mode, *, status="unassessed", dataset=None):
    return {"status": status, "reason": reason, "mode": mode, "contract_version": VERSION,
            "dataset_id": dataset, "model_accuracy_evaluated": False,
            "metrics": {"cell_accuracy": None, "row_recall": None, "row_precision": None}}


def _scope_rows(rows, scope):
    allowed_pages = set(scope.get("pages", []))
    filters = scope.get("fields", {})
    result, unlocated = [], []
    for row in rows:
        if filters and not all(any(norm(row.값.get(f)) == norm(v) for v in values) for f, values in filters.items()):
            continue
        if allowed_pages:
            rp, error = pages(row.값.get("골든_출처페이지") or row.값.get("출처페이지"))
            if error or not rp:
                unlocated.append(row.번호)
                continue
            if not set(rp) & allowed_pages:
                continue
        result.append(row)
    return result, unlocated


IDENTITY = {
    "00": ["지자체명", "계획명"], "01": ["개요유형", "항목명"],
    "02": ["지표범주", "지표명", "연도"],
    "03": ["인벤토리출처", "배출범위", "배출유형", "부문", "세부부문", "연도"],
    "04": ["인벤토리출처", "관리부문", "세부부문", "직간접구분", "연도"],
    "05": ["전망범위", "시나리오", "부문", "세부부문", "연도"],
    "06": ["목표수준", "목표범위", "부문", "기준연도", "목표연도"],
    "07": ["전략수준", "전략명", "비전문구"],
    "08": ["관리번호", "사업명", "부문"], "09": ["관리번호", "사업명", "연도"],
    "10": ["관리번호", "사업명", "연도", "모니터링인자"],
    "11": ["계획구분", "부문", "사업명", "재원구분", "연도"],
    "12": ["과제ID", "과제명", "대응기반영역"], "13": ["거버넌스기구", "절차단계", "역할"],
    "14": ["관리번호", "사업명", "점검연도"], "15": ["관리번호", "사업명", "점검연도"],
}


def _identity_compatible(sheet, left, right, key_value):
    """식별 조건만 사용한다. 골든 숫자/단위 정답에 가까운 후보를 선택하지 않는다."""
    fields = IDENTITY[sheet[:2]]
    shared = [f for f in fields if present(left.get(f)) and present(right.get(f))]
    explicit = set()
    for identifier in ("관리번호", "과제ID"):
        if identifier in shared:
            a, b = key_value(identifier, left[identifier]), key_value(identifier, right[identifier])
            if a and b and norm(a) not in {"미상", "알수없음", "없음", "-"} and norm(b) not in {"미상", "알수없음", "없음", "-"}:
                if a != b:
                    return False
                explicit.add(identifier)
            else:
                shared.remove(identifier)
    if present(left.get("지자체명")) and present(right.get("지자체명")) and not same_city(left["지자체명"], right["지자체명"]):
        return False
    for field in shared:
        if field == "지자체명":
            continue
        if (field == "사업명" and "관리번호" in explicit) or (field == "과제명" and "과제ID" in explicit):
            continue
        if key_value(field, left[field]) != key_value(field, right[field]):
            return False
    prefix = sheet[:2]
    if prefix in {"08", "09", "10", "11", "14", "15"}:
        anchor = {"사업명", "관리번호"}.intersection(shared)
        time = "점검연도" if prefix in {"14", "15"} else "연도"
        return bool(anchor) and (prefix == "08" or time in shared)
    if prefix == "12":
        return bool({"과제명", "과제ID"}.intersection(shared))
    if prefix == "07":
        return bool({"전략명", "비전문구"}.intersection(shared))
    if prefix == "00":
        return "계획명" in shared
    # 하나의 숫자/연도만 같은 행을 대응시키지 않는다.
    return len(shared) >= 2


def _equivalent(field, golden, output, legacy_compare):
    # 명시적으로 등록된 같은 통화 차원에서만 배율을 환산한다.
    if field in {"예산액", "예산단위"}:
        a, b = money_scale(golden.get("예산단위")), money_scale(output.get("예산단위"))
        if a is not None and b is not None:
            ga, ob = number(golden.get("예산액")), number(output.get("예산액"))
            if field == "예산단위":
                return True
            if ga is not None and ob is not None:
                return legacy_compare(field, ga * a, ob * b)
    # CO2와 CO2eq는 동치로 합치지 않는다.
    if field in {"배출량", "전망값", "예상감축량", "단위"}:
        import re
        pattern = r"(kg|t|kt|mt|톤|천톤|만톤|백만톤)(co2eq|co2e|co2)"
        a, b = re.fullmatch(pattern, unit_norm(golden.get("단위"))), re.fullmatch(pattern, unit_norm(output.get("단위")))
        scales = {"kg": 0.001, "t": 1, "톤": 1, "kt": 1000, "천톤": 1000,
                  "만톤": 10000, "mt": 1000000, "백만톤": 1000000}
        if a and b and ({"co2eq": "eq", "co2e": "eq", "co2": "co2"}[a[2]] == {"co2eq": "eq", "co2e": "eq", "co2": "co2"}[b[2]]):
            if field == "단위":
                return True
            x, y = number(golden.get(field)), number(output.get(field))
            if x is not None and y is not None:
                return legacy_compare(field, x * scales[a[1]], y * scales[b[1]])
    return legacy_compare(field, golden.get(field), output.get(field))


def score_v2(output_path, golden_path, *, scope=None):
    from utils.evaluation_v1.golden_score_contract import 데이터시트, 계약헤더, 키값, 값필드
    from utils.evaluation_v1.golden_score_workbook import 워크북읽기
    from utils.evaluation_v1.golden_score_matching import _필드일치
    golden = 워크북읽기(Path(golden_path), golden=True)
    output = 워크북읽기(Path(output_path), golden=False)
    scope = scope or {"kind": "document"}
    total = defaultdict(int)
    details, per_sheet = [], {}
    format_errors = []
    for sheet in 데이터시트:
        if scope.get("sheets") and sheet not in scope["sheets"]:
            continue
        gs, os = golden[sheet], output[sheet]
        if gs.상태 == "골든 없음":
            continue
        if gs.상태 == "형식 오류":
            format_errors.extend(gs.오류)
            continue
        gr, gu = _scope_rows(gs.행들, scope)
        out, ou = _scope_rows(os.행들, scope)
        if not gr:
            continue
        fields = [f for f in 계약헤더(sheet) if f not in EXCLUDED]
        # 양방향 후보 그래프에서 유일한 경우만 승인한다. 행 순서로 먼저 나온 답을 택하지 않는다.
        edges, reverse = {}, defaultdict(list)
        for gi, g in enumerate(gr):
            candidates = [oi for oi, o in enumerate(out) if _identity_compatible(sheet, g.값, o.값, 키값)]
            edges[gi] = candidates
            for oi in candidates:
                reverse[oi].append(gi)
        stats = defaultdict(int)
        stats.update(golden_rows=len(gr), output_rows=len(out), scope_unlocated_golden=len(gu), scope_unlocated_output=len(ou))
        for gi, g in enumerate(gr):
            candidates = edges[gi]
            signatures = {canonical({f: out[i].값.get(f) for f in 계약헤더(sheet)}) for i in candidates}
            o = None
            if candidates and len(signatures) == 1 and all(len(reverse[i]) == 1 for i in candidates):
                o = out[min(candidates, key=lambda i: out[i].번호)]
                stats["row_matches"] += 1
                stats["duplicate_output_candidates"] += len(candidates) - 1
                details.append({"sheet": sheet, "golden_row": g.번호, "output_row": o.번호, "status": "matched"})
            else:
                status = "ambiguous" if candidates else "missing"
                stats[status] += 1
                details.append({"sheet": sheet, "golden_row": g.번호, "output_rows": [out[i].번호 for i in candidates], "status": status})
            for field in fields:
                if not present(g.값.get(field)):
                    continue
                stats["cell_expected"] += 1
                if o is not None and present(o.값.get(field)) and _equivalent(field, g.값, o.값, _필드일치):
                    stats["cell_matches"] += 1
                elif o is not None:
                    details.append({"sheet": sheet, "golden_row": g.번호, "output_row": o.번호, "field": field,
                                    "status": "cell_mismatch", "golden": g.값.get(field), "output": o.값.get(field)})
            if o is not None:
                for f in 값필드.get(sheet, []):
                    if present(g.값.get(f)) and present(o.값.get(f)):
                        stats["value_pairs"] += 1
                        stats["value_matches"] += _equivalent(f, g.값, o.값, _필드일치)
        per_sheet[sheet] = dict(stats)
        for k, v in stats.items():
            total[k] += v
    def ratio(a, b):
        return round(total[a] / total[b], 6) if total[b] else None
    return {"contract_version": VERSION, "scope": scope, "counts": dict(total), "per_sheet": per_sheet,
            "metrics": {"cell_accuracy": ratio("cell_matches", "cell_expected"),
                        "row_recall": ratio("row_matches", "golden_rows"),
                        "row_precision": ratio("row_matches", "output_rows"),
                        "value_agreement": ratio("value_matches", "value_pairs")},
            "format_errors": format_errors, "details": details,
            "limitations": ["골든 매칭 정밀도는 불완전한 골든에 대한 비율이며 실제 오답률이 아님",
                            "모호한 다중 후보는 정답값과 가까운 행을 선택하지 않고 미매칭으로 보존",
                            "골든에 없는 출력은 오답으로 확정하지 않음", "v1/v2는 별도 계약이므로 숫자를 혼합 비교하지 않음"]}


def evaluate_registered(output_path, source_path, *, manifest_path=None, mode="auto", dataset_id=None,
                        allow_draft=False, allow_holdout=False, report_base=None):
    """등록/승인/해시 검증 실패는 Excel 저장 성공과 별개로 반환한다."""
    started = time.perf_counter()
    if mode == "off":
        return _unavailable("골든 평가 비활성", mode, status="disabled")
    failure_status = "required_unavailable" if mode == "required" else "unassessed"
    manifest_path = Path(manifest_path or ROOT / "data/evaluation/benchmark_manifest.json").resolve()
    if not manifest_path.is_file():
        return _unavailable("평가 매니페스트 없음", mode, status=failure_status)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        sha = document_hash(source_path)
        datasets = [d for d in manifest["datasets"] if d.get("source_sha256") == sha and (not dataset_id or d.get("id") == dataset_id)]
        if len(datasets) != 1:
            return _unavailable("원문 SHA 일치 데이터셋 미등록 또는 중복 등록", mode, status=failure_status, dataset=dataset_id)
        ds = datasets[0]
        def unavailable(reason):
            return _unavailable(reason, mode, status=failure_status, dataset=ds["id"])
        if not ds.get("golden"):
            return unavailable("등록된 골든 없음")
        if not ds.get("locked"):
            return unavailable("파일 해시/버전 잠금 미승인")
        if ds.get("role") == "holdout" and not allow_holdout:
            return unavailable("holdout 평가 명시 허용 필요")
        if ds.get("role") not in {"development", "holdout"}:
            return unavailable("알 수 없는 평가 데이터 역할")
        if ds.get("review_status") != "approved" and not (ds.get("review_status") == "draft" and allow_draft):
            return unavailable("골든 내용 검토 미승인 (draft는 --allow-draft-evaluation 필요)")
        golden = (manifest_path.parent / ds["golden"]).resolve()
        if not golden.is_file() or not ds.get("golden_sha256") or document_hash(golden) != ds["golden_sha256"]:
            return unavailable("골든 파일 없음 또는 해시 불일치")
        scope = ds.get("scope", {"kind": "document"})
        if scope.get("kind") not in {"document", "partial"}:
            return unavailable("지원하지 않는 평가 범위")
        if scope.get("kind") == "partial" and not any(scope.get(k) for k in ("pages", "fields", "sheets")):
            return unavailable("부분 골든의 공통 출력 범위 미지정")
        if scope.get("kind", "document") == "document" and any(scope.get(k) for k in ("pages", "fields", "sheets")):
            return unavailable("부분 필터가 있으면 kind=partial로 지정해야 함")
        if scope.get("pages") and (not isinstance(scope["pages"], list) or any(type(p) is not int or p < 1 for p in scope["pages"])):
            return unavailable("잘못된 평가 페이지 범위")
        if scope.get("fields") and (not isinstance(scope["fields"], dict) or any(not isinstance(v, list) or not v for v in scope["fields"].values())):
            return unavailable("범위 필드 필터는 필드명: [허용값] 형식이어야 함")
        if scope.get("sheets"):
            from utils.evaluation_v1.golden_score_contract import 데이터시트
            if not isinstance(scope["sheets"], list) or set(scope["sheets"]) - set(데이터시트):
                return unavailable("지원하지 않는 범위 시트")
        result = score_v2(output_path, golden, scope=scope)
        if result["format_errors"] or not result["counts"].get("cell_expected"):
            return unavailable("골든 형식 오류 또는 범위 내 채점 가능 셀 없음: " + str(result["format_errors"]))
        base = Path(report_base or output_path).with_suffix("")
        base.parent.mkdir(parents=True, exist_ok=True)
        detail_path = Path(str(base) + "_evaluation_v2_details.json")
        save_json(detail_path, result)
        result.pop("details")
        legacy = {"status": "unassessed", "reason": "부분 범위는 v1 참조 채점 미지원"}
        if scope.get("kind", "document") == "document":
            from utils.evaluation_v1.score_against_golden import score_workbooks
            legacy_result = score_workbooks(output_path, golden, report_dir=base.parent / (base.name + "_evaluation_v1"),
                                            label="fixed-v1", write_json=True)
            legacy = {"status": "evaluated", "contract_version": "golden-v1-frozen-20261026",
                      "metrics": legacy_result["독립평가지표"], "reports": legacy_result["리포트"]}
        result.update(status="evaluated", mode=mode, dataset_id=ds["id"], review_status=ds["review_status"],
            model_accuracy_evaluated=True, source_sha256=sha, golden_sha256=ds["golden_sha256"],
            manifest_path=str(manifest_path), manifest_sha256=document_hash(manifest_path),
            excel_sha256=document_hash(output_path), legacy_v1=legacy,
            details_path=str(detail_path), details_sha256=document_hash(detail_path))
        result["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        result["implementation_sha256"] = {p.name: document_hash(p) for p in [Path(__file__), *sorted((ROOT / "utils/evaluation_v1").glob("*.py")), ROOT / "utils/evaluation_v1/headers.json", ROOT / "agents/organizer_agent.py"]}
        evaluation_path = Path(str(base) + "_evaluation.json")
        save_json(evaluation_path, result)
        return {**result, "path": str(evaluation_path.resolve()), "sha256": document_hash(evaluation_path)}
    except Exception as error:
        return _unavailable(f"골든 평가 실패: {type(error).__name__}: {error}", mode, status="failed", dataset=dataset_id)
