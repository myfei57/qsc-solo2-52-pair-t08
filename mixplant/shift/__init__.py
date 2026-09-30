"""Shift settlement subdomain.

A shift closes the books once: it reconciles the handwritten production sheet
(how many batches, how many cubic metres per grade) against the loading slips
and the recipe usage, lists every mismatch on its own line, then settles.
Whether a mismatch suspends the whole shift or is carried as a listed
difference is fixed per crew.  Reconciliation and settlement are idempotent,
and a settled shift is immutable history.
"""

from .constants import (
    CLOSE_KIND,
    CREW_KIND,
    LOADING_KIND,
    OPEN_KIND,
    PRODUCTION_KIND,
    RECONCILE_KIND,
    RESULT_BALANCED,
    RESULT_CARRIED_DIFF,
    RESULT_SUSPENDED,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_SUSPENDED,
    USAGE_KIND,
)
from .models import Crew, LoadingEntry, ProductionEntry, Reconciliation, Settlement, UsageEntry
from .reconcile import build_fingerprint, reconcile_shift
from .registry import ShiftRegistry
from .service import ShiftService

__all__ = [
    "CLOSE_KIND",
    "CREW_KIND",
    "LOADING_KIND",
    "OPEN_KIND",
    "PRODUCTION_KIND",
    "RECONCILE_KIND",
    "USAGE_KIND",
    "RESULT_BALANCED",
    "RESULT_CARRIED_DIFF",
    "RESULT_SUSPENDED",
    "STATUS_CLOSED",
    "STATUS_OPEN",
    "STATUS_SUSPENDED",
    "Crew",
    "LoadingEntry",
    "ProductionEntry",
    "Reconciliation",
    "Settlement",
    "UsageEntry",
    "ShiftRegistry",
    "ShiftService",
    "build_fingerprint",
    "reconcile_shift",
]
