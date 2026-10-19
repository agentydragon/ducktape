# grocy_mcp operations

## Deploy

- **Terraform**: `tf/gitops/agent-machine-access/grocy-<household>.tf` owns the household's Grocy
  proxy provider, the `authentik_provider_oauth2.grocy_mcp_<household>` login provider OIDCProxy
  wraps (listed in the proxy provider's `jwt_federation_providers`), and the
  `grocy-mcp-oidc-<household>` Secret (`client_id`, `client_secret`, `grocy_proxy_client_id`),
  created in `authentik` and reflected into the household namespace.
- **Manifests**: generated per household by <../../cluster/cdk8s/grocy/mcp.py>, settings file
  included.

## Refresh the OpenAPI spec

`@grocy_openapi_spec` (`http_file` in `MODULE.bazel`) fetches
`https://raw.githubusercontent.com/grocy/grocy/<tag>/grocy.openapi.json`. On a new Grocy release,
bump its `urls` and `sha256` (`curl -sSfL <url> | sha256sum`), then run
`bbr test //grocy_mcp:test_server`, which catches spec quirks FastMCP rejects (e.g. empty enums).

## End-to-end verification

After Flux reconciles:

```bash
curl -i https://grocy-mcp-sf.allegedly.works/mcp   # 401, WWW-Authenticate: Bearer resource_metadata=…
claude mcp add --transport http grocy-mcp-sf https://grocy-mcp-sf.allegedly.works/mcp
```

Then list `locations` (e.g. `entities_list`); expect the user's Grocy locations. Success with
empty identity headers means `ak_proxy` is missing from the exchange or its scope mapping is not
on the proxy provider. An empty result can also mean the user lacks permission on those records.
