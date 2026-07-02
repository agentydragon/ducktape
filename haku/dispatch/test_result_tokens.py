import pytest_bazel

from haku.dispatch import result_tokens


def test_roundtrip():
    token = result_tokens.mint("secret", "job-abc")
    assert result_tokens.verify("secret", "job-abc", token)


def test_wrong_job_rejected():
    token = result_tokens.mint("secret", "job-abc")
    assert not result_tokens.verify("secret", "job-other", token)


def test_wrong_secret_rejected():
    token = result_tokens.mint("secret", "job-abc")
    assert not result_tokens.verify("other", "job-abc", token)


def test_garbage_rejected():
    assert not result_tokens.verify("secret", "job-abc", "not-a-token")


if __name__ == "__main__":
    pytest_bazel.main()
