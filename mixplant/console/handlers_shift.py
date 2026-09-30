"""Routes for the shift settlement page and the shift commands."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/shift/crew", "shift.crew"),
    ("POST", "/api/shift/open", "shift.open"),
    ("POST", "/api/shift/production", "shift.production"),
    ("POST", "/api/shift/loading", "shift.loading"),
    ("POST", "/api/shift/usage", "shift.usage"),
    ("POST", "/api/shift/capture", "shift.capture"),
    ("POST", "/api/shift/reconcile", "shift.reconcile"),
    ("POST", "/api/shift/settle", "shift.settle"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the shift settlement routes."""

    router.add(
        "GET",
        "/pages/shift",
        lambda _: Response.html(inject_snapshot(pages.load("shift"), runtime.controller.shift_view())),
    )
    router.add("GET", "/api/shift", lambda _: Response.json(runtime.controller.shift_view()))
    router.add("GET", "/api/shift/list", lambda _: Response.json(runtime.shifts.list_shifts()))
    router.add(
        "GET",
        "/api/shift/detail",
        lambda request: Response.json(runtime.shifts.get_shift(request.require_field("shift_id"))),
    )
    router.add("GET", "/api/shift/history", lambda _: Response.json(runtime.shifts.history()))
    router.add("GET", "/api/shift/verify", lambda _: Response.json(runtime.shifts.verify()))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
