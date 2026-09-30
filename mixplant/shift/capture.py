"""Bridge that reads the live control stream into shift capture facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BatchFact:
    batch_id: str
    grade: str
    volume_m3: float


@dataclass(frozen=True, slots=True)
class LoadFact:
    slip_id: str
    truck_id: str
    batch_id: str
    grade: str
    volume_m3: float


@dataclass(frozen=True, slots=True)
class UsageFact:
    grade: str
    material: str
    consumed_kg: float


@dataclass(frozen=True, slots=True)
class PlantFacts:
    batches: tuple[BatchFact, ...]
    loads: tuple[LoadFact, ...]
    usage: tuple[UsageFact, ...]

    @classmethod
    def empty(cls) -> "PlantFacts":
        return cls(batches=(), loads=(), usage=())


def collect_plant_facts(runtime: Any) -> PlantFacts:
    """Collect batches, confirmed loads and persisted weights from a runtime."""

    batches: list[BatchFact] = []
    grade_of: dict[str, str] = {}
    for batch in runtime.batches.all():
        batches.append(BatchFact(batch_id=batch.batch_id, grade=batch.grade, volume_m3=batch.quantity))
        grade_of[batch.batch_id] = batch.grade

    loads: list[LoadFact] = []
    for load in runtime.chute.manifest.describe().get("loads", []):
        batch_id = str(load.get("batch_id", ""))
        grade = grade_of.get(batch_id, str(load.get("grade", "")))
        truck_id = str(load.get("truck_id", ""))
        loads.append(
            LoadFact(
                slip_id=f"{truck_id}:{batch_id}",
                truck_id=truck_id,
                batch_id=batch_id,
                grade=grade,
                volume_m3=float(load.get("volume_m3", 0.0)),
            )
        )

    usage_totals: dict[tuple[str, str], float] = {}
    reports = runtime.agg.reports().get("reports", [])
    for report in reports:
        grade = grade_of.get(str(report.get("batch_id", "")))
        if not grade:
            continue
        key = (grade, str(report["material"]))
        usage_totals[key] = usage_totals.get(key, 0.0) + float(report["actual_kg"])
    usage = [
        UsageFact(grade=grade, material=material, consumed_kg=mass)
        for (grade, material), mass in sorted(usage_totals.items())
    ]
    return PlantFacts(batches=tuple(batches), loads=tuple(loads), usage=tuple(usage))
