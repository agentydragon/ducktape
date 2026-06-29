# gmail-labeling

Cluster-internal MCP server that lets the Haku background agent manage Gmail labels
confined to the `haku/` namespace. Source + contract: <../../../../haku/gmail_labeling/>
(`SPEC.md` is the closure invariant). Deployed as a sibling MCP service (the
`tana-mcp-ro` shape), not behind Airlock — the tool surface is safe by construction.

## Credential model (two layers)

- **Haku → server:** static bearer `haku-gmail-labeling-token` (this dir, SOPS), reflected
  into `haku-sandbox`. The only gate. Haku calls `https://gmail-labeling.allegedly.works/mcp`
  with it (see `httproute.yaml`).
- **Server → Gmail:** a `gmail.modify` access token **provisioned and rotated by Airlock**.
  Airlock holds the refresh token (`gmail-modify-tokens`, airlock ns) and writes an
  access-only secret (`gmail-modify-access-token`); ESO mirrors that into this namespace
  (see `agents/airlock/eso-access-tokens.yaml`), and the pod mounts it at `/etc/gmail-token`.
  The server re-reads the rotating token via google-auth's `refresh_handler` — no restart.
  This token is **never** delivered to a sandbox, so no agent holds Gmail write scope.

## One-time bootstrap (operator)

The pod stays in `ContainerCreating` until `gmail-modify-access-token` exists, which needs a
one-time browser consent for the `gmail.modify` scope:

1. Visit `https://airlock.allegedly.works/oauth/authorize/gmail_modify` and consent as the
   target Google account. Airlock's callback writes `gmail-modify-tokens` (refresh) and
   `gmail-modify-access-token` (access-only) into the `airlock` namespace; the refresh loop
   keeps the access token fresh thereafter.
2. ESO mirrors `gmail-modify-access-token` into this namespace within ~1m; the pod starts.

Provider config: `agents/airlock/config.yaml` (`gmail_modify`); the same Google OAuth client
as `google` is reused via `GMAIL_MODIFY_CLIENT_ID/SECRET` (`agents/airlock/deployment.yaml`).

## Image

`ghcr.io/agentydragon/gmail-labeling`, built from `//haku/gmail_labeling:image`, pushed by
`.github/workflows/push-images.yml`, tag-tracked by Flux image automation
(`flux-image-automation-ghcr/gmail-labeling-image-{repository,policy}.yaml`). Standard GHCR
image automation; **deviation:** none beyond registering the `ImageRepository` with the
webhook receiver.
