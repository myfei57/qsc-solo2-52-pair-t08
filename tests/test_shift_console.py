"""The shift console routes serve the ledger and run the commands."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from mixplant.console.pages import PageStore
from mixplant.console.router import Request
from mixplant.console.server import ConsoleServer
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select


def _request(method: str, path: str, body: dict | None = None) -> Request:
    parsed = urlsplit(path)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    return Request(method=method, path=parsed.path, query=query, body=body or {})


def _post(server: ConsoleServer, path: str, body: dict) -> dict:
    response = server.handle(_request("POST", path, body))
    assert response.ok, response.text
    return json.loads(response.text)


def _get(server: ConsoleServer, path: str) -> dict:
    response = server.handle(_request("GET", path))
    assert response.ok, response.text
    return json.loads(response.text)


def _close_balanced_shift(server: ConsoleServer, runtime: PlantRuntime, shift_id: str = "S1") -> None:
    publish_and_select(runtime)
    _post(server, "/api/shift/crew", {"crew_id": "A", "mismatch_policy": "continue"})
    _post(server, "/api/shift/open", {"shift_id": shift_id, "crew_id": "A"})
    _post(server, "/api/shift/production", {"shift_id": shift_id, "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    _post(server, "/api/shift/loading", {"shift_id": shift_id, "slip_id": "L1", "grade": "C30", "volume_m3": 6})
    _post(server, "/api/shift/usage", {"shift_id": shift_id, "grade": "C30", "material": "cement", "consumed_kg": 1980})
    settled = _post(server, "/api/shift/settle", {"shift_id": shift_id})
    assert settled["result"]["result"] == "BALANCED"


def test_shift_page_and_overview_render(runtime: PlantRuntime) -> None:
    server = ConsoleServer(runtime, pages=PageStore(runtime.config.web_dir))
    page = server.handle(_request("GET", "/pages/shift"))
    assert page.status == 200
    assert "班次结算" in page.text
    overview = _get(server, "/api/shift")
    assert overview["summary"]["settlements"] == 0


def test_full_settlement_round_trip_over_http(runtime: PlantRuntime) -> None:
    server = ConsoleServer(runtime, pages=PageStore(runtime.config.web_dir))
    _close_balanced_shift(server, runtime)
    detail = _get(server, "/api/shift/detail?shift_id=S1")
    assert detail["shift"]["status"] == "CLOSED"
    assert detail["settlement"]["result"] == "BALANCED"

    first_seq = runtime.store.last_seq()
    replay = _post(server, "/api/shift/settle", {"shift_id": "S1"})
    assert replay["result"]["idempotent"] is True
    assert runtime.store.last_seq() == first_seq

    history = _get(server, "/api/shift/history")
    assert len(history["settlements"]) == 1
    verify = _get(server, "/api/shift/verify")
    assert verify["ok"] is True


def test_suspend_policy_blocks_close_over_http(runtime: PlantRuntime) -> None:
    server = ConsoleServer(runtime, pages=PageStore(runtime.config.web_dir))
    publish_and_select(runtime)
    _post(server, "/api/shift/crew", {"crew_id": "B", "mismatch_policy": "suspend"})
    _post(server, "/api/shift/open", {"shift_id": "S9", "crew_id": "B"})
    _post(server, "/api/shift/production", {"shift_id": "S9", "batch_id": "B1", "grade": "C30", "volume_m3": 6})
    _post(server, "/api/shift/loading", {"shift_id": "S9", "slip_id": "L1", "grade": "C30", "volume_m3": 4})
    outcome = _post(server, "/api/shift/settle", {"shift_id": "S9"})
    assert outcome["result"]["status"] == "SUSPENDED"
    assert outcome["result"]["result"] == "SUSPENDED"
    assert _get(server, "/api/shift/detail?shift_id=S9")["shift"]["status"] == "SUSPENDED"


def test_mutating_a_closed_shift_returns_conflict(runtime: PlantRuntime) -> None:
    server = ConsoleServer(runtime, pages=PageStore(runtime.config.web_dir))
    _close_balanced_shift(server, runtime)
    response = server.handle(
        _request(
            "POST",
            "/api/shift/production",
            {"shift_id": "S1", "batch_id": "BX", "grade": "C30", "volume_m3": 1},
        )
    )
    assert response.status == 409
    assert json.loads(response.text)["code"] == "order_violation"
