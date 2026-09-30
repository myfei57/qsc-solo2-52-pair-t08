"""Routes for the shift settlement page and the shift commands."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/shift/crews", "shift.register_crew"),
    ("POST", "/api/shift/open", "shift.open"),
    ("POST", "/api/shift/close", "shift.close"),
    ("POST", "/api/shift/trays", "shift.record_tray"),
    ("POST", "/api/shift/slips", "shift.record_slip"),
    ("POST", "/api/shift/usage", "shift.record_usage"),
    ("POST", "/api/shift/reverse", "shift.reverse_entry"),
    ("POST", "/api/shift/capture", "shift.capture"),
    ("POST", "/api/shift/reconcile", "shift.reconcile"),
    ("POST", "/api/shift/settle", "shift.settle"),
    ("POST", "/api/shift/reopen", "shift.reopen"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the shift routes."""

    router.add(
        "GET",
        "/pages/shift",
        lambda _: Response.html(inject_snapshot(pages.load("shift"), runtime.controller.shift_view())),
    )
    router.add("GET", "/api/shift", lambda _: Response.json(runtime.controller.shift_view()))
    router.add(
        "GET",
        "/api/shift/settlements",
        lambda _: Response.json(runtime.shifts.settlements()),
    )
    router.add(
        "GET",
        "/api/shift/verify",
        lambda _: Response.json(runtime.shifts.verify_chain()),
    )
    router.add("GET", "/api/shift/detail", _detail(runtime))
    router.add("GET", "/api/shift/preview", _preview(runtime))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler


def _detail(runtime: Any):
    def handler(request: Request) -> Response:
        shift_id = request.require_field("shift_id")
        return Response.json(runtime.shifts.get_shift(str(shift_id)))

    return handler


def _preview(runtime: Any):
    def handler(request: Request) -> Response:
        shift_id = request.require_field("shift_id")
        return Response.json(runtime.shifts.preview(str(shift_id)))

    return handler
