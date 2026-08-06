from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from .io_utils import load_manifest, write_jsonl


class UnlimitedOCRError(RuntimeError):
    pass


def _image_data_url(path: str | Path) -> str:
    image_path = Path(path)
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_chunk_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return str(payload.get("text") or "")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    raw_delta = choice.get("delta")
    raw_message = choice.get("message")
    delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    value = delta.get("content") or message.get("content") or choice.get("text") or ""
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in value
        )
    return str(value)


def _response_text(response: requests.Response) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        try:
            return _extract_chunk_text(response.json())
        except (ValueError, json.JSONDecodeError):
            return response.text

    pieces: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        line = (raw_line or "").strip()
        if not line or not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if value == "[DONE]":
            break
        try:
            pieces.append(_extract_chunk_text(json.loads(value)))
        except json.JSONDecodeError:
            continue
    return "".join(pieces)


def resolve_model(server_url: str, requested_model: str | None, timeout: float = 15) -> str:
    if requested_model:
        return requested_model
    endpoint = server_url.rstrip("/") + "/v1/models"
    session = requests.Session()
    session.trust_env = False
    response = session.get(endpoint, timeout=timeout)
    response.raise_for_status()
    data = response.json().get("data", [])
    if not data or not isinstance(data[0], dict) or not data[0].get("id"):
        raise UnlimitedOCRError("SGLang 서버에서 모델 ID를 확인할 수 없습니다.")
    return str(data[0]["id"])


def request_page(
    *,
    image_path: str | Path,
    server_url: str,
    model: str,
    image_mode: str = "gundam",
    prompt: str = "document parsing.",
    timeout: float = 1200,
    max_tokens: int = 32768,
    processor_code: str | None = None,
    retries: int = 4,
) -> tuple[str, dict[str, Any]]:
    endpoint = server_url.rstrip("/") + "/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
            ],
        }],
        "temperature": 0,
        "max_tokens": max_tokens,
        "skip_special_tokens": False,
        "stream": True,
        "images_config": {"image_mode": image_mode},
    }
    if processor_code:
        payload["custom_logit_processor"] = processor_code
        payload["custom_params"] = {"ngram_size": 35, "window_size": 128}

    last_error: Exception | None = None
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.post(endpoint, json=payload, timeout=timeout, stream=True)
            if response.status_code >= 400:
                detail = response.text[:1000]
                raise UnlimitedOCRError(f"HTTP {response.status_code}: {detail}")
            output = _response_text(response).strip()
            if not output:
                raise UnlimitedOCRError("Unlimited-OCR 서버가 빈 응답을 반환했습니다.")
            return output, {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "attempts": attempt + 1,
                "model": model,
                "image_mode": image_mode,
                "status": "success",
            }
        except (requests.RequestException, UnlimitedOCRError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt * 3, 15))
    raise UnlimitedOCRError(str(last_error or "Unlimited-OCR 호출 실패"))


def run_uocr(
    manifest_path: str | Path,
    *,
    server_url: str = "http://127.0.0.1:10000",
    model: str | None = None,
    image_mode: str = "gundam",
    prompt: str = "document parsing.",
    timeout: float = 1200,
    max_tokens: int = 32768,
    processor_file: str | Path | None = None,
    concurrency: int = 1,
    retries: int = 4,
) -> Path:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    raw_dir = run_dir / "outputs" / "unlimited_ocr" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resolved_model = resolve_model(server_url, model)
    processor_code = Path(processor_file).read_text(encoding="utf-8") if processor_file else None

    def process(item: dict[str, Any]) -> dict[str, Any]:
        page = int(item["page_number"])
        raw_path = raw_dir / f"page_{page:04d}.md"
        if raw_path.exists() and raw_path.stat().st_size > 0:
            return {"page_number": page, "status": "cached", "raw_path": str(raw_path)}
        try:
            text, metadata = request_page(
                image_path=item["image_path"],
                server_url=server_url,
                model=resolved_model,
                image_mode=image_mode,
                prompt=prompt,
                timeout=timeout,
                max_tokens=max_tokens,
                processor_code=processor_code,
                retries=retries,
            )
            raw_path.write_text(text, encoding="utf-8")
            return {"page_number": page, "raw_path": str(raw_path), **metadata}
        except Exception as exc:
            return {"page_number": page, "status": "failed", "error": str(exc), "raw_path": str(raw_path)}

    results: list[dict[str, Any]] = []
    worker_count = max(1, int(concurrency))
    if worker_count == 1:
        for page in manifest["pages"]:
            results.append(process(page))
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(process, page): int(page["page_number"]) for page in manifest["pages"]}
            for future in as_completed(futures):
                results.append(future.result())

    results.sort(key=lambda item: int(item["page_number"]))
    write_jsonl(raw_dir.parent / "requests.jsonl", results)
    failed = [item for item in results if item.get("status") == "failed"]
    if failed:
        pages = ", ".join(str(item["page_number"]) for item in failed)
        raise UnlimitedOCRError(f"{len(failed)}개 페이지 OCR 실패: {pages}. requests.jsonl을 확인하세요.")
    return raw_dir
