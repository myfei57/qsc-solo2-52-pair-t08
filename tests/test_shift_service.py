"""Shift settlement service: policies, idempotency, immutability and replay."""

from __future__ import annotations

import threading

import pytest

from mixplant.config import StationConfig
from mixplant.errors import OrderViolationError, RecordError, ValidationError
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select, run, run_full_cycle


def _crew(runtime: PlantRuntime, crew_id: str = "A", policy: str = "continue", tolerance: float = 0.0) -> None:
    run(
        runtime,
        "shift.crew",
        {"crew_id": crew_id, "mismatch_policy": policy, "tolerance_m3": tolerance},
    )


def _balanced_shift(runtime: PlantRuntime, shift_id: str = "S1", crew_id: str = "A") -> None:
    run(runtime, "shift.open", {"shift_id": shift_id, "crew_id": crew_id})
    run(runtime, "shift.production", {"shift_id": shift_id, "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    run(runtime, "shift.production", {"shift_id": shift_id, "batch_id": "B2", "grade": "C30", "volume_m3": 6})
    run(
        runtime,
        "shift.loading",
        {"shift_id": shift_id, "slip_id": "L1", "truck_id": "T1", "grade": "C30", "volume_m3": 12},
    )
    run(runtime, "shift.usage", {"shift_id": shift_id, "grade": "C30", "material": "cement", "consumed_kg": 3960})


# --------------------------------------------------------------------- setup
def test_crew_policy_is_required_and_fixed(runtime: PlantRuntime) -> None:
    with pytest.raises(ValidationError):
        run(runtime, "shift.crew", {"crew_id": "A", "mismatch_policy": "make_it_up"})
    _crew(runtime, "A", "continue")
    with pytest.raises(Exception):
        _crew(runtime, "A", "suspend")
    crew = runtime.shifts.registry.require_crew("A")
    assert crew.mismatch_policy == "continue"
    assert crew.carries_differences is True


def test_a_crew_has_one_active_shift(runtime: PlantRuntime) -> None:
    _crew(runtime, "A")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.open", {"shift_id": "S2", "crew_id": "A"})


# ------------------------------------------------------------- reconciliation
def test_reconcile_runs_without_closing(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    result = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    assert result["status"] == "OPEN"
    assert result["reconciliation"]["balanced"] is True


def test_reconcile_is_idempotent_for_unchanged_inputs(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    first = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    seq_after_first = runtime.store.last_seq()
    second = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    assert second["idempotent"] is True
    assert runtime.store.last_seq() == seq_after_first
    assert first["reconciliation"]["fingerprint"] == second["reconciliation"]["fingerprint"]


# ----------------------------------------------------------------- settlement
def test_balanced_shift_settles_once_and_is_then_immutable(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    settled = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert settled["status"] == "CLOSED"
    assert settled["result"] == "BALANCED"
    assert settled["idempotent"] is False

    with pytest.raises(OrderViolationError):
        run(runtime, "shift.production", {"shift_id": "S1", "batch_id": "BX", "grade": "C30", "volume_m3": 1})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "LX", "grade": "C30", "volume_m3": 1})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.usage", {"shift_id": "S1", "grade": "C30", "material": "sand", "consumed_kg": 1})


def test_second_settlement_trigger_writes_nothing(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    run(runtime, "shift.settle", {"shift_id": "S1"})
    seq = runtime.store.last_seq()
    again = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert again["idempotent"] is True
    assert again["replayed"] is True
    assert runtime.store.last_seq() == seq


def test_suspend_crew_holds_the_whole_shift(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "suspend")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.production", {"shift_id": "S1", "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "L1", "grade": "C30", "volume_m3": 5})
    held = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert held["status"] == "SUSPENDED"
    assert held["result"] == "SUSPENDED"
    assert held["settlement"] is None
    assert runtime.shifts.registry.require_shift("S1").status == "SUSPENDED"

    seq = runtime.store.last_seq()
    retry = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert retry["status"] == "SUSPENDED"
    assert retry["idempotent"] is True
    assert runtime.store.last_seq() == seq


def test_continue_crew_carries_the_differences(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.production", {"shift_id": "S1", "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "L1", "grade": "C30", "volume_m3": 5})
    settled = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert settled["status"] == "CLOSED"
    assert settled["result"] == "CLOSED_WITH_DIFF"
    assert len(settled["settlement"]["differences"]) >= 1


def test_suspended_shift_can_be_fixed_then_settled(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "suspend")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.production", {"shift_id": "S1", "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "L1", "grade": "C30", "volume_m3": 5})
    run(runtime, "shift.settle", {"shift_id": "S1"})
    run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "L2", "grade": "C30", "volume_m3": 1})
    run(runtime, "shift.usage", {"shift_id": "S1", "grade": "C30", "material": "cement", "consumed_kg": 1980})
    settled = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert settled["status"] == "CLOSED"
    assert settled["result"] == "BALANCED"


# ---------------------------------------------------------------- concurrency
def test_concurrent_settle_is_idempotent(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    results: list[dict] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(run(runtime, "shift.settle", {"shift_id": "S1"}))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    closes = [record for record in runtime.store.committed_records() if record.kind == "shift.close"]
    assert len(closes) == 1
    writers = [item for item in results if not item["idempotent"]]
    assert len(writers) == 1


def test_concurrent_settle_suspend_only_suspends_once(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "suspend")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.production", {"shift_id": "S1", "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    run(runtime, "shift.loading", {"shift_id": "S1", "slip_id": "L1", "grade": "C30", "volume_m3": 5})
    barrier = threading.Barrier(6)
    outcomes: list[dict] = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(run(runtime, "shift.settle", {"shift_id": "S1"}))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(item["status"] == "SUSPENDED" for item in outcomes)
    opens = [record for record in runtime.store.committed_records() if record.kind == "shift.open"]
    # Exactly one open leaf ever carried the SUSPENDED transition.
    suspended_opens = [record for record in opens if record.payload.get("status") == "SUSPENDED"]
    assert len(suspended_opens) == 1


# -------------------------------------------------------------- immutability
def test_rollback_cannot_touch_settled_history(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime)
    run(runtime, "shift.settle", {"shift_id": "S1"})
    close_seq = runtime.shifts.registry.earliest_settle_seq()
    assert close_seq is not None
    with pytest.raises(RecordError):
        runtime.rollback(close_seq - 1, reason="try to erase the close")
    with pytest.raises(RecordError):
        runtime.rollback(1, reason="deep rollback")


def test_settlement_chain_verifies(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    _crew(runtime, "A", "continue")
    _balanced_shift(runtime, "S1")
    run(runtime, "shift.settle", {"shift_id": "S1"})
    run(runtime, "shift.open", {"shift_id": "S2", "crew_id": "A"})
    run(runtime, "shift.production", {"shift_id": "S2", "batch_id": "C1", "grade": "C30", "volume_m3": 3})
    run(runtime, "shift.loading", {"shift_id": "S2", "slip_id": "M1", "grade": "C30", "volume_m3": 3})
    run(runtime, "shift.settle", {"shift_id": "S2"})
    report = runtime.shifts.verify()
    assert report["ok"] is True
    assert len(report["checks"]) == 2
    assert all(check["fingerprint_ok"] for check in report["checks"])


# ------------------------------------------------------------------ capture
def test_capture_from_plant_is_idempotent(runtime: PlantRuntime) -> None:
    run_full_cycle(runtime)
    _crew(runtime, "A", "continue")
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    first = run(runtime, "shift.capture", {"shift_id": "S1"})["shift"]
    second = run(runtime, "shift.capture", {"shift_id": "S1"})["shift"]
    assert len(first["production"]) == 1
    assert len(second["production"]) == 1
    assert len(second["loading"]) == 1
    assert second["loading"][0]["grade"] == "C30"


# ------------------------------------------------------------------ restart
def test_restart_rebuilds_closed_history_and_freeze_line(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state", web_dir=__import__("pathlib").Path("/workspace/web"))
    first = PlantRuntime(config)
    first.bootstrap()
    publish_and_select(first)
    _crew(first, "A", "continue")
    _balanced_shift(first)
    settled = run(first, "shift.settle", {"shift_id": "S1"})
    tail = first.shifts.history()["chain"]["tail"]
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    assert second.shifts.history()["chain"]["ok"] is True
    assert second.shifts.history()["chain"]["tail"] == tail
    got = second.shifts.get_shift("S1")
    assert got["shift"]["status"] == "CLOSED"
    assert got["settlement"]["digest"] == settled["settlement"]["digest"]

    before = second.store.last_seq()
    again = run(second, "shift.settle", {"shift_id": "S1"})
    assert again["idempotent"] is True
    assert second.store.last_seq() == before

    with pytest.raises(RecordError):
        second.rollback(1, reason="after restart")
    second.close()
