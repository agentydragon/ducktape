import pytest
import pytest_bazel

from haku.dispatch.prompt_lint import find_credentials


def test_clean_brief_passes():
    assert find_credentials("Refactor the parser in loom/ to use itertools.batched.") == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("key: sk-ant-api03-" + "a" * 20, "Anthropic API key"),
        ("token=ghp_" + "A1b2C3d4" * 5, "GitHub token"),
        ("aws AKIAIOSFODNN7EXAMPLE", "AWS access key id"),
        ("-----BEGIN RSA PRIVATE KEY-----", "PEM private key"),
        ("bearer eyJ" + "a" * 30 + ".eyJ" + "b" * 30 + ".sig", "JWT"),
        ("AGE-SECRET-KEY-1" + "Q" * 55, "age secret key"),
    ],
)
def test_credential_material_detected(payload: str, expected: str):
    assert expected in find_credentials(f"Use this to authenticate: {payload}")


def test_prose_mentioning_keys_passes():
    # Talking ABOUT credentials is fine; only material itself trips the lint.
    assert find_credentials("Rotate the GitHub token if the API returns 401.") == []


if __name__ == "__main__":
    pytest_bazel.main()
