import pytest
import pytest_bazel

from cluster.proxies.github_api_proxy.destinations import public_address


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "2002:7f00:1::",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "64:ff9b::7f00:1",
    ],
)
def test_special_addresses_are_not_public_origins(address: str) -> None:
    assert not public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_normal_global_addresses_are_permitted(address: str) -> None:
    assert public_address(address)


if __name__ == "__main__":
    pytest_bazel.main()
