"""Crews and their pinned reconciliation policy.

A crew's mismatch policy is fixed the day the crew is registered: when the
three ledgers disagree the settlement either suspends the whole shift or lists
the differences and continues.  It cannot be changed afterwards, and it cannot
be overridden per shift, so nobody can loosen a shift while it is being closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import DuplicateError, NotFoundError, ValidationError
from .kinds import POLICIES


@dataclass(frozen=True, slots=True)
class Crew:
    """A production crew and its fixed mismatch policy."""

    crew_id: str
    name: str
    mismatch_policy: str
    tolerance_m3: float

    def __post_init__(self) -> None:
        if not self.crew_id:
            raise ValidationError("a crew needs an identity")
        if not self.name:
            raise ValidationError("a crew needs a name", crew_id=self.crew_id)
        if self.mismatch_policy not in POLICIES:
            raise ValidationError(
                "mismatch policy must be 'suspend' or 'list_diffs'",
                crew_id=self.crew_id,
                policy=self.mismatch_policy,
            )
        if self.tolerance_m3 < 0:
            raise ValidationError(
                "tolerance must not be negative", crew_id=self.crew_id, tolerance_m3=self.tolerance_m3
            )

    def describe(self) -> dict[str, Any]:
        return {
            "crew_id": self.crew_id,
            "name": self.name,
            "mismatch_policy": self.mismatch_policy,
            "tolerance_m3": round(self.tolerance_m3, 3),
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Crew":
        return cls(
            crew_id=str(document["crew_id"]),
            name=str(document["name"]),
            mismatch_policy=str(document["mismatch_policy"]),
            tolerance_m3=float(document.get("tolerance_m3", 0.5)),
        )


class CrewDirectory:
    """The append only register of crews and policies."""

    __slots__ = ("_crews",)

    def __init__(self) -> None:
        self._crews: dict[str, Crew] = {}

    def register(self, crew: Crew) -> Crew:
        """Register a crew and refuse to overwrite one."""

        if crew.crew_id in self._crews:
            existing = self._crews[crew.crew_id]
            raise DuplicateError(
                "crew was already registered with a fixed policy",
                crew_id=crew.crew_id,
                mismatch_policy=existing.mismatch_policy,
            )
        self._crews[crew.crew_id] = crew
        return crew

    def adopt(self, crew: Crew) -> Crew:
        """Adopt a crew read back from the stream after a restart.

        A replay that carries the same crew twice only happens when an append
        was rejected, so an identical repeat is a no-op; a changed policy on an
        existing crew is a journal integrity error and must never be allowed.
        """

        existing = self._crews.get(crew.crew_id)
        if existing is None:
            self._crews[crew.crew_id] = crew
            return crew
        if existing.mismatch_policy != crew.mismatch_policy:
            raise ValidationError(
                "crew policy was rewritten in the journal",
                crew_id=crew.crew_id,
                stored=existing.mismatch_policy,
                replayed=crew.mismatch_policy,
            )
        return existing

    def require(self, crew_id: str) -> Crew:
        crew = self._crews.get(crew_id)
        if crew is None:
            raise NotFoundError("crew was never registered", crew_id=crew_id)
        return crew

    def get(self, crew_id: str) -> Crew | None:
        return self._crews.get(crew_id)

    def all(self) -> tuple[Crew, ...]:
        return tuple(self._crews[key] for key in sorted(self._crews))

    def describe(self) -> list[dict[str, Any]]:
        return [crew.describe() for crew in self.all()]
