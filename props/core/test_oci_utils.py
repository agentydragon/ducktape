"""Tests for oci_utils module."""

import pytest
import pytest_bazel

from props.core.agent_types import AgentType
from props.core.oci_utils import RegistryProxyConfig, UpstreamRegistryConfig, is_digest

_UPSTREAM = UpstreamRegistryConfig(
    url="http://forgejo-http.forgejo:3000", username=None, password=None, project="props"
)
_NO_PROJECT = UpstreamRegistryConfig(url="http://reg:5000", username=None, password=None, project=None)


class TestPullAuthority:
    """Tests for RegistryProxyConfig.pull_authority()."""

    def test_pull_host_overrides(self) -> None:
        # pull_host without pull_port drops the proxy port entirely — the pull
        # endpoint is a different authority, not the proxy host renamed.
        config = RegistryProxyConfig(host="props-registry-proxy", port=8000, pull_host="props-registry.allegedly.works")
        assert config.pull_authority() == "props-registry.allegedly.works"

    def test_pull_host_with_pull_port(self) -> None:
        config = RegistryProxyConfig(
            host="props-registry-proxy", port=8000, pull_host="props-registry.allegedly.works", pull_port=5000
        )
        assert config.pull_authority() == "props-registry.allegedly.works:5000"

    def test_standard_ports_omitted(self) -> None:
        config = RegistryProxyConfig(
            host="props-registry-proxy", port=8000, pull_host="props-registry.allegedly.works", pull_port=443
        )
        assert config.pull_authority() == "props-registry.allegedly.works"

    def test_port_80_omitted(self) -> None:
        config = RegistryProxyConfig(
            host="props-registry-proxy", port=8000, pull_host="props-registry.allegedly.works", pull_port=80
        )
        assert config.pull_authority() == "props-registry.allegedly.works"


class TestBuildOciReference:
    """Tests for RegistryProxyConfig.build_oci_reference()."""

    DIGEST = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def test_local_registry(self) -> None:
        # Boundary artifact the container runtime parses: authority/repo@digest.
        config = RegistryProxyConfig(host="localhost", port=8000)
        ref = config.build_oci_reference(AgentType.CRITIC, self.DIGEST)
        assert ref == f"localhost:8000/critic@{self.DIGEST}"


class TestProxyUrl:
    """Tests for RegistryProxyConfig.proxy_url."""

    def test_ignores_pull_host(self) -> None:
        config = RegistryProxyConfig(host="props-registry-proxy", port=8000, pull_host="props-registry.allegedly.works")
        assert config.proxy_url == "http://props-registry-proxy:8000"


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


class TestRewritePath:
    """Tests for UpstreamRegistryConfig.rewrite_path() (client -> upstream)."""

    def test_prepends_project(self) -> None:
        assert _UPSTREAM.rewrite_path("/v2/critic/blobs/uploads/") == "/v2/props/critic/blobs/uploads/"

    def test_v2_root_unchanged(self) -> None:
        assert _UPSTREAM.rewrite_path("/v2/") == "/v2/"

    def test_idempotent_no_double_prefix(self) -> None:
        # A path already under the project must not be prefixed again.
        assert _UPSTREAM.rewrite_path("/v2/props/critic/manifests/x") == "/v2/props/critic/manifests/x"

    def test_project_exact_unchanged(self) -> None:
        assert _UPSTREAM.rewrite_path("/v2/props") == "/v2/props"

    def test_no_project_passthrough(self) -> None:
        assert _NO_PROJECT.rewrite_path("/v2/critic/blobs/uploads/") == "/v2/critic/blobs/uploads/"


class TestRewriteLocation:
    """Tests for UpstreamRegistryConfig.rewrite_location() (upstream -> client)."""

    def test_relative_strips_project(self) -> None:
        assert _UPSTREAM.rewrite_location("/v2/props/critic/blobs/uploads/abc") == "/v2/critic/blobs/uploads/abc"

    def test_preserves_query(self) -> None:
        assert (
            _UPSTREAM.rewrite_location("/v2/props/critic/blobs/uploads/abc?_state=xyz")
            == "/v2/critic/blobs/uploads/abc?_state=xyz"
        )

    def test_absolute_upstream_host_stripped(self) -> None:
        assert (
            _UPSTREAM.rewrite_location("http://forgejo-http.forgejo:3000/v2/props/critic/blobs/uploads/abc?_state=xyz")
            == "/v2/critic/blobs/uploads/abc?_state=xyz"
        )

    def test_absolute_public_host_stripped(self) -> None:
        assert (
            _UPSTREAM.rewrite_location("https://git.allegedly.works/v2/props/critic/blobs/uploads/abc")
            == "/v2/critic/blobs/uploads/abc"
        )

    def test_path_without_project_only_strips_host(self) -> None:
        assert _UPSTREAM.rewrite_location("/v2/critic/manifests/sha256:abc") == "/v2/critic/manifests/sha256:abc"

    def test_empty_unchanged(self) -> None:
        assert _UPSTREAM.rewrite_location("") == ""

    def test_roundtrip_back_to_upstream_path(self) -> None:
        # The core invariant: a client that follows the rewritten Location and
        # the proxy that re-applies rewrite_path land on the original upstream
        # path — no namespace drift, so the manifest finds its blobs.
        upstream_path = "/v2/props/critic/blobs/uploads/abc"
        assert _UPSTREAM.rewrite_path(_UPSTREAM.rewrite_location(upstream_path)) == upstream_path


if __name__ == "__main__":
    pytest_bazel.main()
