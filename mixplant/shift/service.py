"""Shift reconciliation and settlement service.

Concurrency
-----------
The console serves requests on worker threads.  Every command that can mutate a
shift runs inside one process wide reentrant lock, and the durable journal is
the cross process guard: the *state* of a shift (``reconciled`` / ``suspended``
/ ``settled``) plus the unique settlement record make repeated triggers
idempotent even after a crash or from a second process.

Idempotency
-----------
Callers may attach an ``idempotency_key`` to ``reconcile`` and ``settle``.  The
first result for a key is stored *inside the durable record* and returned again
for a retried call without writing anything — the mapping is rebuilt on
restart.  Settlement additionally refuses a second closeout from state: once a
shift is ``settled`` the stored snapshot is returned verbatim, so even a
keyless retry (or a second process after a crash) cannot rewrite the shift.

Immutability
------------
Settled shifts reject every mutating command (the domain layer enforces this
regardless of how the request arrived), and each settlement snapshot is chained
by sha256 to the previous one.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from ..service_base import PlantService
from .crew import Crew, CrewDirectory
from .entries import Entry, reversal, slip, tray, usage
from .hashing import settlement_hash
from .kinds import (
    CLOSE_KIND,
    CREW_KIND,
    ENTRY_KIND,
    OPEN_KIND,
    POLICY_SUSPEND,
    RECONCILE_KIND,
    REOPEN_KIND,
    SETTLE_KIND,
    STATUS_CLOSED,
    STATUS_RECONCILED,
    STATUS_SETTLED,
    STATUS_SUSPENDED,
)
from .reconcile import build_statement
from .shiftmodel import SETTLEABLE, Shift, ShiftRegistry

OUTCOME_SETTLED = "settled"
OUTCOME_SETTLED_WITH_DIFFS = "settled_with_differences"
OUTCOME_SUSPENDED = "suspended"

RECONCILE_ACTION = "reconcile"
SETTLE_ACTION = "settle"

GENESIS_HASH = ""


class ShiftService(PlantService):
    """Registers crews, runs the per shift ledgers and closes shifts out."""

    origin = "shift"
    line = "batch"
    subject = "shift"

    def __init__(self, context, machine=None) -> None:
        super().__init__(context, machine)
        self.crews = CrewDirectory()
        self._registry = ShiftRegistry()
        # One lock for every shift: settlement identity must be decided
        # atomically with the journal write.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ crews
    def register_crew(
        self, *, crew_id: str, name: str, mismatch_policy: str, tolerance_m3: float = 0.5
    ) -> dict[str, Any]:
        """Register a crew with its permanently fixed mismatch policy."""

        crew = Crew(
            crew_id=crew_id,
            name=name,
            mismatch_policy=mismatch_policy,
            tolerance_m3=float(tolerance_m3),
        )
        with self._lock:
            self.crews.register(crew)
            self.publish(CREW_KIND, crew.describe())
            self.emit("shift.crew_registered", crew.describe())
        return {"crew": crew.describe()}

    # ------------------------------------------------------------------ shifts
    def open_shift(self, *, shift_id: str, crew_id: str, note: str = "") -> dict[str, Any]:
        """Open a new shift for a registered crew."""

        with self._lock:
            crew = self.crews.require(crew_id)
            shift = Shift(shift_id=shift_id, crew=crew, opened_tick=self.context.clock.now())
            self._registry.add(shift)
            payload = {
                "active": True,
                "shift_id": shift_id,
                "crew_id": crew_id,
                "note": note,
                "opened_tick": shift.opened_tick,
            }
            self.publish(OPEN_KIND, payload)
            self.emit("shift.opened", {"shift_id": shift_id, "crew_id": crew_id})
            return {"shift": shift.describe()}

    def close_shift(self, shift_id: str) -> dict[str, Any]:
        """End data entry for a shift. Only reconciliation/settlement remain."""

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_not_settled("closing")
            if shift.status == STATUS_CLOSED:
                return {"shift": shift.describe(), "noop": True}
            shift.require_status("open")
            shift.status = STATUS_CLOSED
            shift.closed_tick = self.context.clock.now()
            self.publish(
                CLOSE_KIND,
                {
                    "active": True,
                    "shift_id": shift_id,
                    "closed_tick": shift.closed_tick,
                },
            )
            self.emit("shift.closed", {"shift_id": shift_id})
            return {"shift": shift.describe()}

    def reopen_shift(self, shift_id: str, *, reason: str) -> dict[str, Any]:
        """Reopen a suspended shift so corrections can be appended."""

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_not_settled("reopening")
            shift.require_status(STATUS_SUSPENDED)
            shift.status = "open"
            shift.reopen_count += 1
            self.publish(
                REOPEN_KIND,
                {
                    "active": True,
                    "shift_id": shift_id,
                    "reason": reason,
                    "reopen_count": shift.reopen_count,
                },
            )
            self.emit("shift.reopened", {"shift_id": shift_id, "reason": reason})
            return {"shift": shift.describe()}

    # ------------------------------------------------------------------ ledger
    def _next_entry_id(self, shift: Shift) -> str:
        return f"{self.context.ids.next('shentry')}-{shift.shift_id}"

    def _add_entry(self, shift: Shift, entry: Entry) -> Entry:
        shift.ledger.add(entry)
        self.publish(ENTRY_KIND, entry.describe())
        self.emit(
            "shift.entry_recorded",
            {"shift_id": shift.shift_id, "entry_id": entry.entry_id, "source": entry.source},
        )
        return entry

    def record_tray(
        self, *, shift_id: str, ref: str, grade: str, volume_m3: float, note: str = "", origin: str = "console"
    ) -> dict[str, Any]:
        """Record one production tray (打盘) row."""

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_writable()
            entry = tray(
                entry_id=self._next_entry_id(shift),
                shift_id=shift_id,
                ref=ref,
                grade=grade,
                volume_m3=float(volume_m3),
                tick=self.context.clock.now(),
                origin=origin,
                note=note,
            )
            self._add_entry(shift, entry)
            return {"entry": entry.describe(), "entries": len(shift.ledger.entries())}

    def record_slip(
        self, *, shift_id: str, ref: str, grade: str, volume_m3: float, note: str = "", origin: str = "console"
    ) -> dict[str, Any]:
        """Record one loading slip (装车单) row."""

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_writable()
            entry = slip(
                entry_id=self._next_entry_id(shift),
                shift_id=shift_id,
                ref=ref,
                grade=grade,
                volume_m3=float(volume_m3),
                tick=self.context.clock.now(),
                origin=origin,
                note=note,
            )
            self._add_entry(shift, entry)
            return {"entry": entry.describe(), "entries": len(shift.ledger.entries())}

    def record_usage(
        self,
        *,
        shift_id: str,
        ref: str,
        grade: str,
        material: str,
        consumed_kg: float,
        factor_kg_per_m3: float | None = None,
        recipe_name: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Record one recipe usage (配方用量) row.

        The kg/m3 factor defaults to the factor of the named recipe (or the
        currently selected recipe) for the material.
        """

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_writable()
            factor = self._resolve_factor(
                grade=grade,
                material=material,
                factor_kg_per_m3=factor_kg_per_m3,
                recipe_name=recipe_name,
            )
            entry = usage(
                entry_id=self._next_entry_id(shift),
                shift_id=shift_id,
                ref=ref,
                grade=grade,
                material=material,
                consumed_kg=float(consumed_kg),
                factor_kg_per_m3=factor,
                tick=self.context.clock.now(),
                note=note,
            )
            self._add_entry(shift, entry)
            return {"entry": entry.describe(), "entries": len(shift.ledger.entries())}

    def reverse_entry(self, *, shift_id: str, entry_id: str, note: str) -> dict[str, Any]:
        """Append the reversal that cancels one earlier row.

        Only an open shift can be corrected (a suspended shift has to be
        reopened first, which records why), and the correction is itself an
        append — the original row is never deleted.
        """

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_writable()
            original = shift.ledger.get(entry_id)
            correction = reversal(
                self._next_entry_id(shift), original, tick=self.context.clock.now(), note=note
            )
            self._add_entry(shift, correction)
            return {"entry": correction.describe(), "entries": len(shift.ledger.entries())}

    def _resolve_factor(
        self,
        *,
        grade: str,
        material: str,
        factor_kg_per_m3: float | None,
        recipe_name: str | None,
    ) -> float:
        if factor_kg_per_m3 is not None:
            return float(factor_kg_per_m3)
        if recipe_name:
            recipe = self.context.recipes.book.current(recipe_name)
        else:
            try:
                recipe = self.context.recipes.book.selected()
            except Exception:  # pragma: no cover - narrow the re-raise below
                recipe = None
            if recipe is not None and recipe.grade.code != grade:
                recipe = None
        if recipe is None:
            from ..errors import ValidationError

            raise ValidationError(
                "recipe factor is required when no matching recipe is selected",
                grade=grade,
                material=material,
            )
        return float(recipe.factor(material))

    # ------------------------------------------------------------- reconcile
    def preview(self, shift_id: str) -> dict[str, Any]:
        """Compute the current statement without writing anything."""

        with self._lock:
            shift = self._registry.require(shift_id)
            statement = build_statement(
                shift.ledger.entries(),
                shift_id=shift_id,
                tolerance_m3=shift.crew.tolerance_m3,
                tick=self.context.clock.now(),
            )
            return {"statement": statement.describe(), "shift": shift.describe()}

    def reconcile(self, shift_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Reconcile the three ledgers and persist the versioned report."""

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_not_settled("reconciliation")
            cached = shift.idempotent_result(RECONCILE_ACTION, idempotency_key)
            if cached is not None:
                return {"report": cached, "replayed": True}

            statement = build_statement(
                shift.ledger.entries(),
                shift_id=shift_id,
                tolerance_m3=shift.crew.tolerance_m3,
                tick=self.context.clock.now(),
            )
            version = len(shift.reports) + 1
            report = {"version": version, **statement.describe()}

            # Content idempotency: same ledgers reconciled again reuse the
            # existing version and append nothing.
            existing = shift.latest_report
            if existing is not None and existing.get("entries_fingerprint") == report["entries_fingerprint"]:
                if idempotency_key:
                    shift.store_idempotent(RECONCILE_ACTION, idempotency_key, existing)
                return {"report": existing, "unchanged": True}

            shift.reports.append(report)
            record_payload = {"active": True, **report}
            if idempotency_key:
                record_payload["idempotency_key"] = idempotency_key
            self.publish(RECONCILE_KIND, record_payload)
            if statement.has_difference:
                shift.status = STATUS_SUSPENDED if shift.crew.mismatch_policy == POLICY_SUSPEND else STATUS_RECONCILED
            else:
                shift.status = STATUS_RECONCILED
            if idempotency_key:
                shift.store_idempotent(RECONCILE_ACTION, idempotency_key, report)
            self.emit(
                "shift.reconciled",
                {
                    "shift_id": shift_id,
                    "version": version,
                    "has_difference": statement.has_difference,
                    "status": shift.status,
                },
            )
            return {"report": report, "shift": shift.describe()}

    # ---------------------------------------------------------------- settle
    def settle(self, shift_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Close a shift out once and freeze its snapshot.

        Repeating the trigger returns the first settlement: a stored
        idempotency key wins first, then the shift's ``settled`` state.
        """

        with self._lock:
            shift = self._registry.require(shift_id)

            cached = shift.idempotent_result(SETTLE_ACTION, idempotency_key)
            if cached is not None:
                return {"settlement": cached, "replayed": True}
            if shift.is_settled:
                return {"settlement": shift.settlement, "replayed": True}

            if shift.status not in SETTLEABLE:
                shift.require_status(*SETTLEABLE)  # raises the typed error
            if shift.status == STATUS_CLOSED:
                # Closing without an explicit reconciliation still settles on a
                # fresh computation.
                self.reconcile(shift_id)
                shift = self._registry.require(shift_id)

            report = shift.latest_report
            outcome = self._outcome(shift)
            if outcome == OUTCOME_SUSPENDED:
                self.emit("shift.settle_blocked", {"shift_id": shift_id})
                return {
                    "settlement": None,
                    "blocked": True,
                    "outcome": OUTCOME_SUSPENDED,
                    "shift": shift.describe(),
                    "report": report,
                }

            tick = self.context.clock.now()
            settlement_id = self.context.ids.next("shsettle")
            snapshot = self._snapshot(shift, report)
            document = {
                "active": True,
                "settlement_id": settlement_id,
                "shift_id": shift_id,
                "crew": shift.crew.describe(),
                "outcome": outcome,
                "tick": tick,
                "opened_tick": shift.opened_tick,
                "closed_tick": shift.closed_tick or tick,
                "reopen_count": shift.reopen_count,
                "report_version": None if report is None else report.get("version"),
                "entries_fingerprint": None if report is None else report.get("entries_fingerprint"),
                "totals": None if report is None else report.get("totals"),
                "lines": [] if report is None else report.get("lines", []),
                "differences": self._all_differences(report),
                "snapshot": snapshot,
            }
            if idempotency_key:
                document["idempotency_key"] = idempotency_key
            prev_hash = self._registry.last_settlement_hash()
            document["prev_hash"] = prev_hash
            # ``origin`` is part of the stored document (PlantService.publish
            # injects it), so it must be present before the hash is taken for
            # the in memory copy and a restart replay to hash identically.
            document["origin"] = self.origin
            document["hash"] = settlement_hash(document, prev_hash)

            record = self.publish(SETTLE_KIND, document)
            document["seq"] = record.seq
            shift.settlement = document
            shift.status = STATUS_SETTLED
            if idempotency_key:
                shift.store_idempotent(SETTLE_ACTION, idempotency_key, document)
            self.emit(
                "shift.settled",
                {
                    "shift_id": shift_id,
                    "settlement_id": settlement_id,
                    "outcome": outcome,
                    "hash": document["hash"],
                },
            )
            return {"settlement": document, "shift": shift.describe()}

    def _outcome(self, shift: Shift) -> str:
        report = shift.latest_report
        has_diff = bool(report and report.get("has_difference"))
        if not has_diff:
            return OUTCOME_SETTLED
        if shift.crew.mismatch_policy == POLICY_SUSPEND:
            return OUTCOME_SUSPENDED
        return OUTCOME_SETTLED_WITH_DIFFS

    def _all_differences(self, report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if not report:
            return []
        rows: list[dict[str, Any]] = []
        for line in report.get("lines", []):
            rows.extend(line.get("differences", []))
        return rows

    def _snapshot(self, shift: Shift, report: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "shift_id": shift.shift_id,
            "crew": shift.crew.describe(),
            "entries": [entry.describe() for entry in shift.ledger.entries()],
            "report": None if report is None else dict(report),
            "captured_tick": self.context.clock.now(),
        }

    # ------------------------------------------------------------- capture
    def capture_plant(self, shift_id: str) -> dict[str, Any]:
        """Copy the plant's own batches/truck loads into a shift.

        This is the bridge between the control system and the handover sheet:
        every declared production batch becomes a production tray and every
        confirmed (or recorded) truck load becomes a loading slip.  Already
        captured references are skipped, so the command is repeatable.
        """

        with self._lock:
            shift = self._registry.require(shift_id)
            shift.require_writable()
            captured_batches: list[str] = []
            captured_loads: list[str] = []
            skipped_loads: list[dict[str, Any]] = []
            existing_refs = {(entry.source, entry.ref) for entry in shift.ledger.entries()}

            for batch in self.context.batches.all():
                ref = f"batch:{batch.batch_id}"
                if ("production", ref) in existing_refs:
                    continue
                entry = tray(
                    entry_id=self._next_entry_id(shift),
                    shift_id=shift_id,
                    ref=ref,
                    grade=batch.grade,
                    volume_m3=batch.quantity,
                    tick=max(batch.tick, shift.opened_tick),
                    origin="plant",
                    note=f"generation {batch.generation}",
                )
                self._add_entry(shift, entry)
                existing_refs.add(("production", ref))
                captured_batches.append(batch.batch_id)

            for record in self._plant_load_records():
                ref = f"slip:{record['truck_id']}"
                if ("loading", ref) in existing_refs:
                    continue
                grade = str(record.get("grade") or "")
                if not grade:
                    # A load whose batch is unknown cannot be placed under a
                    # grade; surface it for manual entry instead of failing.
                    skipped_loads.append(
                        {"truck_id": record["truck_id"], "batch_id": record.get("batch_id", "")}
                    )
                    continue
                entry = slip(
                    entry_id=self._next_entry_id(shift),
                    shift_id=shift_id,
                    ref=ref,
                    grade=grade,
                    volume_m3=float(record["volume_m3"]),
                    tick=int(record.get("tick", self.context.clock.now())),
                    origin="plant",
                    note=f"truck {record['truck_id']}",
                )
                self._add_entry(shift, entry)
                existing_refs.add(("loading", ref))
                captured_loads.append(str(record["truck_id"]))

            return {
                "captured_batches": captured_batches,
                "captured_loads": captured_loads,
                "skipped_loads": skipped_loads,
                "entries": len(shift.ledger.entries()),
            }

    def _plant_load_records(self) -> tuple[dict[str, Any], ...]:
        """Read chute load rows from the durable stream (restart safe)."""

        rows: list[dict[str, Any]] = []
        batches = {batch.batch_id: batch for batch in self.context.batches.all()}
        for record in self.store.history("chute.load"):
            payload = record.payload
            if payload.get("confirmed"):
                continue  # the confirmation rewrites the same logical row
            batch_id = str(payload.get("batch_id", ""))
            batch = batches.get(batch_id)
            rows.append(
                {
                    "truck_id": payload.get("truck_id"),
                    "batch_id": batch_id,
                    "grade": "" if batch is None else batch.grade,
                    "volume_m3": payload.get("volume_m3"),
                    "tick": record.tick,
                }
            )
        return tuple(rows)

    # ------------------------------------------------------------- queries
    def require_shift(self, shift_id: str) -> Shift:
        """Return the live shift aggregate (console/controller use)."""

        with self._lock:
            return self._registry.require(shift_id)

    def get_shift(self, shift_id: str) -> dict[str, Any]:
        shift = self._registry.require(shift_id)
        with self._lock:
            return {
                "shift": shift.describe(),
                "ledger": shift.ledger.describe(),
                "report": shift.latest_report,
                "settlement": shift.settlement,
            }

    def list_shifts(self, *, include_settled: bool = True) -> dict[str, Any]:
        with self._lock:
            shifts = self._registry.all()
            if not include_settled:
                shifts = tuple(shift for shift in shifts if not shift.is_settled)
            return {
                "crews": self.crews.describe(),
                "shifts": [shift.describe() for shift in shifts],
            }

    def settlements(self) -> dict[str, Any]:
        """Return every frozen settlement in closeout order."""

        with self._lock:
            return {
                "settlements": [
                    shift.settlement
                    for shift in self._registry.all()
                    if shift.settlement is not None
                ]
            }

    def verify_chain(self) -> dict[str, Any]:
        """Recompute every settlement hash and check the chain."""

        with self._lock:
            prev_hash = GENESIS_HASH
            checked: list[dict[str, Any]] = []
            ok = True
            for shift in self._registry.all():
                document = shift.settlement
                if document is None:
                    continue
                stored_hash = str(document.get("hash", ""))
                stored_prev = str(document.get("prev_hash", ""))
                expected = settlement_hash(document, prev_hash)
                row_ok = stored_hash == expected and stored_prev == prev_hash
                ok = ok and row_ok
                checked.append(
                    {
                        "shift_id": shift.shift_id,
                        "settlement_id": document.get("settlement_id"),
                        "ok": row_ok,
                        "prev_ok": stored_prev == prev_hash,
                    }
                )
                prev_hash = stored_hash
            return {"ok": ok, "head": prev_hash, "settlements": checked}

    def status(self) -> dict[str, Any]:
        with self._lock:
            shifts = self._registry.all()
            return {
                "crews": len(self.crews.all()),
                "shifts": len(shifts),
                "open": sum(1 for shift in shifts if not shift.is_settled),
                "settled": sum(1 for shift in shifts if shift.is_settled),
                "chain_ok": self.verify_chain()["ok"],
            }

    # ------------------------------------------------------------- restore
    def hydrate(self) -> None:
        """Rebuild crews and shifts from the surviving journal records."""

        with self._lock:
            for record in self.store.visible():
                kind = record.kind
                payload = record.payload
                if kind == CREW_KIND:
                    self.crews.adopt(Crew.from_document(dict(payload)))
                elif kind == OPEN_KIND:
                    crew = self.crews.require(str(payload["crew_id"]))
                    shift = Shift(
                        shift_id=str(payload["shift_id"]),
                        crew=crew,
                        opened_tick=int(payload.get("opened_tick", record.tick)),
                    )
                    self._registry.adopt(shift)
                elif kind == ENTRY_KIND:
                    shift = self._registry.get(str(payload["shift_id"]))
                    if shift is not None:
                        shift.ledger.add(Entry.from_document(dict(payload)))
                elif kind == RECONCILE_KIND:
                    shift = self._registry.get(str(payload["shift_id"]))
                    if shift is not None:
                        stored_key = payload.get("idempotency_key")
                        report = {
                            key: value
                            for key, value in dict(payload).items()
                            if key not in ("active", "idempotency_key")
                        }
                        shift.reports.append(report)
                        shift.status = self._status_from_report(shift, report)
                        if stored_key:
                            shift.store_idempotent(RECONCILE_ACTION, str(stored_key), report)
                elif kind == CLOSE_KIND:
                    shift = self._registry.get(str(payload["shift_id"]))
                    if shift is not None and shift.status == "open":
                        shift.status = STATUS_CLOSED
                        shift.closed_tick = int(payload.get("closed_tick", record.tick))
                elif kind == REOPEN_KIND:
                    shift = self._registry.get(str(payload["shift_id"]))
                    if shift is not None:
                        shift.status = "open"
                        shift.reopen_count = int(payload.get("reopen_count", shift.reopen_count + 1))
                elif kind == SETTLE_KIND:
                    shift = self._registry.get(str(payload["shift_id"]))
                    if shift is not None:
                        document = dict(payload)
                        stored_key = document.get("idempotency_key")
                        shift.settlement = document
                        shift.status = STATUS_SETTLED
                        if stored_key:
                            shift.store_idempotent(SETTLE_ACTION, str(stored_key), document)

    def _status_from_report(self, shift: Shift, report: Mapping[str, Any]) -> str:
        if not report.get("has_difference"):
            return STATUS_RECONCILED
        return STATUS_SUSPENDED if shift.crew.mismatch_policy == POLICY_SUSPEND else STATUS_RECONCILED
