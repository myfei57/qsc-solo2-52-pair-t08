"""Reconciliation arithmetic: fold the three ledgers into one statement.

For every concrete grade (标号) the statement lines up three independently
reported cubic metre totals:

* ``produced_m3``  — net production from the tray rows,
* ``loaded_m3``    — net loading from the slip rows,
* ``recipe_m3``    — the volume the consumed materials imply under the recipe
  factors (the smallest per-material implication, so a missing or light
  ingredient cannot hide an over-production).

It also reports the tray count (打了几盘) per grade.  Any pair that differs by
more than the crew tolerance is listed as an explicit ``difference`` row — the
rows the settlement either suspends on or carries into the history, depending
on the crew's fixed policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .entries import Entry
from .hashing import entries_fingerprint
from .kinds import KIND_REVERSE, SOURCE_LOADING, SOURCE_PRODUCTION, SOURCE_RECIPE

QUANT = 3


def q3(value: float) -> float:
    return round(float(value), QUANT)


@dataclass(frozen=True, slots=True)
class GradeLine:
    """One grade's three-way statement."""

    grade: str
    tray_count: int
    produced_m3: float
    loaded_m3: float
    recipe_m3: float | None
    materials: Mapping[str, dict[str, float]]
    differences: tuple[dict[str, Any], ...]

    @property
    def has_difference(self) -> bool:
        return bool(self.differences)

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "tray_count": self.tray_count,
            "produced_m3": q3(self.produced_m3),
            "loaded_m3": q3(self.loaded_m3),
            "recipe_m3": None if self.recipe_m3 is None else q3(self.recipe_m3),
            "materials": {
                material: {key: q3(value) for key, value in row.items()}
                for material, row in sorted(self.materials.items())
            },
            "differences": list(self.differences),
        }


@dataclass(frozen=True, slots=True)
class Statement:
    """The full reconciliation statement for a shift."""

    shift_id: str
    entries_fingerprint: str
    tolerance_m3: float
    lines: tuple[GradeLine, ...]
    totals: Mapping[str, Any]
    has_difference: bool
    tick: int

    def grades(self) -> tuple[str, ...]:
        return tuple(line.grade for line in self.lines)

    def describe(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "entries_fingerprint": self.entries_fingerprint,
            "tolerance_m3": q3(self.tolerance_m3),
            "tick": self.tick,
            "has_difference": self.has_difference,
            "totals": dict(self.totals),
            "lines": [line.describe() for line in self.lines],
        }


def _diff_row(kind: str, grade: str, left: float, right: float, *, names: tuple[str, str]) -> dict[str, Any]:
    gap = left - right
    return {
        "kind": kind,
        "grade": grade,
        "left": names[0],
        "right": names[1],
        "left_m3": q3(left),
        "right_m3": q3(right),
        "gap_m3": q3(gap),
        "abs_gap_m3": q3(abs(gap)),
    }


def _grade_differences(
    grade: str, *, produced: float, loaded: float, recipe: float | None, tolerance: float
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    if abs(produced - loaded) > tolerance:
        rows.append(
            _diff_row(
                "production_vs_loading",
                grade,
                produced,
                loaded,
                names=("produced_m3", "loaded_m3"),
            )
        )
    if recipe is not None and abs(produced - recipe) > tolerance:
        rows.append(
            _diff_row(
                "production_vs_recipe",
                grade,
                produced,
                recipe,
                names=("produced_m3", "recipe_m3"),
            )
        )
    if recipe is not None and abs(loaded - recipe) > tolerance:
        rows.append(
            _diff_row(
                "loading_vs_recipe",
                grade,
                loaded,
                recipe,
                names=("loaded_m3", "recipe_m3"),
            )
        )
    return tuple(rows)


def build_statement(
    entries: tuple[Entry, ...],
    *,
    shift_id: str,
    tolerance_m3: float,
    tick: int,
) -> Statement:
    """Fold every ledger row into the reconciliation statement."""

    grades: dict[str, dict[str, Any]] = {}

    def bucket(grade: str) -> dict[str, Any]:
        node = grades.get(grade)
        if node is None:
            node = {
                "produced": 0.0,
                "loaded": 0.0,
                "trays": 0,
                "recipe_m3": [],
                "materials": {},
            }
            grades[grade] = node
        return node

    for entry in entries:
        node = bucket(entry.grade)
        if entry.source == SOURCE_PRODUCTION:
            node["produced"] += entry.signed_volume_m3
            if entry.kind != KIND_REVERSE:
                node["trays"] += 1
        elif entry.source == SOURCE_LOADING:
            node["loaded"] += entry.signed_volume_m3
        elif entry.source == SOURCE_RECIPE:
            material_row = node["materials"].setdefault(
                entry.material, {"consumed_kg": 0.0, "factor_kg_per_m3": entry.factor_kg_per_m3}
            )
            # A reverse cancels the consumed mass as well.
            signed = entry.consumed_kg if entry.kind != KIND_REVERSE else -entry.consumed_kg
            material_row["consumed_kg"] += signed
            material_row["factor_kg_per_m3"] = entry.factor_kg_per_m3

    lines: list[GradeLine] = []
    total_produced = total_loaded = 0.0
    total_recipe_values: list[float] = []
    total_trays = 0
    any_diff = False

    for grade in sorted(grades):
        node = grades[grade]
        produced = q3(node["produced"])
        loaded = q3(node["loaded"])
        materials: dict[str, dict[str, float]] = {}
        implications: list[float] = []
        for material, row in sorted(node["materials"].items()):
            consumed = q3(row["consumed_kg"])
            factor = q3(row["factor_kg_per_m3"])
            if factor > 0 and consumed > 0:
                implied = q3(consumed / factor)
                implications.append(implied)
                materials[material] = {
                    "consumed_kg": consumed,
                    "factor_kg_per_m3": factor,
                    "implied_m3": implied,
                }
        recipe_m3 = q3(min(implications)) if implications else None
        differences = _grade_differences(
            grade, produced=produced, loaded=loaded, recipe=recipe_m3, tolerance=tolerance_m3
        )
        any_diff = any_diff or bool(differences)
        line = GradeLine(
            grade=grade,
            tray_count=int(node["trays"]),
            produced_m3=produced,
            loaded_m3=loaded,
            recipe_m3=recipe_m3,
            materials=materials,
            differences=differences,
        )
        lines.append(line)
        total_produced += produced
        total_loaded += loaded
        total_trays += int(node["trays"])
        if recipe_m3 is not None:
            total_recipe_values.append(recipe_m3)

    totals = {
        "grade_count": len(lines),
        "tray_count": total_trays,
        "produced_m3": q3(total_produced),
        "loaded_m3": q3(total_loaded),
        "recipe_m3": None if not total_recipe_values else q3(sum(total_recipe_values)),
    }
    return Statement(
        shift_id=shift_id,
        entries_fingerprint=entries_fingerprint(entries),
        tolerance_m3=tolerance_m3,
        lines=tuple(lines),
        totals=totals,
        has_difference=any_diff,
        tick=tick,
    )
