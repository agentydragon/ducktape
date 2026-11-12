"""CRD initialization failures must not abort or extend the readiness wait."""

import pytest
import pytest_bazel
import yaml
from tenacity import wait_none

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


def test_warm_prepare_retries_admission_until_both_webhooks_are_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pki_infrastructure, "READY_SECONDS", 10)
    monkeypatch.setattr(pki_infrastructure, "wait_fixed", lambda _: wait_none())
    deployments = [
        {"kind": "Deployment", "metadata": {"name": "fake-cert-manager"}},
        {"kind": "Deployment", "metadata": {"name": "fake-trust-manager"}},
    ]
    rollout_waits: list[str] = []
    probes: list[dict[str, object]] = []
    bundle_attempts = 0

    def kubectl(*args: str, **kwargs: object) -> str:
        nonlocal bundle_attempts
        if args[0] == "-n":
            rollout_waits.append(args[4].removeprefix("deployment/"))
        elif args[0] == "create":
            assert args[1:4] == ("--dry-run=server", "-f", "-")
            input_text = kwargs["input_text"]
            assert isinstance(input_text, str)
            resource: dict[str, object] = yaml.safe_load(input_text)
            probes.append(resource)
            if resource["kind"] == "Bundle":
                bundle_attempts += 1
                if bundle_attempts == 1:
                    raise RuntimeError("no endpoints available")
        else:
            pytest.fail(f"unexpected kubectl command: {args}")
        return ""

    pki_infrastructure.prepare(kubectl, deployments[:1], deployments[1:], warm=True)

    assert rollout_waits == ["fake-cert-manager", "fake-trust-manager"]
    assert [(probe["kind"], probe["apiVersion"]) for probe in probes] == [
        ("ClusterIssuer", "cert-manager.io/v1"),
        ("Bundle", "trust.cert-manager.io/v1alpha1"),
        ("Bundle", "trust.cert-manager.io/v1alpha1"),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
