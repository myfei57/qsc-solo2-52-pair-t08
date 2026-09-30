"""Record kinds written by the shift service.

Every kind keeps the ``<group>.<leaf>`` shape the append only stream requires.
All of them live under the ``shift`` group so a settled shift's evidence stays
together in the replay tree.
"""

from __future__ import annotations

CREW_KIND = "shift.crew"
OPEN_KIND = "shift.open"
CLOSE_KIND = "shift.close"
ENTRY_KIND = "shift.entry"
RECONCILE_KIND = "shift.reconcile"
REOPEN_KIND = "shift.reopen"
SETTLE_KIND = "shift.settle"

# Lifecycle statuses a shift moves through.
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_RECONCILED = "reconciled"
STATUS_SUSPENDED = "suspended"
STATUS_SETTLED = "settled"

# The two policies a crew is pinned to at registration.
POLICY_SUSPEND = "suspend"
POLICY_LIST_DIFFS = "list_diffs"
POLICIES = (POLICY_SUSPEND, POLICY_LIST_DIFFS)

# The three independent ledgers a shift reconciles.
SOURCE_PRODUCTION = "production"
SOURCE_LOADING = "loading"
SOURCE_RECIPE = "recipe"
ENTRY_SOURCES = (SOURCE_PRODUCTION, SOURCE_LOADING, SOURCE_RECIPE)

# Entry kinds on the production / loading ledgers.
KIND_TRAY = "tray"
KIND_REVERSE = "reverse"
KIND_SLIP = "slip"
