"""The in memory shift aggregate rebuilt from the journal.

The store is the source of truth: this object only caches what the surviving
records say, so a restart rebuilding the same journal reaches the same state.
It also owns the shift's idempotency key table, which is persisted inside the
reconcile / settle records so a duplicated call returns the first result
instead of writing a second time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import NotFoundError, OrderViolationError
from .crew import Crew
from .entries import ShiftLedger
from .kinds import (
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RECONCILED,
    STATUS_SETTLED,
    STATUS_SUSPENDED,
)

# Statuses in which new ledger rows are still accepted.  A suspended shift is
# *not* writable: a crew with the suspend policy must reopen it (recording why)
# before corrections can be appended.
WRITABLE = frozenset({STATUS_OPEN})
# Statuses a settlement can be requested from.
SETTLEABLE = frozenset({STATUS_CLOSED, STATUS_RECONCILED, STATUS_SUSPENDED})


@dataclass(slots=True)
class Shift:
    """One production shift and everything gathered for its closeout."""

    shift_id: str
    crew: Crew
    opened_tick: int
    status: str = STATUS_OPEN
    closed_tick: int | None = None
    ledger: ShiftLedger = field(init=False)
    reports: list[dict[str, Any]] = field(default_factory=list)
    settlement: dict[str, Any] | None = None
    reopen_count: int = 0
    # action -> {idempotency_key -> stored response}
    idempotency: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ledger = ShiftLedger(self.shift_id)

    @property
    def is_writable(self) -> bool:
        return self.status in WRITABLE

    @property
    def is_settled(self) -> bool:
        return self.status == STATUS_SETTLED

    @property
    def latest_report(self) -> dict[str, Any] | None:
        return self.reports[-1] if self.reports else None

    def require_status(self, *allowed: str) -> None:
        if self.status not in allowed:
            raise OrderViolationError(
                "the shift is not in the required status for this action",
                shift_id=self.shift_id,
                status=self.status,
                required=list(allowed),
            )

    def require_writable(self) -> None:
        if not self.is_writable:
            raise OrderViolationError(
                "the shift is closed; its ledgers are frozen",
                shift_id=self.shift_id,
                status=self.status,
            )

    def require_not_settled(self, action: str) -> None:
        if self.is_settled:
            raise OrderViolationError(
                f"{action} is forbidden: a settled shift is immutable history",
                shift_id=self.shift_id,
                status=self.status,
            )

    def store_idempotent(self, action: str, key: str, response: dict[str, Any]) -> None:
        self.idempotency.setdefault(action, {})[key] = response

    def idempotent_result(self, action: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        return self.idempotency.get(action, {}).get(key)

    def describe(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "shift_id": self.shift_id,
            "crew": self.crew.describe(),
            "status": self.status,
            "opened_tick": self.opened_tick,
            "closed_tick": self.closed_tick,
            "reopen_count": self.reopen_count,
            "entry_count": len(self.ledger.entries()),
            "writable": self.is_writable,
            "settled": self.is_settled,
        }
        if self.latest_report is not None:
            report = self.latest_report
            body["latest_report"] = {
                "version": report.get("version"),
                "tick": report.get("tick"),
                "has_difference": report.get("has_difference"),
                "fingerprint": report.get("entries_fingerprint"),
            }
        if self.settlement is not None:
            body["settlement"] = {
                "settlement_id": self.settlement.get("settlement_id"),
                "outcome": self.settlement.get("outcome"),
                "tick": self.settlement.get("tick"),
                "hash": self.settlement.get("hash"),
            }
        return body


class ShiftRegistry:
    """Every shift known to the plant, oldest first."""

    __slots__ = ("_shifts", "_order")

    def __init__(self) -> None:
        self._shifts: dict[str, Shift] = {}
        self._order: list[str] = []

    def add(self, shift: Shift) -> Shift:
        if shift.shift_id in self._shifts:
            raise OrderViolationError("shift identity was reused", shift_id=shift.shift_id)
        self._shifts[shift.shift_id] = shift
        self._order.append(shift.shift_id)
        return shift

    def adopt(self, shift: Shift) -> Shift:
        existing = self._shifts.get(shift.shift_id)
        if existing is None:
            self._shifts[shift.shift_id] = shift
            self._order.append(shift.shift_id)
            return shift
        return existing

    def get(self, shift_id: str) -> Shift | None:
        return self._shifts.get(shift_id)

    def require(self, shift_id: str) -> Shift:
        shift = self.get(shift_id)
        if shift is None:
            raise NotFoundError("shift was never opened", shift_id=shift_id)
        return shift

    def all(self) -> tuple[Shift, ...]:
        return tuple(self._shifts[name] for name in self._order)

    def open_shifts(self) -> tuple[Shift, ...]:
        return tuple(shift for shift in self.all() if not shift.is_settled)

    def last_settlement_hash(self) -> str:
        """Return the head of the settlement hash chain."""

        for shift in reversed(self.all()):
            if shift.settlement is not None:
                return str(shift.settlement["hash"])
        return ""
