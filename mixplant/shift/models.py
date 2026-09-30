"""Frozen value objects for crews, shifts, reconciliations and settlements."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import ValidationError
from .constants import POLICIES, POLICY_CONTINUE, STATUS_OPEN


def _positive(value: float, label: str, *, identity: str = "") -> None:
    if value < 0:
        raise ValidationError(f"{label} must not be negative", value=value, identity=identity)


@dataclass(frozen=True, slots=True)
class Crew:
    """A production crew whose mismatch policy is fixed at registration."""

    crew_id: str
    name: str
    mismatch_policy: str
    tolerance_m3: float
    created_tick: int

    def __post_init__(self) -> None:
        if not self.crew_id:
            raise ValidationError("crew identity must not be empty")
        if self.mismatch_policy not in POLICIES:
            raise ValidationError("unknown crew mismatch policy", policy=self.mismatch_policy, known=sorted(POLICIES))
        if self.tolerance_m3 < 0:
            raise ValidationError("crew tolerance must not be negative", tolerance_m3=self.tolerance_m3)

    @property
    def carries_differences(self) -> bool:
        """True when the crew settles anyway and lists the differences."""

        return self.mismatch_policy == POLICY_CONTINUE

    def describe(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "name": self.name,
            "mismatch_policy": self.mismatch_policy,
            "tolerance_m3": self.tolerance_m3,
            "created_tick": self.created_tick,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Crew":
        return cls(
            crew_id=str(document["crew_id"]),
            name=str(document.get("name", document["crew_id"])),
            mismatch_policy=str(document["mismatch_policy"]),
            tolerance_m3=float(document.get("tolerance_m3", 0.0)),
            created_tick=int(document.get("created_tick", 0)),
        )


@dataclass(frozen=True, slots=True)
class ProductionEntry:
    """One handwritten batch line: the batch id, its grade and cubic metres."""

    batch_id: str
    grade: str
    volume_m3: float
    tick: int

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValidationError("production entry needs a batch identity")
        if not self.grade:
            raise ValidationError("production entry needs a grade", batch_id=self.batch_id)
        if self.volume_m3 <= 0:
            raise ValidationError("production volume must be positive", batch_id=self.batch_id, volume_m3=self.volume_m3)

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "volume_m3": self.volume_m3,
            "tick": self.tick,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "ProductionEntry":
        return cls(
            batch_id=str(document["batch_id"]),
            grade=str(document["grade"]),
            volume_m3=float(document["volume_m3"]),
            tick=int(document.get("tick", 0)),
        )


@dataclass(frozen=True, slots=True)
class LoadingEntry:
    """One loading slip: which truck carried how much of a grade away."""

    slip_id: str
    truck_id: str
    grade: str
    volume_m3: float
    tick: int

    def __post_init__(self) -> None:
        if not self.slip_id:
            raise ValidationError("loading entry needs a slip identity")
        if not self.grade:
            raise ValidationError("loading entry needs a grade", slip_id=self.slip_id)
        if self.volume_m3 <= 0:
            raise ValidationError("loaded volume must be positive", slip_id=self.slip_id, volume_m3=self.volume_m3)

    def describe(self) -> dict[str, Any]:
        return {
            "slip_id": self.slip_id,
            "truck_id": self.truck_id,
            "grade": self.grade,
            "volume_m3": self.volume_m3,
            "tick": self.tick,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "LoadingEntry":
        return cls(
            slip_id=str(document["slip_id"]),
            truck_id=str(document.get("truck_id", "")),
            grade=str(document["grade"]),
            volume_m3=float(document["volume_m3"]),
            tick=int(document.get("tick", 0)),
        )


@dataclass(frozen=True, slots=True)
class UsageEntry:
    """One recipe usage line: how many kg of a material a grade consumed."""

    grade: str
    material: str
    consumed_kg: float
    tick: int

    def __post_init__(self) -> None:
        if not self.grade:
            raise ValidationError("usage entry needs a grade")
        if not self.material:
            raise ValidationError("usage entry needs a material", grade=self.grade)
        _positive(self.consumed_kg, "consumed mass", identity=self.grade)

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "material": self.material,
            "consumed_kg": self.consumed_kg,
            "tick": self.tick,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "UsageEntry":
        return cls(
            grade=str(document["grade"]),
            material=str(document["material"]),
            consumed_kg=float(document["consumed_kg"]),
            tick=int(document.get("tick", 0)),
        )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The computed three way reconciliation of one shift.

    ``rows`` holds the per grade totals; ``differences`` lists every line that
    did not tie out (one per grade and pair of sources, plus material lines).
    """

    shift_id: str
    tick: int
    batches: int
    produced_m3: float
    loaded_m3: float
    theoretical_m3: float
    rows: tuple[Mapping[str, Any], ...]
    differences: tuple[Mapping[str, Any], ...]
    fingerprint: str

    @property
    def balanced(self) -> bool:
        return not self.differences

    def describe(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "tick": self.tick,
            "batches": self.batches,
            "produced_m3": self.produced_m3,
            "loaded_m3": self.loaded_m3,
            "theoretical_m3": self.theoretical_m3,
            "balanced": self.balanced,
            "difference_count": len(self.differences),
            "rows": [dict(row) for row in self.rows],
            "differences": [dict(item) for item in self.differences],
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Reconciliation":
        return cls(
            shift_id=str(document["shift_id"]),
            tick=int(document.get("tick", 0)),
            batches=int(document.get("batches", 0)),
            produced_m3=float(document.get("produced_m3", 0.0)),
            loaded_m3=float(document.get("loaded_m3", 0.0)),
            theoretical_m3=float(document.get("theoretical_m3", 0.0)),
            rows=tuple(dict(row) for row in document.get("rows", ())),
            differences=tuple(dict(item) for item in document.get("differences", ())),
            fingerprint=str(document.get("fingerprint", "")),
        )


@dataclass(frozen=True, slots=True)
class Settlement:
    """The immutable close of shift record.

    ``digest`` chains onto ``prev_digest`` so the closed history can later be
    verified to be byte for byte the one that was settled.
    """

    settlement_id: str
    shift_id: str
    crew_id: str
    tick: int
    result: str
    mismatch_policy: str
    batches: int
    produced_m3: float
    loaded_m3: float
    theoretical_m3: float
    differences: tuple[Mapping[str, Any], ...]
    fingerprint: str
    prev_digest: str
    digest: str
    snapshot: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    record_seq: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "shift_id": self.shift_id,
            "crew_id": self.crew_id,
            "tick": self.tick,
            "result": self.result,
            "mismatch_policy": self.mismatch_policy,
            "batches": self.batches,
            "produced_m3": self.produced_m3,
            "loaded_m3": self.loaded_m3,
            "theoretical_m3": self.theoretical_m3,
            "difference_count": len(self.differences),
            "differences": [dict(item) for item in self.differences],
            "fingerprint": self.fingerprint,
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "snapshot": dict(self.snapshot),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Settlement":
        return cls(
            settlement_id=str(document["settlement_id"]),
            shift_id=str(document["shift_id"]),
            crew_id=str(document["crew_id"]),
            tick=int(document.get("tick", 0)),
            result=str(document["result"]),
            mismatch_policy=str(document.get("mismatch_policy", "")),
            batches=int(document.get("batches", 0)),
            produced_m3=float(document.get("produced_m3", 0.0)),
            loaded_m3=float(document.get("loaded_m3", 0.0)),
            theoretical_m3=float(document.get("theoretical_m3", 0.0)),
            differences=tuple(dict(item) for item in document.get("differences", ())),
            fingerprint=str(document.get("fingerprint", "")),
            prev_digest=str(document.get("prev_digest", "")),
            digest=str(document["digest"]),
            snapshot=MappingProxyType(dict(document.get("snapshot", {}))),
            record_seq=int(document.get("record_seq", 0)),
        )


@dataclass(frozen=True, slots=True)
class Shift:
    """One open or suspended shift together with its three source ledgers."""

    shift_id: str
    crew_id: str
    opened_tick: int
    status: str
    production: tuple[ProductionEntry, ...]
    loading: tuple[LoadingEntry, ...]
    usage: tuple[UsageEntry, ...]
    reconciliation: Reconciliation | None
    last_result: str = ""

    @property
    def is_closed(self) -> bool:
        return self.status == "CLOSED"

    def describe(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "crew_id": self.crew_id,
            "opened_tick": self.opened_tick,
            "status": self.status,
            "production": [item.describe() for item in self.production],
            "loading": [item.describe() for item in self.loading],
            "usage": [item.describe() for item in self.usage],
            "reconciliation": None if self.reconciliation is None else self.reconciliation.describe(),
            "last_result": self.last_result,
        }


def open_shift_document(shift: Shift) -> dict[str, Any]:
    """Build the single leaf snapshot persisted for an open/suspended shift."""

    return {
        "active": True,
        "shift_id": shift.shift_id,
        "crew_id": shift.crew_id,
        "opened_tick": shift.opened_tick,
        "status": shift.status,
        "last_result": shift.last_result,
        "production": [item.describe() for item in shift.production],
        "loading": [item.describe() for item in shift.loading],
        "usage": [item.describe() for item in shift.usage],
        "reconciliation": None if shift.reconciliation is None else shift.reconciliation.describe(),
    }


def shift_from_document(document: Mapping[str, Any]) -> Shift:
    """Rebuild an open/suspended shift from its persisted leaf snapshot."""

    reconciliation = document.get("reconciliation")
    return Shift(
        shift_id=str(document["shift_id"]),
        crew_id=str(document["crew_id"]),
        opened_tick=int(document.get("opened_tick", 0)),
        status=str(document.get("status", STATUS_OPEN)),
        production=tuple(ProductionEntry.from_document(item) for item in document.get("production", ())),
        loading=tuple(LoadingEntry.from_document(item) for item in document.get("loading", ())),
        usage=tuple(UsageEntry.from_document(item) for item in document.get("usage", ())),
        reconciliation=None if not reconciliation else Reconciliation.from_document(reconciliation),
        last_result=str(document.get("last_result", "")),
    )
