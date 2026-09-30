"""Content hashing for immutable shift history.

Two different hash chains are maintained:

* the **entries fingerprint** of a shift pins the exact ledger rows the
  reconciliation was computed from.  Re-reconciling unchanged ledgers produces
  the same fingerprint, which makes reconciliation idempotent;

* the **settlement chain** links every closed shift to the one before it
  (``prev_hash`` + ``sha256``).  Each settled shift is a frozen snapshot, and
  the chain lets a reviewer prove later that no closed shift was rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .entries import Entry
from .kinds import ENTRY_SOURCES


def canonical_json(value: Any) -> str:
    """Serialise a value deterministically for hashing."""

    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    """Return the sha256 hex digest of the canonical form of ``value``."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def entries_fingerprint(entries: tuple[Entry, ...]) -> str:
    """Fingerprint the rows that feed a reconciliation.

    Only the accounting content matters, not write order noise: rows are
    ordered by their stable entry id and reduced to the fields that move the
    totals.
    """

    rows = [
        {
            "entry_id": entry.entry_id,
            "source": entry.source,
            "kind": entry.kind,
            "ref": entry.ref,
            "grade": entry.grade,
            "volume_m3": round(entry.volume_m3, 3),
            "material": entry.material,
            "consumed_kg": round(entry.consumed_kg, 3),
            "factor_kg_per_m3": round(entry.factor_kg_per_m3, 3),
            "reverses": entry.reverses,
        }
        for entry in sorted(entries, key=lambda item: item.entry_id)
        if entry.source in ENTRY_SOURCES
    ]
    return digest(rows)


def settlement_hash(settlement: Mapping[str, Any], prev_hash: str) -> str:
    """Hash one settlement document chained onto ``prev_hash``.

    The document's own ``hash``/``prev_hash`` fields are excluded so the hash
    never depends on itself, and ``seq`` is excluded because the store assigns
    it only when the document is appended.
    """

    body = {
        key: value
        for key, value in settlement.items()
        if key not in ("hash", "prev_hash", "seq")
    }
    return digest({"prev_hash": prev_hash, "settlement": body})
