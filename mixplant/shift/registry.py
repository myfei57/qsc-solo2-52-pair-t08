"""In memory registry rebuilt by replaying the shift records on bootstrap.

Crews, open shifts and settlements are kept here, oldest first, exactly like the
batch registry and the load manifest.  Settlements additionally form a SHA256
chain whose earliest close seq acts as the immutable history waterline: the
store refuses to roll back through it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, OrderViolationError
from .constants import STATUS_CLOSED, STATUS_OPEN, STATUS_SUSPENDED
from .models import (
    Crew,
    Settlement,
    Shift,
)

GENESIS_DIGEST = "0" * 64


def settlement_digest(prev_digest: str, body: Mapping[str, Any]) -> str:
    """Hash a settlement body onto the end of the chain."""

    encoded = json.dumps(
        {"prev": prev_digest, "body": body}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def chain_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable fields of a settlement payload used for hashing."""

    keys = (
        "settlement_id",
        "shift_id",
        "crew_id",
        "result",
        "mismatch_policy",
        "batches",
        "produced_m3",
        "loaded_m3",
        "theoretical_m3",
        "fingerprint",
    )
    return {key: payload[key] for key in keys} | {
        "differences": [dict(item) for item in payload.get("differences", ())]
    }


class ShiftRegistry:
    """Owns crews, open shifts and the immutable settlement history."""

    __slots__ = ("_crews", "_shifts", "_settlements", "_open_by_crew")

    def __init__(self) -> None:
        self._crews: dict[str, Crew] = {}
        self._shifts: dict[str, Shift] = {}
        self._settlements: list[Settlement] = []
        self._open_by_crew: dict[str, str] = {}

    # ------------------------------------------------------------------ crews
    def register_crew(self, crew: Crew) -> Crew:
        if crew.crew_id in self._crews:
            raise DuplicateError("crew identity was reused", crew_id=crew.crew_id)
        self._crews[crew.crew_id] = crew
        return crew

    def adopt_crew(self, crew: Crew) -> Crew:
        """Adopt a crew read back from the stream (last write keeps it)."""

        existing = self._crews.get(crew.crew_id)
        # The policy is immutable, so an adopt must never silently change it.
        if existing is not None and existing.mismatch_policy != crew.mismatch_policy:
            raise OrderViolationError(
                "a crew mismatch policy cannot change after registration",
                crew_id=crew.crew_id,
                existing=existing.mismatch_policy,
                replayed=crew.mismatch_policy,
            )
        self._crews[crew.crew_id] = crew
        return crew

    def require_crew(self, crew_id: str) -> Crew:
        crew = self._crews.get(crew_id)
        if crew is None:
            raise NotFoundError("crew was never registered", crew_id=crew_id)
        return crew

    def crews(self) -> tuple[Crew, ...]:
        return tuple(self._crews[id_] for id_ in sorted(self._crews))

    # ----------------------------------------------------------------- shifts
    def open_shift(self, shift: Shift) -> Shift:
        if shift.shift_id in self._shifts:
            raise DuplicateError("shift identity was reused", shift_id=shift.shift_id)
        crew = self.require_crew(shift.crew_id)
        current = self._open_by_crew.get(crew.crew_id)
        if current is not None:
            raise OrderViolationError("crew already has an open shift", crew_id=crew.crew_id, shift_id=current)
        self._shifts[shift.shift_id] = shift
        self._open_by_crew[crew.crew_id] = shift.shift_id
        return shift

    def adopt_shift(self, shift: Shift, *, closed: bool = False) -> Shift:
        """Adopt a shift snapshot read back from the stream."""

        if shift.crew_id not in self._crews:
            raise NotFoundError("shift references an unknown crew", crew_id=shift.crew_id)
        self._shifts[shift.shift_id] = shift
        if shift.status == STATUS_CLOSED or closed:
            self._open_by_crew.pop(shift.crew_id, None)
        else:
            self._open_by_crew[shift.crew_id] = shift.shift_id
        return shift

    def replace_shift(self, shift: Shift) -> None:
        """Overwrite the in memory shift while keeping its identity."""

        if shift.shift_id not in self._shifts:
            raise NotFoundError("shift was never opened", shift_id=shift.shift_id)
        self._shifts[shift.shift_id] = shift

    def require_shift(self, shift_id: str) -> Shift:
        shift = self._shifts.get(shift_id)
        if shift is None:
            raise NotFoundError("shift was never opened", shift_id=shift_id)
        return shift

    def find_shift(self, shift_id: str) -> Shift | None:
        return self._shifts.get(shift_id)

    def open_shift_id_for(self, crew_id: str) -> str | None:
        return self._open_by_crew.get(crew_id)

    def shifts(self) -> tuple[Shift, ...]:
        return tuple(self._shifts[id_] for id_ in sorted(self._shifts))

    def require_mutable(self, shift_id: str) -> Shift:
        """Return a shift only while it can still accept changes."""

        shift = self.require_shift(shift_id)
        if shift.status == STATUS_CLOSED:
            raise OrderViolationError(
                "settled shift is immutable history", shift_id=shift_id, status=shift.status
            )
        return shift

    # ------------------------------------------------------------- settlements
    def settle(self, settlement: Settlement) -> Settlement:
        """Append a settlement, verify the chain, and retire the shift."""

        shift = self.require_shift(settlement.shift_id)
        if shift.status == STATUS_CLOSED:
            raise OrderViolationError(
                "shift already settled", shift_id=settlement.shift_id, status=shift.status
            )
        expected_prev = self.tail_digest()
        if settlement.prev_digest != expected_prev:
            raise OrderViolationError(
                "settlement chain is broken",
                shift_id=settlement.shift_id,
                expected=expected_prev,
                provided=settlement.prev_digest,
            )
        expected_digest = settlement_digest(expected_prev, chain_body(settlement.describe()))
        if expected_digest != settlement.digest:
            raise OrderViolationError(
                "settlement digest does not match its content", shift_id=settlement.shift_id
            )
        self._settlements.append(settlement)
        closed = Shift(
            shift_id=shift.shift_id,
            crew_id=shift.crew_id,
            opened_tick=shift.opened_tick,
            status=STATUS_CLOSED,
            production=shift.production,
            loading=shift.loading,
            usage=shift.usage,
            reconciliation=shift.reconciliation,
            last_result=settlement.result,
        )
        self.replace_shift(closed)
        self._open_by_crew.pop(shift.crew_id, None)
        return settlement

    def adopt_settlement(self, settlement: Settlement) -> Settlement:
        """Adopt a settlement read back from the stream, verifying the chain."""

        if any(item.settlement_id == settlement.settlement_id for item in self._settlements):
            return settlement
        if any(item.shift_id == settlement.shift_id for item in self._settlements):
            return settlement
        expected_prev = self.tail_digest()
        if settlement.prev_digest != expected_prev:
            raise OrderViolationError(
                "settlement chain is broken on replay",
                shift_id=settlement.shift_id,
                expected=expected_prev,
                provided=settlement.prev_digest,
            )
        expected_digest = settlement_digest(expected_prev, chain_body(settlement.describe()))
        if expected_digest != settlement.digest:
            raise OrderViolationError(
                "settlement digest does not match on replay", shift_id=settlement.shift_id
            )
        self._settlements.append(settlement)
        shift = self._shifts.get(settlement.shift_id)
        if shift is not None:
            self.adopt_shift(
                Shift(
                    shift_id=shift.shift_id,
                    crew_id=shift.crew_id,
                    opened_tick=shift.opened_tick,
                    status=STATUS_CLOSED,
                    production=shift.production,
                    loading=shift.loading,
                    usage=shift.usage,
                    reconciliation=shift.reconciliation,
                    last_result=settlement.result,
                ),
                closed=True,
            )
        return settlement

    def settlement_for(self, shift_id: str) -> Settlement | None:
        for item in self._settlements:
            if item.shift_id == shift_id:
                return item
        return None

    def settlements(self) -> tuple[Settlement, ...]:
        return tuple(self._settlements)

    def tail_digest(self) -> str:
        return GENESIS_DIGEST if not self._settlements else self._settlements[-1].digest

    def verify_chain(self) -> dict[str, Any]:
        """Recompute every digest and report the first broken link."""

        prev = GENESIS_DIGEST
        for index, item in enumerate(self._settlements):
            expected = settlement_digest(prev, chain_body(item.describe()))
            if item.prev_digest != prev or item.digest != expected:
                return {
                    "ok": False,
                    "broken_at": index,
                    "settlement_id": item.settlement_id,
                    "expected": expected,
                    "found": item.digest,
                }
            prev = item.digest
        return {"ok": True, "settlements": len(self._settlements), "tail": prev}

    def earliest_settle_seq(self) -> int | None:
        """Smallest close seq; the rollback waterline that guards history."""

        seqs = [item.record_seq for item in self._settlements if item.record_seq > 0]
        return min(seqs) if seqs else None

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        return {
            "crews": len(self._crews),
            "open_shifts": sum(1 for shift in self._shifts.values() if shift.status == STATUS_OPEN),
            "suspended_shifts": sum(
                1 for shift in self._shifts.values() if shift.status == STATUS_SUSPENDED
            ),
            "closed_shifts": sum(1 for shift in self._shifts.values() if shift.status == STATUS_CLOSED),
            "settlements": len(self._settlements),
            "chain": self.verify_chain(),
        }
