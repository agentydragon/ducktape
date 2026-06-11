import hashlib

import pytest
import pytest_bazel

from gatelet.server.endpoints.challenge import compute_correct_option


def test_requires_power_of_two():
    with pytest.raises(AssertionError):
        compute_correct_option("k", "n", 3)


def test_requires_max_256():
    with pytest.raises(AssertionError):
        compute_correct_option("k", "n", 512)


def test_uses_last_byte():
    key = "key"
    nonce = "nonce"
    expected = hashlib.sha256(f"{key}{nonce}".encode()).digest()[-1] % 16
    assert compute_correct_option(key, nonce, 16) == expected


if __name__ == "__main__":
    pytest_bazel.main()
