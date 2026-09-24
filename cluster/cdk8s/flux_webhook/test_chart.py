"""The ntfy Provider's headers, as the notification controller reads them."""

from __future__ import annotations

import pytest_bazel
import yaml

from cluster.cdk8s.flux_webhook import chart


def test_ntfy_provider_headers_parse_as_yaml_map() -> None:
    """The notification controller parses Provider Secret headers as a YAML map."""
    headers = yaml.safe_load(chart._NTFY_HEADERS)

    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer {{ .alertmanager_token }}"
    assert headers["Template"] == "yes"


if __name__ == "__main__":
    pytest_bazel.main()
