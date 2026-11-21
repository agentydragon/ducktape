# agent-box TODO

> **Status: parked.** The manifests are parked under `cluster/parked/`. Revive the package before working on this backlog.

Revisit these follow-ups only if resuming the VM.

- [ ] **`claude` agent user** on agent-box: same multi-user pattern as `codex`
      (the `agentUsers` list + a per-user HM module under
      `cluster/parked/agent-box/nix/home/`),
      but running Claude Code against Anthropic directly (not via LiteLLM/z.ai).
- [ ] **Auto-provision the Codex CLI auth credential** (`~/.codex/auth.json`):
      today the ChatGPT login is done manually via the device-code flow and is lost
      on every image rebuild + VM recreate. Investigate SOPS-planting it (decrypted
      with the `agent-box-codex-user` key) — caveat: the token likely carries a
      refresh token that rotates on use, so a static plant could go stale; check
      whether the Codex CLI rewrites `auth.json` after refresh.
