# openclaw — credential retention only

The OpenClaw gateway, its operator, and the whole OpenShell stack were deleted on
2026-07-31. `public-coder-agent` is the reference agent now — same OpenClaw image,
plain Deployment, `sandbox.mode: "off"`. Rationale and the evaluated alternatives:
<../../../../plans/personal_agents/verdicts.md>.

What survives here is **only the credentials**, kept because each is unique and
costs real effort to re-mint:

| Secret                        | Namespace          | What it is                                       |
| ----------------------------- | ------------------ | ------------------------------------------------ |
| `openclaw-anthropic-api-key`  | `openclaw-gateway` | Anthropic workspace key, $300/mo limit           |
| `openclaw-openai-api-key`     | `openclaw-gateway` | OpenAI project key with a $100/mo project budget |
| `openclaw-telegram-bot-token` | `openclaw-gateway` | `@agentydragonopenclawbot` from @BotFather       |
| `ibkr-flex-query-credentials` | `openclaw-sandbox` | IBKR Flex Query token + query ID                 |

**`openclaw-gateway` is not yet clean.** The 2026-07-31 teardown left an
`OpenClawInstance/openclaw` behind — Flux pruned its manifest, but its finalizer
has no controller left to run it, so it lingers and keeps a scaled-to-zero
`StatefulSet/openclaw` and two orphaned PVCs alive (`openclaw-data`, 20Gi on the
Proxmox-pinned `local-path-proxmox`; and `openshell-data-openshell-gateway-0`,
1Gi). The three `openclaw.rocks` CRDs survived too, as Helm does not remove CRDs
on uninstall. Clearing that up is tracked in <../../TODO.md> § "Retire the
`openclaw-*` namespaces"; until then, read the sections below as describing what
_should_ be here, not everything that is.

The two namespaces exist for one reason: **each SOPS document pins its namespace in
`metadata`, and these files set no `mac_only_encrypted`, so the document MAC covers
that field.** Re-homing them into `shared-secrets` is not a text edit — it needs the
cluster age key and a `sops` round-trip. Keeping the namespaces was the option that
loses nothing and requires no key.

`openclaw-sandbox` is genuinely empty; `openclaw-gateway` is not, per the note
above. The privileged Pod Security labels that `openclaw-sandbox` carried for
OpenShell's supervisor are gone, and
`openclaw-gateway` opts out of Goldilocks since there is nothing to right-size.

Not to be confused with the `openclaw` **ImageRepository/ImagePolicy** under
<../../flux-image-automation-ghcr/>, which are deliberately alive: they track
`ghcr.io/agentydragon/openclaw`, the image `public-coder-agent` runs.

## When this directory should disappear

Either of these makes it dead weight — delete the whole tree then:

- The credentials get re-homed (a `sops` pass moving them into
  `agents/shared-secrets/`, which needs the age key), or
- They are no longer wanted, in which case revoke them upstream too: the Anthropic
  and OpenAI keys at their consoles, the Telegram token via @BotFather, the IBKR
  Flex token in Account Management. Deleting the Secret alone leaves a live
  credential in the wild.

Both `namespace.yaml` files carry a `CLEANUP(added 2026-07-31)` tombstone saying
the same thing next to the resource itself. The sequenced version — what to move,
what to update alongside it, and the grep that tells you the tree is safe to
delete — is in <../../TODO.md> § "Retire the `openclaw-*` namespaces".
