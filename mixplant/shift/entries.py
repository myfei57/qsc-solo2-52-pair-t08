"""Append only ledger entries that make up a shift.

A shift collects three independent ledgers:

* ``production`` trays — one row per tray (盘) batched, tagged with a concrete
  grade and the cubic metres produced,
* ``loading`` slips — one row per loading ticket (装车单),
* ``recipe`` usage — material consumed against a grade (配方用量).

Corrections are never an update of an earlier row.  A wrong row is cancelled by
a ``reverse``/``void`` entry carrying the negated volume, so every value the
settlement freezes is traceable to its original handwriting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import DuplicateError, NotFoundError, ValidationError
from .kinds import (
    ENTRY_SOURCES,
    KIND_REVERSE,
    KIND_SLIP,
    KIND_TRAY,
    SOURCE_LOADING,
    SOURCE_PRODUCTION,
    SOURCE_RECIPE,
)


def _rounded(value: float) -> float:
    return round(float(value), 3)


@dataclass(frozen=True, slots=True)
class Entry:
    """One immutable row on one of the three shift ledgers."""

    entry_id: str
    shift_id: str
    source: str
    kind: str
    ref: str
    grade: str
    volume_m3: float
    tick: int
    material: str = ""
    consumed_kg: float = 0.0
    factor_kg_per_m3: float = 0.0
    note: str = ""
    reverses: str = ""
    origin: str = "console"

    def __post_init__(self) -> None:
        if not self.entry_id:
            raise ValidationError("an entry needs an identity")
        if not self.shift_id:
            raise ValidationError("an entry needs a shift")
        if self.source not in ENTRY_SOURCES:
            raise ValidationError("unknown ledger", source=self.source)
        if not self.ref:
            raise ValidationError("an entry needs a document reference", entry_id=self.entry_id)
        if not self.grade:
            raise ValidationError("an entry needs a grade", entry_id=self.entry_id)
        if self.kind == KIND_REVERSE and not self.reverses:
            raise ValidationError("a reverse entry must name the entry it cancels", entry_id=self.entry_id)
        if self.source == SOURCE_RECIPE:
            if self.material == "":
                raise ValidationError("a recipe usage entry needs a material", entry_id=self.entry_id)
            if self.consumed_kg <= 0:
                raise ValidationError("consumed material must be positive", entry_id=self.entry_id)
            if self.factor_kg_per_m3 <= 0:
                raise ValidationError("recipe factor must be positive", entry_id=self.entry_id)
        elif self.volume_m3 <= 0:
            raise ValidationError("volume must be positive", entry_id=self.entry_id)

    @property
    def signed_volume_m3(self) -> float:
        """Reverse entries subtract; every other entry adds."""

        if self.kind == KIND_REVERSE:
            return -abs(self.volume_m3)
        return self.volume_m3

    def describe(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "active": True,
            "entry_id": self.entry_id,
            "shift_id": self.shift_id,
            "source": self.source,
            "kind": self.kind,
            "ref": self.ref,
            "grade": self.grade,
            "volume_m3": _rounded(self.volume_m3),
            "tick": self.tick,
            "origin": self.origin,
        }
        if self.material:
            body["material"] = self.material
            body["consumed_kg"] = round(self.consumed_kg, 3)
            body["factor_kg_per_m3"] = round(self.factor_kg_per_m3, 3)
        if self.note:
            body["note"] = self.note
        if self.reverses:
            body["reverses"] = self.reverses
        return body

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Entry":
        return cls(
            entry_id=str(document["entry_id"]),
            shift_id=str(document["shift_id"]),
            source=str(document["source"]),
            kind=str(document["kind"]),
            ref=str(document["ref"]),
            grade=str(document["grade"]),
            volume_m3=float(document.get("volume_m3", 0.0)),
            tick=int(document["tick"]),
            material=str(document.get("material", "")),
            consumed_kg=float(document.get("consumed_kg", 0.0)),
            factor_kg_per_m3=float(document.get("factor_kg_per_m3", 0.0)),
            note=str(document.get("note", "")),
            reverses=str(document.get("reverses", "")),
            origin=str(document.get("origin", "console")),
        )


def tray(
    *,
    entry_id: str,
    shift_id: str,
    ref: str,
    grade: str,
    volume_m3: float,
    tick: int,
    origin: str = "console",
    note: str = "",
) -> Entry:
    """Build one production tray row."""

    return Entry(
        entry_id=entry_id,
        shift_id=shift_id,
        source=SOURCE_PRODUCTION,
        kind=KIND_TRAY,
        ref=ref,
        grade=grade,
        volume_m3=volume_m3,
        tick=tick,
        origin=origin,
        note=note,
    )


def slip(
    *,
    entry_id: str,
    shift_id: str,
    ref: str,
    grade: str,
    volume_m3: float,
    tick: int,
    origin: str = "console",
    note: str = "",
) -> Entry:
    """Build one loading slip row."""

    return Entry(
        entry_id=entry_id,
        shift_id=shift_id,
        source=SOURCE_LOADING,
        kind=KIND_SLIP,
        ref=ref,
        grade=grade,
        volume_m3=volume_m3,
        tick=tick,
        origin=origin,
        note=note,
    )


def usage(
    *,
    entry_id: str,
    shift_id: str,
    ref: str,
    grade: str,
    material: str,
    consumed_kg: float,
    factor_kg_per_m3: float,
    tick: int,
    origin: str = "console",
    note: str = "",
) -> Entry:
    """Build one recipe usage row.

    The cubic metres implied by this row are ``consumed / factor``; the factor
    is the recipe's kg-per-cubic-metre for ``material``.
    """

    implied = consumed_kg / factor_kg_per_m3
    return Entry(
        entry_id=entry_id,
        shift_id=shift_id,
        source=SOURCE_RECIPE,
        kind="usage",
        ref=ref,
        grade=grade,
        volume_m3=implied,
        tick=tick,
        material=material,
        consumed_kg=consumed_kg,
        factor_kg_per_m3=factor_kg_per_m3,
        origin=origin,
        note=note,
    )


def reversal(entry_id: str, original: Entry, *, tick: int, note: str) -> Entry:
    """Build the row that cancels ``original``."""

    return Entry(
        entry_id=entry_id,
        shift_id=original.shift_id,
        source=original.source,
        kind=KIND_REVERSE,
        ref=f"rev-{original.ref}",
        grade=original.grade,
        volume_m3=abs(original.signed_volume_m3),
        tick=tick,
        material=original.material,
        consumed_kg=original.consumed_kg,
        factor_kg_per_m3=original.factor_kg_per_m3,
        note=note,
        reverses=original.entry_id,
        origin=original.origin,
    )


class ShiftLedger:
    """The rows collected by one shift, oldest first."""

    __slots__ = ("shift_id", "_entries", "_index", "_refs")

    def __init__(self, shift_id: str) -> None:
        self.shift_id = shift_id
        self._entries: list[Entry] = []
        self._index: dict[str, Entry] = {}
        self._refs: set[tuple[str, str]] = set()

    def add(self, entry: Entry) -> Entry:
        if entry.shift_id != self.shift_id:
            raise ValidationError("entry belongs to another shift", shift_id=self.shift_id)
        if entry.entry_id in self._index:
            raise DuplicateError("shift entry identity was reused", entry_id=entry.entry_id)
        ref_key = (entry.source, entry.ref)
        if entry.kind != KIND_REVERSE and ref_key in self._refs:
            raise DuplicateError(
                "the same document was entered twice", source=entry.source, ref=entry.ref
            )
        if entry.reverses and entry.reverses not in self._index:
            raise ValidationError(
                "the reversed entry was never recorded", entry_id=entry.entry_id, reverses=entry.reverses
            )
        self._entries.append(entry)
        self._index[entry.entry_id] = entry
        if entry.kind != KIND_REVERSE:
            self._refs.add(ref_key)
        return entry

    def get(self, entry_id: str) -> Entry:
        entry = self._index.get(entry_id)
        if entry is None:
            raise NotFoundError("shift entry was never recorded", entry_id=entry_id)
        return entry

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def of_source(self, source: str) -> tuple[Entry, ...]:
        return tuple(entry for entry in self._entries if entry.source == source)

    def describe(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "entries": [entry.describe() for entry in self._entries],
        }
