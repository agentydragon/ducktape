# Claude Hooks Secrets

This directory contains age-encrypted secrets that are decrypted during Claude Code session startup.

## Structure

Each `*.age` file decrypts to one of two JSON formats:

### Flat env vars (legacy, all other secrets)

```json
{ "ENV_VAR_NAME": "value", "ANOTHER_VAR": "another value" }
```

All keys are exported as shell environment variables.

### Typed secrets (new format)

```json
{"type": "<type>", ...fields}
```

The `"type"` discriminator triggers type-specific handling. Typed secrets are **not** exported
to the shell — they are consumed internally by the hook.

#### `kubeconfig`

```json
{
  "type": "kubeconfig",
  "server": "https://allegedly.works:6443",
  "ca_b64": "<base64-encoded cluster CA PEM>",
  "token": "<ServiceAccount token>"
}
```

The hook builds a kubeconfig YAML from these fields, injecting the Anthropic TLS proxy CA
alongside the cluster CA so kubectl works through the TLS-inspecting proxy.

## Kubeconfig Setup

The kubeconfig secret is regenerated automatically by `bazel run //cluster:bootstrap` after
cluster deployment. The bootstrap script uses `KubeconfigSecret` from
<tools/claude_hooks/kubeconfig_setup.py> to build and encrypt the secret.

## Security

- Secrets are encrypted with age (X25519)
- Decryption key is provided via `DUCKTAPE_CLAUDE_HOOKS_SECRETS_AGE_KEY` env var
- The `claude-code-web` ServiceAccount has access to the `claude-sandbox` namespace
- Resource quotas limit what can be created in the sandbox
