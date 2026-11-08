"""Serialization contract for the shared workload listener settings."""

from pathlib import Path
from typing import Any

import pytest_bazel
from pydantic import TypeAdapter

from agentplane.workload_auth.listener import TLS, KubernetesBearerAuth, Listener, SPIFFEAuth


def test_default_authentication_modes_survive_nested_yaml_serialization() -> None:
    listener = Listener(
        listen_address="0.0.0.0:8080",
        tls=TLS(cert_file=Path("/trust/tls.crt"), key_file=Path("/trust/tls.key")),
        auth=[KubernetesBearerAuth(audience="test-audience"), SPIFFEAuth(ca_file=Path("/trust/ca.crt"))],
    )

    # yaml_config uses these TypeAdapter options for a settings mapping. Go's
    # AuthenticationDecodeHook requires mode even when Python supplies its default.
    nested = TypeAdapter(dict[str, Any]).dump_python({"rpc": listener}, mode="json", exclude_unset=True)
    rpc = nested["rpc"]
    assert [method["mode"] for method in rpc["auth"]] == ["kubernetes_bearer", "spiffe"]
    Listener.model_validate(rpc)


if __name__ == "__main__":
    pytest_bazel.main()
