"""Shift reconciliation and settlement behaviour."""

from __future__ import annotations

import threading

import pytest

from mixplant.config import StationConfig
from mixplant.errors import DuplicateError, OrderViolationError, ValidationError
from mixplant.runtime import PlantRuntime
from mixplant.shift.kinds import POLICY_LIST_DIFFS, POLICY_SUSPEND
from tests.support import run, run_full_cycle


def register(rt: PlantRuntime, crew_id: str = "A", policy: str = POLICY_SUSPEND, tol: float = 0.5) -> None:
    if rt.shifts.crews.get(crew_id) is None:
        run(rt, "shift.register_crew", {
            "crew_id": crew_id, "name": f"crew-{crew_id}", "mismatch_policy": policy, "tolerance_m3": tol
        })


def balanced_shift(rt: PlantRuntime, shift_id: str = "S1", *, grade: str = "C30", volume: float = 12.0):
    """Record a shift where trays, slips and recipe usage all agree."""

    register(rt)
    run(rt, "shift.open", {"shift_id": shift_id, "crew_id": "A"})
    run(rt, "shift.record_tray", {"shift_id": shift_id, "ref": "t1", "grade": grade, "volume_m3": volume / 2})
    run(rt, "shift.record_tray", {"shift_id": shift_id, "ref": "t2", "grade": grade, "volume_m3": volume / 2})
    run(rt, "shift.record_slip", {"shift_id": shift_id, "ref": "s1", "grade": grade, "volume_m3": volume})
    run(rt, "shift.record_usage", {
        "shift_id": shift_id, "ref": "u1", "grade": grade,
        "material": "cement", "consumed_kg": volume * 330.0, "factor_kg_per_m3": 330.0,
    })
    run(rt, "shift.close", {"shift_id": shift_id})
    return shift_id


def settle_balanced(rt: PlantRuntime, shift_id: str = "S1") -> dict:
    balanced_shift(rt, shift_id)
    result = run(rt, "shift.settle", {"shift_id": shift_id})
    assert result["settlement"]["outcome"] == "settled"
    return result["settlement"]


# --------------------------------------------------------------------- crews
def test_crew_policy_is_registered_and_described(runtime: PlantRuntime) -> None:
    register(runtime)
    listing = runtime.shifts.list_shifts()
    assert listing["crews"][0]["mismatch_policy"] == POLICY_SUSPEND
    assert listing["crews"][0]["tolerance_m3"] == 0.5


def test_crew_cannot_be_registered_twice_with_another_policy(runtime: PlantRuntime) -> None:
    register(runtime, "A", POLICY_SUSPEND)
    with pytest.raises(DuplicateError):
        run(runtime, "shift.register_crew", {
            "crew_id": "A", "name": "crew-A", "mismatch_policy": POLICY_LIST_DIFFS, "tolerance_m3": 0.5
        })


def test_crew_policy_is_validated(runtime: PlantRuntime) -> None:
    with pytest.raises(ValidationError):
        register(runtime, "A", "make_it_up")


# ------------------------------------------------------------------ lifecycle
def test_open_close_reconcile_settle_balanced(runtime: PlantRuntime) -> None:
    settlement = settle_balanced(runtime)
    assert settlement["outcome"] == "settled"
    assert settlement["totals"]["tray_count"] == 2
    assert settlement["totals"]["produced_m3"] == 12.0
    assert settlement["totals"]["loaded_m3"] == 12.0
    assert settlement["totals"]["recipe_m3"] == 12.0
    line = settlement["lines"][0]
    assert line["grade"] == "C30"
    assert line["differences"] == []
    assert settlement["hash"]
    assert runtime.shifts.verify_chain()["ok"] is True


def test_statement_groups_volumes_by_grade(runtime: PlantRuntime) -> None:
    register(runtime)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C25", "volume_m3": 6})
    run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t2", "grade": "C30", "volume_m3": 8})
    run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "s1", "grade": "C25", "volume_m3": 6})
    run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "s2", "grade": "C30", "volume_m3": 8})
    preview = run(runtime, "shift.preview", {"shift_id": "S1"})
    grades = {line["grade"]: line for line in preview["statement"]["lines"]}
    assert grades["C25"]["tray_count"] == 1
    assert grades["C30"]["tray_count"] == 1
    assert grades["C25"]["produced_m3"] == 6.0
    assert grades["C30"]["loaded_m3"] == 8.0


def test_tray_ref_may_not_be_entered_twice(runtime: PlantRuntime) -> None:
    register(runtime)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 6})
    with pytest.raises(DuplicateError):
        run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 6})


def test_correction_is_appended_not_overwritten(runtime: PlantRuntime) -> None:
    register(runtime)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    recorded = run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 6})
    entry_id = recorded["entry"]["entry_id"]
    run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t2", "grade": "C30", "volume_m3": 6})
    reversed_row = run(runtime, "shift.reverse_entry", {
        "shift_id": "S1", "entry_id": entry_id, "note": "wrong grade sheet"
    })
    assert reversed_row["entry"]["reverses"] == entry_id
    preview = run(runtime, "shift.preview", {"shift_id": "S1"})
    line = preview["statement"]["lines"][0]
    assert line["produced_m3"] == 6.0
    assert line["tray_count"] == 2
    shift = runtime.shifts.get_shift("S1")
    assert len(shift["ledger"]["entries"]) == 3  # original rows are retained


# ------------------------------------------------------------- mismatch policy
def _mismatched_shift(rt: PlantRuntime, policy: str, crew_id: str = "A") -> None:
    register(rt, crew_id, policy)
    run(rt, "shift.open", {"shift_id": "S1", "crew_id": crew_id})
    run(rt, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 12})
    run(rt, "shift.record_slip", {"shift_id": "S1", "ref": "s1", "grade": "C30", "volume_m3": 11})
    run(rt, "shift.record_usage", {
        "shift_id": "S1", "ref": "u1", "grade": "C30",
        "material": "cement", "consumed_kg": 12 * 330, "factor_kg_per_m3": 330,
    })
    run(rt, "shift.close", {"shift_id": "S1"})


def test_suspend_crew_blocks_the_settlement_and_lists_differences(runtime: PlantRuntime) -> None:
    _mismatched_shift(runtime, POLICY_SUSPEND)
    report = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    assert report["shift"]["status"] == "suspended"
    assert report["report"]["has_difference"] is True
    blocked = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert blocked["blocked"] is True
    assert blocked["outcome"] == "suspended"
    assert blocked["settlement"] is None


def test_list_diffs_crew_settles_with_differences_listed(runtime: PlantRuntime) -> None:
    _mismatched_shift(runtime, POLICY_LIST_DIFFS)
    result = run(runtime, "shift.settle", {"shift_id": "S1"})
    settlement = result["settlement"]
    assert settlement["outcome"] == "settled_with_differences"
    kinds = {row["kind"] for row in settlement["differences"]}
    assert "production_vs_loading" in kinds
    gap = next(row for row in settlement["differences"] if row["kind"] == "production_vs_loading")
    assert gap["gap_m3"] == 1.0


def test_tolerance_keeps_small_gaps_clean(runtime: PlantRuntime) -> None:
    register(runtime, "A", POLICY_SUSPEND, tol=0.5)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 12})
    run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "s1", "grade": "C30", "volume_m3": 11.6})
    run(runtime, "shift.close", {"shift_id": "S1"})
    settlement = run(runtime, "shift.settle", {"shift_id": "S1"})["settlement"]
    assert settlement["outcome"] == "settled"
    assert settlement["differences"] == []


def test_suspended_shift_must_be_reopened_before_a_correction(runtime: PlantRuntime) -> None:
    _mismatched_shift(runtime, POLICY_SUSPEND)
    run(runtime, "shift.settle", {"shift_id": "S1"})  # blocks, shift suspended
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "s2", "grade": "C30", "volume_m3": 1})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.capture", {"shift_id": "S1"})


def test_suspended_shift_can_be_reopened_corrected_and_settled(runtime: PlantRuntime) -> None:
    _mismatched_shift(runtime, POLICY_SUSPEND)
    run(runtime, "shift.settle", {"shift_id": "S1"})
    run(runtime, "shift.reopen", {"shift_id": "S1", "reason": "missing slip"})
    run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "s2", "grade": "C30", "volume_m3": 1})
    run(runtime, "shift.close", {"shift_id": "S1"})
    settlement = run(runtime, "shift.settle", {"shift_id": "S1"})["settlement"]
    assert settlement["outcome"] == "settled"
    assert settlement["reopen_count"] == 1
    assert settlement["totals"]["loaded_m3"] == 12.0


# ------------------------------------------------------------------ idempotent
def test_reconcile_is_content_idempotent(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)
    first = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    second = run(runtime, "shift.reconcile", {"shift_id": "S1"})
    assert second.get("unchanged") is True
    assert first["report"]["version"] == second["report"]["version"]
    reconcile_records = [r for r in runtime.store.visible() if r.kind == "shift.reconcile"]
    assert len(reconcile_records) == 1


def test_reconcile_replays_stored_idempotency_key(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)
    payload = {"shift_id": "S1", "idempotency_key": "req-7"}
    first = run(runtime, "shift.reconcile", payload)
    second = run(runtime, "shift.reconcile", payload)
    assert second.get("replayed") is True
    assert first["report"] == second["report"]
    assert len([r for r in runtime.store.visible() if r.kind == "shift.reconcile"]) == 1


def test_settlement_is_idempotent_without_a_key(runtime: PlantRuntime) -> None:
    first = settle_balanced(runtime)
    second = run(runtime, "shift.settle", {"shift_id": "S1"})
    assert second.get("replayed") is True
    assert second["settlement"]["settlement_id"] == first["settlement_id"]
    assert len([r for r in runtime.store.visible() if r.kind == "shift.settle"]) == 1


def test_concurrent_settlement_writes_once(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)
    results: list[dict] = []
    errors: list[Exception] = []

    def trigger() -> None:
        try:
            results.append(runtime.shifts.settle("S1"))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=trigger) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    ids = {item["settlement"]["settlement_id"] for item in results if item.get("settlement")}
    assert ids == {results[0]["settlement"]["settlement_id"]}
    assert len([r for r in runtime.store.visible() if r.kind == "shift.settle"]) == 1


def test_concurrent_settlement_with_same_client_key_writes_once(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)
    results: list[dict] = []

    def trigger() -> None:
        results.append(runtime.shifts.settle("S1", idempotency_key="button-click-1"))

    threads = [threading.Thread(target=trigger) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({item["settlement"]["hash"] for item in results}) == 1
    assert len([r for r in runtime.store.visible() if r.kind == "shift.settle"]) == 1


def test_concurrent_reconcile_writes_once(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)
    results: list[dict] = []

    def trigger() -> None:
        results.append(runtime.shifts.reconcile("S1", idempotency_key="rec-1"))

    threads = [threading.Thread(target=trigger) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(item["report"]["version"] == 1 for item in results)
    assert len([r for r in runtime.store.visible() if r.kind == "shift.reconcile"]) == 1


# ----------------------------------------------------------------- immutability
def test_settled_shift_rejects_every_mutation(runtime: PlantRuntime) -> None:
    settle_balanced(runtime)
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.record_tray", {"shift_id": "S1", "ref": "t9", "grade": "C30", "volume_m3": 6})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.reopen", {"shift_id": "S1", "reason": "too late"})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.close", {"shift_id": "S1"})


def test_settled_reconciliation_is_forbidden(runtime: PlantRuntime) -> None:
    settle_balanced(runtime)
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.reconcile", {"shift_id": "S1"})


def test_settlements_chain_across_shifts(runtime: PlantRuntime) -> None:
    first = settle_balanced(runtime, "S1")
    second = settle_balanced(runtime, "S2")
    assert second["prev_hash"] == first["hash"]
    verification = runtime.shifts.verify_chain()
    assert verification["ok"] is True
    assert verification["head"] == second["hash"]


def test_tampering_with_a_settlement_breaks_the_chain(runtime: PlantRuntime) -> None:
    settle_balanced(runtime, "S1")
    settle_balanced(runtime, "S2")
    shift = runtime.shifts.require_shift("S1")
    shift.settlement["totals"]["produced_m3"] = 999.0
    assert runtime.shifts.verify_chain()["ok"] is False


def test_cannot_open_the_same_shift_twice(runtime: PlantRuntime) -> None:
    register(runtime)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})


def test_entries_are_rejected_after_close(runtime: PlantRuntime) -> None:
    balanced_shift(runtime)  # closes S1
    with pytest.raises(OrderViolationError):
        run(runtime, "shift.record_slip", {"shift_id": "S1", "ref": "sx", "grade": "C30", "volume_m3": 1})


# ------------------------------------------------------------- plant capture
def test_capture_copies_batches_and_loads_into_the_shift(runtime: PlantRuntime) -> None:
    from tests.support import publish_and_select

    publish_and_select(runtime)
    run_full_cycle(runtime, batch_id="B-001", truck_id="T-01")
    register(runtime)
    run(runtime, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    captured = run(runtime, "shift.capture", {"shift_id": "S1"})
    assert captured["captured_batches"] == ["B-001"]
    assert captured["captured_loads"] == ["T-01"]
    # Repeatable: a second capture adds nothing.
    again = run(runtime, "shift.capture", {"shift_id": "S1"})
    assert again["captured_batches"] == []
    assert again["captured_loads"] == []
    preview = run(runtime, "shift.preview", {"shift_id": "S1"})
    line = preview["statement"]["lines"][0]
    assert line["produced_m3"] == 6.0
    assert line["loaded_m3"] == 6.0


# ------------------------------------------------------------- restart / audit
def test_settled_shift_survives_restart(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    settle_balanced(first, "S1")
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    shift = second.shifts.get_shift("S1")
    assert shift["shift"]["status"] == "settled"
    assert shift["settlement"]["totals"]["produced_m3"] == 12.0
    assert len(shift["ledger"]["entries"]) == 4
    assert second.shifts.verify_chain()["ok"] is True
    # History really is immutable after a restart.
    with pytest.raises(OrderViolationError):
        second.shifts.record_tray(shift_id="S1", ref="t9", grade="C30", volume_m3=1)
    second.close()


def test_suspended_shift_survives_restart(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    _mismatched_shift(first, POLICY_SUSPEND)
    run(first, "shift.reconcile", {"shift_id": "S1"})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    shift = second.shifts.require_shift("S1")
    assert shift.status == "suspended"
    assert shift.latest_report["version"] == 1
    blocked = second.shifts.settle("S1")
    assert blocked["blocked"] is True
    second.close()


def test_crew_directory_survives_restart_with_fixed_policy(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    register(first, "A", POLICY_LIST_DIFFS)
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    crew = second.shifts.crews.require("A")
    assert crew.mismatch_policy == POLICY_LIST_DIFFS
    with pytest.raises(DuplicateError):
        second.shifts.register_crew(
            crew_id="A", name="crew-A", mismatch_policy=POLICY_SUSPEND, tolerance_m3=0.5
        )
    second.close()


def test_restart_does_not_reissue_entry_ids(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    register(first)
    run(first, "shift.open", {"shift_id": "S1", "crew_id": "A"})
    entry = run(first, "shift.record_tray", {"shift_id": "S1", "ref": "t1", "grade": "C30", "volume_m3": 6})
    first_id = entry["entry"]["entry_id"]
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    after = run(second, "shift.record_tray", {"shift_id": "S1", "ref": "t2", "grade": "C30", "volume_m3": 6})
    assert after["entry"]["entry_id"] != first_id
    second.close()


def test_settlement_writes_an_audit_event(runtime: PlantRuntime) -> None:
    settle_balanced(runtime)
    event_records = [
        record for record in runtime.store.visible()
        if record.kind == "audit.event" and record.payload.get("name") == "shift.settled"
    ]
    assert event_records
    names = {entry["name"] for entry in runtime.audit.recent(50)}
    assert "shift.settled" in names


def test_settlement_idempotency_key_survives_restart(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    balanced_shift(first, "S1")
    first.shifts.settle("S1", idempotency_key="closeout-9")
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    replayed = second.shifts.settle("S1", idempotency_key="closeout-9")
    assert replayed.get("replayed") is True
    assert replayed["settlement"]["idempotency_key"] == "closeout-9"
    assert len([r for r in second.store.visible() if r.kind == "shift.settle"]) == 1
    second.close()


def test_shifts_are_listed_for_the_console(runtime: PlantRuntime) -> None:
    settle_balanced(runtime, "S1")
    listing = runtime.shifts.list_shifts()
    assert [item["shift_id"] for item in listing["shifts"]] == ["S1"]
    assert listing["shifts"][0]["settled"] is True
    open_only = runtime.shifts.list_shifts(include_settled=False)
    assert open_only["shifts"] == []


def test_status_surface_reports_the_chain(runtime: PlantRuntime) -> None:
    settle_balanced(runtime)
    status = runtime.shifts.status()
    assert status["settled"] == 1
    assert status["chain_ok"] is True


def test_unknown_shift_is_not_found(runtime: PlantRuntime) -> None:
    from mixplant.errors import NotFoundError

    register(runtime)
    with pytest.raises(NotFoundError):
        run(runtime, "shift.close", {"shift_id": "GHOST"})
