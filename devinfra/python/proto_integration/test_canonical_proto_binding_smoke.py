from devinfra.python.proto_integration import status_consumer


def test_first_party_proto_imports_canonical_status_from_runfiles() -> None:
    status = status_consumer.make_status(7, "permission denied")

    assert status.code == 7
    assert status.message == "permission denied"


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
