"""Record kinds, statuses and the per crew mismatch policies."""

from __future__ import annotations

# Append only record kinds.  Each kind owns one leaf of the replayed state, so
# the kinds must stay distinct even though they all live in the ``shift`` group.
CREW_KIND = "shift.crew"
OPEN_KIND = "shift.open"
PRODUCTION_KIND = "shift.production"
LOADING_KIND = "shift.loading"
USAGE_KIND = "shift.usage"
RECONCILE_KIND = "shift.reconcile"
CLOSE_KIND = "shift.close"

# Shift lifecycle.
STATUS_OPEN = "OPEN"
STATUS_SUSPENDED = "SUSPENDED"
STATUS_CLOSED = "CLOSED"

# What a settlement decided.
RESULT_BALANCED = "BALANCED"
RESULT_CARRIED_DIFF = "CLOSED_WITH_DIFF"
RESULT_SUSPENDED = "SUSPENDED"

# Per crew policy, fixed when the crew is registered and never changed after.
POLICY_SUSPEND = "suspend"
POLICY_CONTINUE = "continue"
POLICIES = frozenset({POLICY_SUSPEND, POLICY_CONTINUE})

# The three handwritten sources reconciled against one another.
SOURCE_PRODUCTION = "production"
SOURCE_LOADING = "loading"
SOURCE_USAGE = "recipe_usage"

IMMUTABLE_STATUSES = frozenset({STATUS_CLOSED})
