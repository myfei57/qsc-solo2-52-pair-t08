#!/usr/bin/env python3
"""Minimal pytest-compatible runner used when pytest is not installed.

It supports the subset the test suite relies on: plain ``test_`` functions,
``tmp_path`` and the shared ``runtime``/``cycled`` fixtures declared in
``tests/conftest.py``.  When pytest is available the suite should be run with
``python -m pytest`` instead.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback
from types import ModuleType

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Provide a tiny pytest shim so the repository's conftest imports without pytest.
if "pytest" not in sys.modules:
    import contextlib
    import math
    import types

    _pytest = types.ModuleType("pytest")
    _pytest.fixture = lambda *a, **k: (lambda fn: fn)  # decorator passthrough

    class _Approx:
        def __init__(self, expected, rel=1e-6, abs=1e-12):
            self.expected = expected
            self.rel = rel
            self.abs = abs

        def __eq__(self, other):
            return other == self.expected or math.isclose(
                other, self.expected, rel_tol=self.rel, abs_tol=self.abs
            )

        def __repr__(self):
            return f"approx({self.expected!r})"

    def _approx(expected, rel=1e-6, abs=1e-12):
        return _Approx(expected, rel=rel, abs=abs)

    @contextlib.contextmanager
    def _raises(expected):
        try:
            yield
        except expected:
            return
        raise AssertionError(f"{expected.__name__} was not raised")

    _pytest.approx = _approx
    _pytest.raises = _raises
    sys.modules["pytest"] = _pytest


def _load_module(name: str, path: pathlib.Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_conftest() -> tuple[ModuleType, dict[str, object]]:
    conftest = _load_module("conftest", ROOT / "conftest.py")
    tests_conftest_path = ROOT / "tests" / "conftest.py"
    fixtures: dict[str, object] = {}
    if tests_conftest_path.exists():
        tests_conftest = _load_module("tests_conftest", tests_conftest_path)
        fixtures["runtime"] = tests_conftest.runtime
        fixtures["cycled"] = tests_conftest.cycled
    return conftest, fixtures


def _tmp_path(request):
    ctx = tempfile.TemporaryDirectory()
    request.addfinalizer(ctx.cleanup)
    return pathlib.Path(ctx.name)


def _fixture_value(fixture, request, fixtures):
    if fixture.__name__ == "tmp_path":
        return _tmp_path(request)
    kwargs = {}
    for pname in inspect.signature(fixture).parameters:
        if pname in fixtures:
            kwargs[pname] = _fixture_value(fixtures[pname], request, fixtures)
        elif pname == "tmp_path":
            kwargs[pname] = _tmp_path(request)
        else:
            raise RuntimeError(f"unknown fixture {pname}")
    return fixture(**kwargs)


class Request:
    def __init__(self) -> None:
        self._finalizers: list = []

    def addfinalizer(self, fn) -> None:
        self._finalizers.append(fn)

    def finalize(self) -> None:
        for fn in reversed(self._finalizers):
            fn()


def main() -> int:
    _, fixtures = _load_conftest()
    failures = 0
    passed = 0
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    for path in test_files:
        module = _load_module(f"tests.{path.stem}", path)
        for name, fn in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            params = inspect.signature(fn).parameters
            request = Request()
            kwargs = {}
            try:
                for pname, param in params.items():
                    if pname in fixtures:
                        kwargs[pname] = _fixture_value(fixtures[pname], request, fixtures)
                    elif pname == "tmp_path":
                        kwargs[pname] = _tmp_path(request)
                    elif param.default is not inspect.Parameter.empty:
                        kwargs[pname] = param.default
                    else:
                        raise RuntimeError(f"unknown fixture {pname}")
                fn(**kwargs)
                passed += 1
                print(f"PASS {path.name}::{name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {path.name}::{name}")
                traceback.print_exc()
            finally:
                request.finalize()
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
