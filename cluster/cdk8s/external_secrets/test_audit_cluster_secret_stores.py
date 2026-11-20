"""The stored-object audit uses the same named-list predicate as native admission."""

from __future__ import annotations

from email.message import Message
from typing import Any
from urllib.error import HTTPError

import pytest
import pytest_bazel

from cluster.cdk8s.external_secrets.audit_cluster_secret_stores import audit


def _store(name: str, **spec: object) -> dict[str, Any]:
    return {"metadata": {"name": name}, "spec": {"provider": {"dummy": "do-not-log"}, **spec}}


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"conditions": None},
        {"conditions": []},
        {"conditions": [{}]},
        {"conditions": [{"namespaces": []}]},
        {"conditions": [{"namespaces": None}]},
        {"conditions": [{"namespaceSelector": {}}]},
        {"conditions": [{"namespaceSelector": {"matchLabels": {"allow": "true"}}}]},
        {"conditions": [{"namespaceRegexes": ["^consumer$"]}]},
    ],
)
def test_audit_flags_unfenced_stores(spec: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert audit(lambda _: {"items": [_store("unfenced", **spec)], "metadata": {}}) == 1
    result = capsys.readouterr()
    assert "ClusterSecretStore unfenced has no explicit consumer namespace." in result.err
    assert "do-not-log" not in result.err + result.out


def test_audit_traverses_every_page_and_accepts_any_explicit_list(capsys: pytest.CaptureFixture[str]) -> None:
    page_tokens: list[str] = []

    def fetch(continuation: str) -> dict[str, Any]:
        page_tokens.append(continuation)
        if not continuation:
            return {
                "items": [_store("named", conditions=[{}, {"namespaces": ["consumer"]}])],
                "metadata": {"continue": "second"},
            }
        return {"items": [_store("late-violation")], "metadata": {}}

    assert audit(fetch) == 1
    assert page_tokens == ["", "second"]
    result = capsys.readouterr()
    assert result.err == "ClusterSecretStore late-violation has no explicit consumer namespace.\n"
    assert "Checked 2 ClusterSecretStores. Violations: 1." in result.out


def test_audit_accepts_compliant_and_empty_inventories() -> None:
    assert audit(lambda _: {"items": [_store("named", conditions=[{"namespaces": ["consumer"]}])], "metadata": {}}) == 0
    assert audit(lambda _: {"items": [], "metadata": {}}) == 0


def test_audit_fails_on_api_error_without_logging_error_details(capsys: pytest.CaptureFixture[str]) -> None:
    def fetch(_: str) -> dict[str, Any]:
        raise HTTPError("https://example.invalid", 403, "do-not-log", Message(), None)

    assert audit(fetch) == 2
    result = capsys.readouterr()
    assert "audit could not complete (HTTPError)" in result.err
    assert "do-not-log" not in result.err + result.out


if __name__ == "__main__":
    pytest_bazel.main()
