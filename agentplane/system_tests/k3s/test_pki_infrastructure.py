"""CRD initialization failures must not abort or extend the readiness wait."""

import pytest
import pytest_bazel

from agentplane.system_tests.k3s import pki_infrastructure

_CRDS = [{"kind": "CustomResourceDefinition", "metadata": {"name": "widgets.example.com"}}]
_NULL_CONDITIONS = ".status.conditions accessor error: <nil> is of the type <nil>, expected []interface{}"


def test_crd_wait_retries_initial_null_conditions_with_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([100.0, 100.0, 108.0])
    monkeypatch.setattr(pki_infrastructure, "monotonic", lambda: next(clock))
    timeouts: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> str:
        if args[0] == "wait":
            timeouts.append(float(args[-1].removeprefix("--timeout=").removesuffix("s")))
            if len(timeouts) == 1:
                raise RuntimeError(_NULL_CONDITIONS)
        return ""

    pki_infrastructure._install(kubectl, _CRDS, {})

    assert timeouts == [180.0, 172.0]


def test_crd_wait_preserves_unrelated_errors() -> None:
    waits = 0

    def kubectl(*args: str, **kwargs: object) -> str:
        nonlocal waits
        if args[0] == "wait":
            waits += 1
            raise RuntimeError("Forbidden: cannot watch customresourcedefinitions")
        return ""

    with pytest.raises(RuntimeError, match="Forbidden"):
        pki_infrastructure._install(kubectl, _CRDS, {})
    assert waits == 1


def test_crd_wait_stops_retrying_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pki_infrastructure, "READY_SECONDS", 0)
    waits = 0

    def kubectl(*args: str, **kwargs: object) -> str:
        nonlocal waits
        if args[0] == "wait":
            waits += 1
            raise RuntimeError(_NULL_CONDITIONS)
        return ""

    with pytest.raises(RuntimeError, match="accessor error"):
        pki_infrastructure._install(kubectl, _CRDS, {})
    assert waits == 1


if __name__ == "__main__":
    pytest_bazel.main()
