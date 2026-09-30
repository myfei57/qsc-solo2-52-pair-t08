"""Pure reconciliation: per grade totals, three way differences, fingerprint."""

from __future__ import annotations

from mixplant.shift.models import LoadingEntry, ProductionEntry, UsageEntry
from mixplant.shift.reconcile import build_fingerprint, reconcile_shift

FACTORS = {"C30": {"cement": 330.0, "sand": 760.0}, "C20": {"cement": 260.0}}


def _prod(batch_id: str, grade: str, volume: float) -> ProductionEntry:
    return ProductionEntry(batch_id=batch_id, grade=grade, volume_m3=volume, tick=1)


def _load(slip_id: str, grade: str, volume: float, truck: str = "T") -> LoadingEntry:
    return LoadingEntry(slip_id=slip_id, truck_id=truck, grade=grade, volume_m3=volume, tick=1)


def _usage(grade: str, material: str, kg: float) -> UsageEntry:
    return UsageEntry(grade=grade, material=material, consumed_kg=kg, tick=1)


def _balanced_sources():
    production = (_prod("B1", "C30", 6), _prod("B2", "C30", 6))
    loading = (_load("L1", "C30", 12),)
    usage = (_usage("C30", "cement", 330 * 12), _usage("C30", "sand", 760 * 12))
    return production, loading, usage


def test_balanced_shift_has_no_differences() -> None:
    production, loading, usage = _balanced_sources()
    result = reconcile_shift(
        shift_id="S1", production=production, loading=loading, usage=usage, factors_by_grade=FACTORS
    )
    assert result.balanced is True
    assert result.batches == 2
    assert result.produced_m3 == 12.0
    assert result.loaded_m3 == 12.0
    assert result.theoretical_m3 == 12.0
    assert result.rows[0]["grade"] == "C30"
    assert result.rows[0]["present_in"] == ["loading", "production", "recipe_usage"]


def test_loaded_vs_produced_mismatch_is_listed() -> None:
    production, _, usage = _balanced_sources()
    loading = (_load("L1", "C30", 11),)
    result = reconcile_shift(
        shift_id="S1", production=production, loading=loading, usage=usage, factors_by_grade=FACTORS
    )
    kinds = {item["kind"] for item in result.differences}
    assert "volume_mismatch" in kinds
    mismatch = next(item for item in result.differences if item["kind"] == "volume_mismatch")
    assert mismatch["delta_m3"] == -1.0
    assert mismatch["left"] == "loaded"
    assert mismatch["right"] == "produced"


def test_tolerance_absorbs_a_small_difference() -> None:
    production, _, usage = _balanced_sources()
    loading = (_load("L1", "C30", 11.6),)
    strict = reconcile_shift(
        shift_id="S1", production=production, loading=loading, usage=usage, factors_by_grade=FACTORS
    )
    assert any(item["kind"] == "volume_mismatch" for item in strict.differences)
    tolerant = reconcile_shift(
        shift_id="S1",
        production=production,
        loading=loading,
        usage=usage,
        factors_by_grade=FACTORS,
        tolerance_m3=0.5,
    )
    assert tolerant.balanced is True


def test_grade_missing_from_one_source_is_listed() -> None:
    production = (_prod("B1", "C30", 6),)
    loading = (_load("L1", "C30", 6), _load("L2", "C20", 4))
    result = reconcile_shift(
        shift_id="S1", production=production, loading=loading, usage=(), factors_by_grade=FACTORS
    )
    missing = [item for item in result.differences if item["kind"] == "grade_missing_source"]
    grades = {item["grade"] for item in missing}
    assert {"C30", "C20"} <= grades


def test_unmapped_usage_material_is_reported() -> None:
    production = (_prod("B1", "C30", 6),)
    loading = (_load("L1", "C30", 6),)
    usage = (_usage("C30", "fly_ash", 100),)
    result = reconcile_shift(
        shift_id="S1", production=production, loading=loading, usage=usage, factors_by_grade=FACTORS
    )
    assert any(item["kind"] == "usage_unmapped" for item in result.differences)


def test_fingerprint_is_stable_and_input_sensitive() -> None:
    production, loading, usage = _balanced_sources()
    first = build_fingerprint(production=production, loading=loading, usage=usage, factors_by_grade=FACTORS)
    again = build_fingerprint(production=production, loading=loading, usage=usage, factors_by_grade=FACTORS)
    assert first == again
    changed_loading = (_load("L1", "C30", 11.99),)
    different = build_fingerprint(
        production=production, loading=changed_loading, usage=usage, factors_by_grade=FACTORS
    )
    assert different != first


def test_fingerprint_does_not_depend_on_record_order() -> None:
    a = (_prod("B1", "C30", 6), _prod("B2", "C30", 6))
    b = tuple(reversed(a))
    assert build_fingerprint(production=a, loading=(), usage=()) == build_fingerprint(
        production=b, loading=(), usage=()
    )
