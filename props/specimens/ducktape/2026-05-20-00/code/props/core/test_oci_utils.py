"""Tests for oci_utils module."""

import pytest
import pytest_bazel

from props.core.agent_types import AgentType
from props.core.oci_utils import RegistryProxyConfig, is_digest


class TestPullAuthority:
    """Tests for RegistryProxyConfig.pull_authority()."""

    def test_host_and_port(self) -> None:
        config = RegistryProxyConfig(host="localhost", port=8000)
        assert config.pull_authority() == "localhost:8000"

    def test_pull_host_overrides(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works")
        assert config.pull_authority() == "props.allegedly.works"

    def test_pull_host_with_pull_port(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works", pull_port=5000)
        assert config.pull_authority() == "props.allegedly.works:5000"

    def test_standard_ports_omitted(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works", pull_port=443)
        assert config.pull_authority() == "props.allegedly.works"

    def test_port_80_omitted(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works", pull_port=80)
        assert config.pull_authority() == "props.allegedly.works"

    def test_no_pull_host_uses_host_and_port(self) -> None:
        config = RegistryProxyConfig(host="127.0.0.1", port=5000)
        assert config.pull_authority() == "127.0.0.1:5000"


class TestBuildOciReference:
    """Tests for RegistryProxyConfig.build_oci_reference()."""

    DIGEST = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def test_local_registry(self) -> None:
        config = RegistryProxyConfig(host="localhost", port=8000)
        ref = config.build_oci_reference(AgentType.CRITIC, self.DIGEST)
        assert ref == f"localhost:8000/critic@{self.DIGEST}"

    def test_external_registry(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works")
        ref = config.build_oci_reference(AgentType.CRITIC, self.DIGEST)
        assert ref == f"props.allegedly.works/critic@{self.DIGEST}"

    def test_grader_agent_type(self) -> None:
        config = RegistryProxyConfig(host="localhost", port=8000)
        ref = config.build_oci_reference(AgentType.GRADER, self.DIGEST)
        assert ref == f"localhost:8000/grader@{self.DIGEST}"


class TestProxyUrl:
    """Tests for RegistryProxyConfig.proxy_url."""

    def test_uses_host_and_port(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000)
        assert config.proxy_url == "http://props:8000"

    def test_ignores_pull_host(self) -> None:
        config = RegistryProxyConfig(host="props", port=8000, pull_host="props.allegedly.works")
        assert config.proxy_url == "http://props:8000"


class TestIsDigest:
    """Tests for is_digest()."""

    @pytest.mark.parametrize(
        "ref",
        [
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "sha384:0123456789abcdef0123456789abcdef",
            "sha512:0123456789abcdef0123456789abcdef",
        ],
    )
    def test_valid_digests(self, ref: str) -> None:
        assert is_digest(ref)

    @pytest.mark.parametrize("ref", ["latest", "v1.0", "critic:latest", "localhost:8000/critic@sha256:abc"])
    def test_non_digests(self, ref: str) -> None:
        assert not is_digest(ref)


if __name__ == "__main__":
    pytest_bazel.main()
