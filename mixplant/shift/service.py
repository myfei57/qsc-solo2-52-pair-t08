"""Shift service: crews, three source ledgers, reconciliation and settlement.

Concurrency: every mutating call runs inside one reentrant lock and uses a
read state then compare and set transition.  A repeated settlement trigger on
a closed shift returns the existing settlement without writing anything; a
repeat over the same suspended reconciliation writes nothing either.  Settled
shifts reject every mutation through :meth:`ShiftRegistry.require_mutable`.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Callable, Mapping

from ..errors import OrderViolationError, ValidationError
from ..service_base import PlantService
from .constants import (
    CLOSE_KIND,
    CREW_KIND,
    LOADING_KIND,
    OPEN_KIND,
    POLICIES,
    PRODUCTION_KIND,
    RECONCILE_KIND,
    RESULT_BALANCED,
    RESULT_CARRIED_DIFF,
    RESULT_SUSPENDED,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_SUSPENDED,
    USAGE_KIND,
)
from .models import (
    Crew,
    LoadingEntry,
    ProductionEntry,
    Reconciliation,
    Settlement,
    Shift,
    UsageEntry,
    open_shift_document,
    shift_from_document,
)
from .reconcile import reconcile_shift
from .registry import ShiftRegistry, settlement_digest
from .capture import PlantFacts

FactorsProvider = Callable[[], Mapping[str, Mapping[str, float]]]
PlantFactsProvider = Callable[[], PlantFacts]


class ShiftService(PlantService):
    """Registers crews, runs shifts and closes their books, idempotently."""

    origin = "shift"
    line = "batch"
    subject = "shift"

    def __init__(
        self,
        context,
        machine=None,
        *,
        factors_provider: FactorsProvider | None = None,
        plant_facts_provider: PlantFactsProvider | None = None,
    ) -> None:
        super().__init__(context, machine)
        self.registry = ShiftRegistry()
        self._factors_provider = factors_provider or (lambda: {})
        self._plant_facts_provider = plant_facts_provider or PlantFacts.empty
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- helpers
    def _factors(self) -> Mapping[str, Mapping[str, float]]:
        provider = self._factors_provider
        try:
            return provider() or {}
        except Exception:  # pragma: no cover - a provider must never break settle
            return {}

    def _crew_tolerance(self, crew_id: str, override: float | None) -> float:
        if override is not None:
            if override < 0:
                raise ValidationError("tolerance must not be negative", tolerance_m3=override)
            return float(override)
        return self.registry.require_crew(crew_id).tolerance_m3

    def _compute(self, shift: Shift, *, tolerance_m3: float, tick: int) -> Reconciliation:
        return reconcile_shift(
            shift_id=shift.shift_id,
            production=shift.production,
            loading=shift.loading,
            usage=shift.usage,
            factors_by_grade=self._factors(),
            tolerance_m3=tolerance_m3,
            tick=tick,
        )

    def _save_shift(self, shift: Shift) -> None:
        """Persist the current full snapshot of an open/suspended shift."""

        self.registry.replace_shift(shift)
        self.publish(OPEN_KIND, open_shift_document(shift))

    def _settlement_snapshot(self, shift: Shift, reconciliation: Reconciliation) -> dict[str, Any]:
        """Freeze everything the close decision was made from."""

        return {
            "shift": shift.describe(),
            "crew": self.registry.require_crew(shift.crew_id).describe(),
            "reconciliation": reconciliation.describe(),
            "factors_by_grade": {
                grade: dict(factors)
                for grade, factors in sorted(self._factors().items())
                if grade in {row["grade"] for row in reconciliation.rows}
            },
        }

    # ------------------------------------------------------------------ crews
    def register_crew(
        self,
        *,
        crew_id: str,
        name: str | None = None,
        mismatch_policy: str,
        tolerance_m3: float = 0.0,
    ) -> dict[str, Any]:
        """Register a crew; its mismatch policy is fixed from this point on."""

        with self._lock:
            if mismatch_policy not in POLICIES:
                raise ValidationError(
                    "unknown crew mismatch policy", policy=mismatch_policy, known=sorted(POLICIES)
                )
            crew = Crew(
                crew_id=crew_id,
                name=name or crew_id,
                mismatch_policy=mismatch_policy,
                tolerance_m3=float(tolerance_m3),
                created_tick=self.context.clock.now(),
            )
            self.registry.register_crew(crew)
            self.publish(CREW_KIND, crew.describe())
            self.emit("shift.crew_registered", {"crew_id": crew_id, "mismatch_policy": mismatch_policy})
            return {"crew": crew.describe()}

    # ------------------------------------------------------------------ shifts
    def open_shift(self, *, shift_id: str, crew_id: str) -> dict[str, Any]:
        """Open a shift for a crew that has no open or suspended shift."""

        with self._lock:
            crew = self.registry.require_crew(crew_id)
            current = self.registry.open_shift_id_for(crew.crew_id)
            if current is not None:
                raise OrderViolationError("crew already has an active shift", crew_id=crew_id, shift_id=current)
            shift = Shift(
                shift_id=shift_id,
                crew_id=crew.crew_id,
                opened_tick=self.context.clock.now(),
                status=STATUS_OPEN,
                production=(),
                loading=(),
                usage=(),
                reconciliation=None,
            )
            self.registry.open_shift(shift)
            self.publish(OPEN_KIND, open_shift_document(shift))
            self.emit("shift.opened", {"shift_id": shift_id, "crew_id": crew_id})
            return {"shift": shift.describe()}

    def record_production(self, *, shift_id: str, batch_id: str, grade: str, volume_m3: float) -> dict[str, Any]:
        with self._lock:
            shift = self.registry.require_mutable(shift_id)
            if any(item.batch_id == batch_id for item in shift.production):
                raise DuplicateProductionError(batch_id=batch_id, shift_id=shift_id)
            entry = ProductionEntry(
                batch_id=batch_id, grade=grade, volume_m3=float(volume_m3), tick=self.context.clock.now()
            )
            updated = dataclasses.replace(shift, production=(*shift.production, entry))
            self._save_shift(updated)
            self.publish(
                PRODUCTION_KIND, {"active": True, "shift_id": shift_id, "entry": entry.describe()}
            )
            return {"entry": entry.describe(), "shift": updated.describe()}

    def record_loading(
        self, *, shift_id: str, slip_id: str, grade: str, volume_m3: float, truck_id: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            shift = self.registry.require_mutable(shift_id)
            if any(item.slip_id == slip_id for item in shift.loading):
                raise DuplicateLoadingError(slip_id=slip_id, shift_id=shift_id)
            entry = LoadingEntry(
                slip_id=slip_id,
                truck_id=truck_id,
                grade=grade,
                volume_m3=float(volume_m3),
                tick=self.context.clock.now(),
            )
            updated = dataclasses.replace(shift, loading=(*shift.loading, entry))
            self._save_shift(updated)
            self.publish(LOADING_KIND, {"active": True, "shift_id": shift_id, "entry": entry.describe()})
            return {"entry": entry.describe(), "shift": updated.describe()}

    def record_usage(
        self, *, shift_id: str, grade: str, material: str, consumed_kg: float
    ) -> dict[str, Any]:
        with self._lock:
            shift = self.registry.require_mutable(shift_id)
            entry = UsageEntry(
                grade=grade, material=material, consumed_kg=float(consumed_kg), tick=self.context.clock.now()
            )
            updated = dataclasses.replace(shift, usage=(*shift.usage, entry))
            self._save_shift(updated)
            self.publish(USAGE_KIND, {"active": True, "shift_id": shift_id, "entry": entry.describe()})
            return {"entry": entry.describe(), "shift": updated.describe()}

    def capture_from_plant(self, *, shift_id: str) -> dict[str, Any]:
        """Fold the live production stream into the shift's three ledgers.

        This is the convenience bridge from the control system: declared
        batches become production entries, confirmed loads become loading slips
        and persisted weighings become recipe usage.  Lines already on the
        shift are skipped, so a repeat capture is idempotent.
        """

        with self._lock:
            shift = self.registry.require_mutable(shift_id)
            facts = self._plant_facts_provider()
            now = self.context.clock.now()

            production = list(shift.production)
            known_batches = {item.batch_id for item in production}
            for fact in facts.batches:
                if fact.batch_id in known_batches:
                    continue
                production.append(
                    ProductionEntry(
                        batch_id=fact.batch_id, grade=fact.grade, volume_m3=fact.volume_m3, tick=now
                    )
                )
                known_batches.add(fact.batch_id)

            loading = list(shift.loading)
            known_slips = {item.slip_id for item in loading}
            for fact in facts.loads:
                if fact.slip_id in known_slips or fact.batch_id not in known_batches:
                    continue
                loading.append(
                    LoadingEntry(
                        slip_id=fact.slip_id,
                        truck_id=fact.truck_id,
                        grade=fact.grade,
                        volume_m3=fact.volume_m3,
                        tick=now,
                    )
                )
                known_slips.add(fact.slip_id)

            usage = list(shift.usage)
            known_usage = {(item.grade, item.material) for item in usage}
            for fact in facts.usage:
                key = (fact.grade, fact.material)
                if key in known_usage:
                    continue
                usage.append(
                    UsageEntry(grade=fact.grade, material=fact.material, consumed_kg=fact.consumed_kg, tick=now)
                )
                known_usage.add(key)

            updated = dataclasses.replace(
                shift, production=tuple(production), loading=tuple(loading), usage=tuple(usage)
            )
            self._save_shift(updated)
            self.emit("shift.captured", {"shift_id": shift_id})
            return {"shift": updated.describe()}

    # ------------------------------------------------------------- reconcile
    def reconcile(self, *, shift_id: str, tolerance_m3: float | None = None) -> dict[str, Any]:
        """Compute the reconciliation; persist it once per input fingerprint."""

        with self._lock:
            shift = self.registry.require_shift(shift_id)
            if shift.status == STATUS_CLOSED:
                settlement = self.registry.settlement_for(shift_id)
                return {
                    "reconciliation": shift.reconciliation.describe() if shift.reconciliation else None,
                    "status": STATUS_CLOSED,
                    "idempotent": True,
                    "settlement": None if settlement is None else settlement.describe(),
                }
            tolerance = self._crew_tolerance(shift.crew_id, tolerance_m3)
            reconciliation = self._compute(shift, tolerance_m3=tolerance, tick=self.context.clock.now())
            prior = shift.reconciliation
            changed = prior is None or prior.fingerprint != reconciliation.fingerprint
            if changed:
                updated = dataclasses.replace(shift, reconciliation=reconciliation)
                self._save_shift(updated)
                self.publish(
                    RECONCILE_KIND,
                    {
                        "active": True,
                        "shift_id": shift_id,
                        "status": updated.status,
                        **reconciliation.describe(),
                    },
                )
                self.emit("shift.reconciled", {"shift_id": shift_id, "balanced": reconciliation.balanced})
            return {
                "reconciliation": reconciliation.describe(),
                "status": shift.status,
                "idempotent": not changed,
            }

    # ---------------------------------------------------------------- settle
    def settle(self, *, shift_id: str, tolerance_m3: float | None = None) -> dict[str, Any]:
        """Close the books once.  Safe to trigger concurrently and repeatedly."""

        with self._lock:
            shift = self.registry.require_shift(shift_id)

            # Idempotent fast path: a settled shift replays its own settlement
            # and never writes a second time.
            if shift.status == STATUS_CLOSED:
                settlement = self.registry.settlement_for(shift_id)
                return {
                    "status": STATUS_CLOSED,
                    "result": None if settlement is None else settlement.result,
                    "settlement": None if settlement is None else settlement.describe(),
                    "idempotent": True,
                    "replayed": True,
                }

            crew = self.registry.require_crew(shift.crew_id)
            tolerance = self._crew_tolerance(crew.crew_id, tolerance_m3)
            reconciliation = self._compute(shift, tolerance_m3=tolerance, tick=self.context.clock.now())

            if not reconciliation.balanced and not crew.carries_differences:
                return self._suspend(shift, reconciliation)

            result = RESULT_BALANCED if reconciliation.balanced else RESULT_CARRIED_DIFF
            snapshot = self._settlement_snapshot(shift, reconciliation)
            prev_digest = self.registry.tail_digest()
            settlement_id = self.context.ids.next("settle")
            body = {
                "settlement_id": settlement_id,
                "shift_id": shift_id,
                "crew_id": crew.crew_id,
                "result": result,
                "mismatch_policy": crew.mismatch_policy,
                "batches": reconciliation.batches,
                "produced_m3": reconciliation.produced_m3,
                "loaded_m3": reconciliation.loaded_m3,
                "theoretical_m3": reconciliation.theoretical_m3,
                "fingerprint": reconciliation.fingerprint,
                "differences": [dict(item) for item in reconciliation.differences],
            }
            draft = Settlement(
                settlement_id=settlement_id,
                shift_id=shift_id,
                crew_id=crew.crew_id,
                tick=self.context.clock.now(),
                result=result,
                mismatch_policy=crew.mismatch_policy,
                batches=reconciliation.batches,
                produced_m3=reconciliation.produced_m3,
                loaded_m3=reconciliation.loaded_m3,
                theoretical_m3=reconciliation.theoretical_m3,
                differences=reconciliation.differences,
                fingerprint=reconciliation.fingerprint,
                prev_digest=prev_digest,
                digest=settlement_digest(prev_digest, body),
                snapshot=snapshot,
            )
            record = self.publish(CLOSE_KIND, draft.describe())
            settled = dataclasses.replace(draft, record_seq=record.seq)
            self.registry.settle(settled)
            self.publish(
                RECONCILE_KIND,
                {
                    "active": True,
                    "shift_id": shift_id,
                    "status": STATUS_CLOSED,
                    **reconciliation.describe(),
                },
            )
            self.emit(
                "shift.settled",
                {
                    "shift_id": shift_id,
                    "settlement_id": settlement_id,
                    "result": result,
                    "differences": len(reconciliation.differences),
                    "seq": record.seq,
                },
            )
            return {
                "status": STATUS_CLOSED,
                "result": result,
                "settlement": settled.describe(),
                "idempotent": False,
                "replayed": False,
            }

    def _suspend(self, shift: Shift, reconciliation: Reconciliation) -> dict[str, Any]:
        """Suspend a whole shift; only a changed reconciliation writes again."""

        prior = shift.reconciliation
        changed = prior is None or prior.fingerprint != reconciliation.fingerprint
        newly_suspended = shift.status != STATUS_SUSPENDED
        if changed or newly_suspended:
            suspended = dataclasses.replace(
                shift, status=STATUS_SUSPENDED, reconciliation=reconciliation, last_result=RESULT_SUSPENDED
            )
            self._save_shift(suspended)
            self.publish(
                RECONCILE_KIND,
                {
                    "active": True,
                    "shift_id": shift.shift_id,
                    "status": STATUS_SUSPENDED,
                    **reconciliation.describe(),
                },
            )
            self.emit(
                "shift.suspended",
                {
                    "shift_id": shift.shift_id,
                    "differences": len(reconciliation.differences),
                    "fingerprint": reconciliation.fingerprint,
                },
            )
        return {
            "status": STATUS_SUSPENDED,
            "result": RESULT_SUSPENDED,
            "settlement": None,
            "idempotent": not (changed or newly_suspended),
            "replayed": False,
            "differences": [dict(item) for item in reconciliation.differences],
        }

    # ------------------------------------------------------------- replay io
    def adopt_crew_record(self, payload: Mapping[str, Any]) -> None:
        self.registry.adopt_crew(Crew.from_document(payload))

    def adopt_shift_record(self, payload: Mapping[str, Any]) -> None:
        self.registry.adopt_shift(shift_from_document(payload))

    def adopt_settlement_record(self, payload: Mapping[str, Any], *, record_seq: int) -> None:
        settlement = Settlement.from_document(payload)
        self.registry.adopt_settlement(dataclasses.replace(settlement, record_seq=record_seq))

    # --------------------------------------------------------------- queries
    def get_shift(self, shift_id: str) -> dict[str, Any]:
        shift = self.registry.require_shift(shift_id)
        settlement = self.registry.settlement_for(shift_id)
        return {
            "shift": shift.describe(),
            "crew": self.registry.require_crew(shift.crew_id).describe(),
            "settlement": None if settlement is None else settlement.describe(),
        }

    def list_shifts(self) -> dict[str, Any]:
        return {
            "crews": [crew.describe() for crew in self.registry.crews()],
            "shifts": [
                {
                    "shift": shift.describe(),
                    "settlement": (
                        None
                        if (item := self.registry.settlement_for(shift.shift_id)) is None
                        else item.describe()
                    ),
                }
                for shift in self.registry.shifts()
            ],
        }

    def history(self) -> dict[str, Any]:
        return {
            "settlements": [item.describe() for item in self.registry.settlements()],
            "chain": self.registry.verify_chain(),
        }

    def verify(self) -> dict[str, Any]:
        """Verify the settlement hash chain and every closed shift snapshot."""

        report = self.registry.verify_chain()
        checks: list[dict[str, Any]] = []
        for settlement in self.registry.settlements():
            snapshot = dict(settlement.snapshot)
            reconciliation_doc = snapshot.get("reconciliation") or {}
            factors = snapshot.get("factors_by_grade") or {}
            shift_doc = snapshot.get("shift") or {}
            recomputed = reconcile_shift(
                shift_id=settlement.shift_id,
                production=tuple(
                    ProductionEntry.from_document(item) for item in shift_doc.get("production", ())
                ),
                loading=tuple(
                    LoadingEntry.from_document(item) for item in shift_doc.get("loading", ())
                ),
                usage=tuple(UsageEntry.from_document(item) for item in shift_doc.get("usage", ())),
                factors_by_grade=factors,
                tolerance_m3=0.0,
                tick=settlement.tick,
            )
            ok = recomputed.fingerprint == settlement.fingerprint
            checks.append(
                {
                    "settlement_id": settlement.settlement_id,
                    "shift_id": settlement.shift_id,
                    "fingerprint_ok": ok,
                    "stored_fingerprint": settlement.fingerprint,
                    "recomputed_fingerprint": recomputed.fingerprint,
                }
            )
            if not ok:
                report = dict(report)
                report["ok"] = False
        report["checks"] = checks
        report["freeze_seq"] = self.registry.earliest_settle_seq()
        return report

    def status(self) -> dict[str, Any]:
        return self.registry.status()


class DuplicateProductionError(OrderViolationError):
    code = "duplicate_production_entry"


class DuplicateLoadingError(OrderViolationError):
    code = "duplicate_loading_entry"
