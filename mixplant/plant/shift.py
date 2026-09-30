"""The page model rendered by the shift settlement console page."""

from __future__ import annotations

from typing import Any


def shift_view(shift_service: Any) -> dict[str, Any]:
    """Compose everything the shift page needs in one document."""

    listing = shift_service.list_shifts(include_settled=True)
    settlements = shift_service.settlements()
    return {
        "status": shift_service.status(),
        "crews": listing["crews"],
        "shifts": listing["shifts"],
        "settlements": settlements["settlements"],
        "verification": shift_service.verify_chain(),
    }
