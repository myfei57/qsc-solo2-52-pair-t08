"""Pure three way reconciliation: production sheet vs loading slips vs recipes.

Nothing here touches the store or the clock, so the same inputs always produce
the same result and the same fingerprint.  That fingerprint is what makes
"reconcile again" idempotent: a repeated trigger over unchanged data is a replay
and writes nothing new.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .models import LoadingEntry, ProductionEntry, Reconciliation, UsageEntry

ROUND = 4

# The pairs a shift summary has to tie out.  The production sheet is the
# handwritten truth; loading slips and recipe usage both have to match it.
PAIRS = (
    ("loaded", "装车方", "produced", "装车单"),
    ("theoretical", "配方理论方", "produced", "配方用量"),
)


def _round(value: float) -> float:
    return round(value + 0.0, ROUND)


def build_fingerprint(
    *,
    production: Sequence[ProductionEntry],
    loading: Sequence[LoadingEntry],
    usage: Sequence[UsageEntry],
    factors_by_grade: Mapping[str, Mapping[str, float]] | None = None,
) -> str:
    """Hash the reconciliation inputs in a canonical order."""

    payload = {
        "production": sorted((item.describe() for item in production), key=lambda row: row["batch_id"]),
        "loading": sorted((item.describe() for item in loading), key=lambda row: row["slip_id"]),
        "usage": sorted(
            (item.describe() for item in usage), key=lambda row: (row["grade"], row["material"])
        ),
        "factors": {
            grade: {material: float(value) for material, value in sorted(factors.items())}
            for grade, factors in sorted((factors_by_grade or {}).items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _theoretical_by_grade(
    usage: Sequence[UsageEntry],
    factors_by_grade: Mapping[str, Mapping[str, float]],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Turn consumed kg back into cubic metres using the recipe factors.

    Each consumed material implies a volume (consumed / kg per m3).  The grade's
    theoretical volume is the mean of those implied volumes, and every material
    whose implied volume disagrees beyond half a cubic metre is reported so a
    single mis recorded line cannot hide in the average.
    """

    grouped: dict[str, dict[str, float]] = {}
    for item in usage:
        grouped.setdefault(item.grade, {})[item.material] = (
            grouped.setdefault(item.grade, {}).get(item.material, 0.0) + item.consumed_kg
        )

    theoretical: dict[str, float] = {}
    material_lines: list[dict[str, Any]] = []
    for grade, consumed in grouped.items():
        factors = factors_by_grade.get(grade) or {}
        implied: dict[str, float] = {}
        for material, mass in sorted(consumed.items()):
            factor = factors.get(material)
            if not factor or factor <= 0:
                material_lines.append(
                    {
                        "kind": "usage_unmapped",
                        "grade": grade,
                        "material": material,
                        "consumed_kg": _round(mass),
                        "factor_kg_per_m3": None,
                        "message": "no recipe factor for this material",
                    }
                )
                continue
            implied[material] = _round(mass / factor)
        if implied:
            values = list(implied.values())
            theoretical[grade] = _round(sum(values) / len(values))
            spread = max(values) - min(values)
            if len(values) > 1 and spread > 0.5:
                material_lines.append(
                    {
                        "kind": "usage_material_spread",
                        "grade": grade,
                        "implied_m3": {material: value for material, value in implied.items()},
                        "spread_m3": _round(spread),
                        "message": "consumed materials imply different volumes",
                    }
                )
    return theoretical, material_lines


def reconcile_shift(
    *,
    shift_id: str,
    production: Sequence[ProductionEntry],
    loading: Sequence[LoadingEntry],
    usage: Sequence[UsageEntry],
    factors_by_grade: Mapping[str, Mapping[str, float]] | None = None,
    tolerance_m3: float = 0.0,
    tick: int = 0,
) -> Reconciliation:
    """Fold the three sources into per grade rows and listed differences."""

    factors_by_grade = factors_by_grade or {}

    produced_by_grade: dict[str, float] = {}
    batch_count_by_grade: dict[str, int] = {}
    for item in production:
        produced_by_grade[item.grade] = produced_by_grade.get(item.grade, 0.0) + item.volume_m3
        batch_count_by_grade[item.grade] = batch_count_by_grade.get(item.grade, 0) + 1

    loaded_by_grade: dict[str, float] = {}
    for item in loading:
        loaded_by_grade[item.grade] = loaded_by_grade.get(item.grade, 0.0) + item.volume_m3

    theoretical_by_grade, material_lines = _theoretical_by_grade(usage, factors_by_grade)

    grades = sorted(set(produced_by_grade) | set(loaded_by_grade) | set(theoretical_by_grade))
    rows: list[Mapping[str, Any]] = []
    differences: list[Mapping[str, Any]] = list(material_lines)

    for grade in grades:
        produced = _round(produced_by_grade.get(grade, 0.0))
        loaded = _round(loaded_by_grade.get(grade, 0.0))
        theoretical = _round(theoretical_by_grade.get(grade, 0.0))
        batches = batch_count_by_grade.get(grade, 0)
        row = {
            "grade": grade,
            "batches": batches,
            "produced_m3": produced,
            "loaded_m3": loaded,
            "theoretical_m3": theoretical,
            "loaded_minus_produced_m3": _round(loaded - produced),
            "theoretical_minus_produced_m3": _round(theoretical - produced),
            "present_in": sorted(
                source
                for source, bucket in (
                    ("production", produced_by_grade),
                    ("loading", loaded_by_grade),
                    ("recipe_usage", theoretical_by_grade),
                )
                if grade in bucket
            ),
        }
        rows.append(row)

        if not {"production", "loading", "recipe_usage"}.issubset(row["present_in"]):
            missing = sorted({"production", "loading", "recipe_usage"} - set(row["present_in"]))
            differences.append(
                {
                    "kind": "grade_missing_source",
                    "grade": grade,
                    "missing": missing,
                    "produced_m3": produced,
                    "loaded_m3": loaded,
                    "theoretical_m3": theoretical,
                    "message": "grade is not present in every source",
                }
            )

        for left_key, _left_label, right_key, _right_label in PAIRS:
            delta = _round(row[f"{left_key}_m3"] - row[f"{right_key}_m3"])
            if abs(delta) > tolerance_m3 + 1e-9:
                differences.append(
                    {
                        "kind": "volume_mismatch",
                        "grade": grade,
                        "left": left_key,
                        "right": right_key,
                        "left_m3": row[f"{left_key}_m3"],
                        "right_m3": row[f"{right_key}_m3"],
                        "delta_m3": delta,
                        "tolerance_m3": _round(tolerance_m3),
                        "message": f"{_left_label}对不上{_right_label}",
                    }
                )

    fingerprint = build_fingerprint(
        production=production, loading=loading, usage=usage, factors_by_grade=factors_by_grade
    )
    return Reconciliation(
        shift_id=shift_id,
        tick=tick,
        batches=len(production),
        produced_m3=_round(sum(produced_by_grade.values())),
        loaded_m3=_round(sum(loaded_by_grade.values())),
        theoretical_m3=_round(sum(theoretical_by_grade.values())),
        rows=tuple(rows),
        differences=tuple(differences),
        fingerprint=fingerprint,
    )
