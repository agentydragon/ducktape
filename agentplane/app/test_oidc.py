"""The app's OIDC settings pin keys only for an issuer under the reserved .test domain."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.app.oidc import OIDCSettings

_KEYS = frozenset({"test-ed25519-public-key"})


def test_a_test_issuer_may_pin_keys() -> None:
    OIDCSettings(
        issuer="https://operator-tokens.test", public_base_url="https://app.example.test", static_ed25519_keys=_KEYS
    )


@pytest.mark.parametrize(
    "issuer", ["https://auth.example.com/application/o/agentplane/", "https://operator-tokens.test.example.com/"]
)
def test_a_real_issuer_may_not_pin_keys(issuer: str) -> None:
    with pytest.raises(ValidationError):
        OIDCSettings(issuer=issuer, public_base_url="https://app.example.test", static_ed25519_keys=_KEYS)


if __name__ == "__main__":
    pytest_bazel.main()
