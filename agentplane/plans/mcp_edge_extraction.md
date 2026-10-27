# MCP edge extraction

The standalone edge owns external Connection/OAuth serving and is used by the k3s system tests.
The Action Service no longer contains the integrated Connection/OAuth runtime, registrations, or
callers. Generated Actions config retains false-only/no-op compatibility fields until the new image
is observed running. See the
[retained-state evidence](../debug/actions_schema_retirement_20261023.md) and the
[edge rollout sequence](../mcp_edge/README.md#configuration-rollout-sequencing) for the
separate data and config gates.

## Remaining work

1. **Finish compatibility cleanup.** After the new Action Service image is observed running,
   remove `integrated_connection_service` and `max_wait_seconds` from generated manifests and
   deploy that config. Remove their parser fields only in a later image. `direct_wait_seconds`
   stays because `orchestration_v1` uses it. Endpoint removal does not move or delete database
   rows, edit migrations, or drop tables; any future schema cleanup requires a separate verified
   ownership and retention plan. Actions keeps its upstream `/oauth/client-metadata.json` and the
   app its `/mcp-linkage/callback`.

## Cutover constraints

- Preserve Connection/grant identities, requests/history, Sandboxes, suspended state and volumes.
  The operator waived restoration testing and accepts database-stored secrets without application
  encryption; issued opaque tokens remain hashed.
- Keep one writer for transferred records. The Action Service source in this change no longer
  registers the legacy Connection gRPC service or facade. Never infer data ownership from endpoint
  removal, and do not restart a legacy writer after a separately verified data transfer.
- Use published, Flux-selected images, after the system tests that exercise the change pass, with
  the rollback in each PR ([changes that can break staging](../AGENTS.md#changes-that-can-break-staging)).
  Images that read removed Actions columns cannot run against its current schema.
- Keep operator OIDC independent of external MCP OAuth. The edge acts only as an explicitly
  permitted, currently bound ServiceAccount; Actions still checks namespace, audience, UID and
  caller label. No all-owner reader identity. See the [edge authority contract](../mcp_edge/README.md#authority).
