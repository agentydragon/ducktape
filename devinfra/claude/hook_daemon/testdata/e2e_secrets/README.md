# E2E Test Secrets Fixture

Test-only SOPS-encrypted secret files + age key + web-style profile used
by the container E2E test at
<../../session_start/container_e2e/test_container_e2e.py>.

## What's Here

| File                                 | Purpose                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_age.key`                       | Test-only age keypair. **Not a real secret.** Only decrypts the fake files in this directory.                                                     |
| `buildbuddy.yaml`                    | Encrypted `buildbuddy_api_key: test-fake-bb-key` — mounted at `/project/secrets/buildbuddy.yaml` in the test container.                           |
| `github-pat-agentydragon-agent.yaml` | Encrypted `github_token: test-fake-gh-agent-token` — mounted at `/project/secrets/github-pat-agentydragon-agent.yaml`.                            |
| `github-ci-read-pat.yaml`            | Encrypted `github_token: test-fake-ci-read-token` — mounted at `/project/secrets/github-ci-read-pat.yaml`.                                        |
| `claude-web-k8s-token.yaml`          | Encrypted `k8s_token: test-fake-k8s-token` — mounted at `/project/secrets/claude-web-k8s-token.yaml`, consumed by the daemon's kubeconfig writer. |
| `profile.yaml`                       | Web-style profile used by the test: real `startup_env_script`, real `k8s:` block, no container runtime / tmpfs / BES / bg commands.               |

## Why Fake Encrypted Files?

The container E2E test exercises the **real** `devinfra/secrets/web_env.sh`
and the **real** kubeconfig writer against the **real** SOPS file paths
(`secrets/*.yaml`) — just with fake values encrypted by a test-only age
key. This catches regressions in the secret flow (env script + daemon
kubeconfig path) that a hand-rolled mock would miss.

## Regenerating

```bash
# 1. Regenerate the keypair (rarely needed)
age-keygen -o test_age.key

# 2. Re-encrypt fixtures (each file is independent; key is in test_age.key)
TEST_AGE_PUB=$(grep 'public key' test_age.key | awk '{print $NF}')
for f in buildbuddy.yaml github-pat-agentydragon-agent.yaml \
         github-ci-read-pat.yaml claude-web-k8s-token.yaml; do
  # Edit the plaintext content inline, then re-encrypt with only the test key.
  # SOPS --age flag avoids inheriting the repo's .sops.yaml creation rules.
  sops encrypt --age "$TEST_AGE_PUB" <(echo "<plaintext YAML>") > "$f"
done
```
