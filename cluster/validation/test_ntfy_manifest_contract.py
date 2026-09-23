"""Focused contracts over the self-hosted ntfy authentication wiring."""

from __future__ import annotations

from typing import cast

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_FLUX_WEBHOOK_MANIFESTS = "_main/cluster/k8s/flux-webhook/flux-webhook.k8s.yaml"
_NTFY_MANIFESTS = "_main/cluster/k8s/ntfy/ntfy.k8s.yaml"


def _external_secret_template_data(path: str, name: str) -> dict[str, str]:
    manifests = yaml.safe_load_all(get_required_path(path).read_text())
    manifest = next(
        obj
        for obj in manifests
        if obj and obj.get("kind") == "ExternalSecret" and obj.get("metadata", {}).get("name") == name
    )
    return cast(dict[str, str], manifest["spec"]["target"]["template"]["data"])


def test_ntfy_provider_headers_parse_as_yaml_map() -> None:
    """The notification controller parses Provider Secret headers as a YAML map."""
    headers = yaml.safe_load(_external_secret_template_data(_FLUX_WEBHOOK_MANIFESTS, "ntfy-webhook")["headers"])

    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer {{ .alertmanager_token }}"
    assert headers["Template"] == "yes"


def test_alertmanager_secret_contains_bearer_token() -> None:
    data = _external_secret_template_data(_NTFY_MANIFESTS, "alertmanager-ntfy-webhook")

    assert data["address"] == "https://ntfy.allegedly.works/alerts"
    assert data["token"] == "{{ .alertmanager_token }}"


if __name__ == "__main__":
    pytest_bazel.main()
