"""Shift settlement package.

The package closes a production *shift* (班组/班次) by reconciling three
independent numbers for every concrete grade:

* how many trays were batched and how many cubic metres the production sheet
  says were produced per grade,
* how many cubic metres the loading slips say left the plant,
* how many cubic metres the recipe usage implies given the recipe factors.

A reconciled shift is settled once and then frozen: the input ledgers, the
reconciliation reports and the settlement snapshots are append only records in
the shared journal.  Every settlement carries a content hash chained to the
previous settlement, so a closed shift is independently verifiable history.
"""

from .kinds import (
    CREW_KIND,
    ENTRY_KIND,
    OPEN_KIND,
    RECONCILE_KIND,
    REOPEN_KIND,
    SETTLE_KIND,
)

__all__ = [
    "CREW_KIND",
    "ENTRY_KIND",
    "OPEN_KIND",
    "RECONCILE_KIND",
    "REOPEN_KIND",
    "SETTLE_KIND",
]
