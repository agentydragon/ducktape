import pytest_bazel

from x.agentplane.runner.main import inherited_environment


def test_base_environment_carries_the_egress_wiring_and_neither_provider_credential() -> None:
    """A tool in the sandbox reaches the outside only through the sidecar, so the proxy variables have
    to survive into the harness child. Neither provider key belongs in the base every child starts
    from: each adapter adds its own on top, which is what keeps a Codex child from seeing the
    Anthropic token and a Claude child from seeing the OpenAI one."""
    child = inherited_environment(
        {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://127.0.0.1:3128",
            "https_proxy": "http://127.0.0.1:3128",
            "NO_PROXY": "127.0.0.1,localhost",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
            "ANTHROPIC_AUTH_TOKEN": "test-anthropic-token",
            "OPENAI_API_KEY": "test-openai-key",
        }
    )
    assert child["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert child["https_proxy"] == "http://127.0.0.1:3128"
    assert child["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/ca-certificates.crt"
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "OPENAI_API_KEY" not in child


if __name__ == "__main__":
    pytest_bazel.main()
