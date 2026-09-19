import pytest_bazel

from devinfra.k8s.extract_crd import _remove_path


def test_remove_path_supports_mappings_and_array_indexes() -> None:
    document = {"versions": [{"schema": {"properties": {"provider": {"keepersecurity": {"enabled": True}}}}}]}

    _remove_path(document, "versions.0.schema.properties.provider.keepersecurity")

    assert document == {"versions": [{"schema": {"properties": {"provider": {}}}}]}


if __name__ == "__main__":
    pytest_bazel.main()
