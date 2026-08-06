"""Shared retry primitives and terminal-state tracking for visual objects."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, TypeVar


TERMINAL_OBJECT_STATUSES = frozenset({
    "extracted",
    "not_relevant",
    "no_data",
    "needs_review",
})

_STATUS_PRIORITY = {
    "needs_review": 0,
    "not_relevant": 1,
    "no_data": 2,
    "extracted": 3,
}

T = TypeVar("T")


def split_in_half(values: Sequence[T]) -> list[list[T]]:
    """Split a failed batch deterministically while preserving input order."""
    items = list(values)
    if len(items) <= 1:
        return [items] if items else []
    midpoint = max(1, len(items) // 2)
    return [group for group in (items[:midpoint], items[midpoint:]) if group]


def has_usable_table_value(rows: Any) -> bool:
    """Return whether a visual table contains explicit numeric or project content."""
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("값")
        if value is not None and str(value).strip().casefold() not in {"", "null", "none", "nan"}:
            return True
        values = row.get("values") or row.get("연도별")
        if isinstance(values, dict) and any(
            value is not None
            and str(value).strip().casefold() not in {"", "null", "none", "nan"}
            for value in values.values()
        ):
            return True
        fields = row.get("fields")
        if isinstance(fields, dict) and any(
            str(fields.get(key) or "").strip()
            for key in ("사업명", "감축사업명", "관리번호")
        ):
            return True
    return False


def normalize_terminal_status(
    status: Any,
    *,
    response_received: bool,
    table: Any = None,
    data_expected: bool = False,
) -> str:
    """Normalize model output to the four-state visual-object contract."""
    if not response_received:
        return "needs_review"
    normalized = str(status or "").strip().lower()
    if normalized == "failed" or normalized not in TERMINAL_OBJECT_STATUSES:
        normalized = "needs_review"
    if normalized == "extracted" and data_expected and not has_usable_table_value(table):
        return "no_data"
    return normalized


@dataclass(slots=True)
class ObjectOutcome:
    object_id: str
    status: str = "needs_review"
    response_received: bool = False
    attempt_count: int = 0
    terminal_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "status": self.status,
            "response_received": self.response_received,
            "attempt_count": self.attempt_count,
            "terminal_reason": self.terminal_reason,
            "history": [dict(row) for row in self.history],
        }


class ObjectOutcomeLedger:
    """Thread-safe one-row terminal outcome ledger keyed by object/evidence ID."""

    def __init__(self, object_ids: Iterable[str] = ()) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, ObjectOutcome] = {}
        self.register(object_ids)

    def register(self, object_ids: Iterable[str]) -> None:
        with self._lock:
            for raw_id in object_ids:
                object_id = str(raw_id or "").strip()
                if object_id and object_id not in self._rows:
                    self._rows[object_id] = ObjectOutcome(object_id=object_id)

    def mark_attempt(self, object_ids: Iterable[str], *, label: str = "") -> None:
        ids = [str(value or "").strip() for value in object_ids]
        self.register(ids)
        with self._lock:
            for object_id in ids:
                if not object_id:
                    continue
                row = self._rows[object_id]
                row.attempt_count += 1
                row.history.append({
                    "event": "attempt",
                    "attempt": row.attempt_count,
                    "label": str(label or ""),
                })

    def record(
        self,
        object_id: str,
        status: str,
        *,
        response_received: bool,
        reason: str = "",
    ) -> None:
        normalized_id = str(object_id or "").strip()
        if not normalized_id:
            return
        normalized_status = normalize_terminal_status(
            status,
            response_received=response_received,
        )
        self.register([normalized_id])
        with self._lock:
            row = self._rows[normalized_id]
            row.history.append({
                "event": "outcome",
                "status": normalized_status,
                "response_received": bool(response_received),
                "reason": str(reason or ""),
            })
            current_priority = _STATUS_PRIORITY.get(row.status, -1) if row.resolved else -1
            incoming_priority = _STATUS_PRIORITY[normalized_status]
            if incoming_priority >= current_priority:
                row.status = normalized_status
                row.response_received = bool(response_received)
                row.terminal_reason = str(reason or "")
                row.resolved = True

    def attempt_count(self, object_id: str) -> int:
        with self._lock:
            row = self._rows.get(str(object_id or "").strip())
            return row.attempt_count if row else 0

    def outcome(self, object_id: str, *, unresolved_reason: str = "") -> ObjectOutcome:
        normalized_id = str(object_id or "").strip()
        self.register([normalized_id])
        with self._lock:
            row = self._rows[normalized_id]
            if not row.resolved:
                row.status = "needs_review"
                row.response_received = False
                row.terminal_reason = str(unresolved_reason or "최종 응답을 확보하지 못함")
                row.resolved = True
                row.history.append({
                    "event": "terminal",
                    "status": "needs_review",
                    "reason": row.terminal_reason,
                })
            return ObjectOutcome(
                object_id=row.object_id,
                status=row.status,
                response_received=row.response_received,
                attempt_count=row.attempt_count,
                terminal_reason=row.terminal_reason,
                history=[dict(value) for value in row.history],
                resolved=row.resolved,
            )

    def unresolved_ids(self) -> list[str]:
        with self._lock:
            return sorted(object_id for object_id, row in self._rows.items() if not row.resolved)

    def rows(self, *, unresolved_reason: str = "") -> list[dict[str, Any]]:
        with self._lock:
            ids = sorted(self._rows)
        return [
            self.outcome(object_id, unresolved_reason=unresolved_reason).to_dict()
            for object_id in ids
        ]
