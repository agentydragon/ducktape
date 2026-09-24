"""The Flux ntfy Provider's templated headers parse the way the notification controller reads them."""

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def test_ntfy_provider_headers_parse_as_yaml_map() -> None:
    """The notification controller parses Provider Secret headers as a YAML map."""
    manifests = get_required_path("_main/cluster/generated/flux-webhook/flux-webhook.k8s.yaml").read_text()
    external_secret = one(
        obj
        for obj in yaml.safe_load_all(manifests)
        if obj and obj["kind"] == "ExternalSecret" and obj["metadata"]["name"] == "ntfy-webhook"
    )
    headers = yaml.safe_load(external_secret["spec"]["target"]["template"]["data"]["headers"])

    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer {{ .alertmanager_token }}"
    assert headers["Template"] == "yes"


if __name__ == "__main__":
    pytest_bazel.main()
