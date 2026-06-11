# Grocy Alternatives Research (2026-04)

Evaluated whether to migrate from Grocy to an alternative with a more modern API,
native OIDC, and/or existing MCP servers.

## Conclusion

**No drop-in replacement exists.** The self-hosted landscape splits into
recipe/meal-planning tools and general home inventory tools — none combines
Grocy's full stock management (add/consume/transfer, expiry tracking, shopping
lists, barcode scanning) with a modern API and native OIDC.

Decision: stay with Grocy, harden our MCP server layer (strip unreliable output
schema validation, fix retry safety, expand e2e test coverage).

## Comparison

| Tool                 | Inventory/Stock  | Expiry tracking | Shopping lists |    OIDC    |   API quality   |   MCP server    |
| -------------------- | :--------------: | :-------------: | :------------: | :--------: | :-------------: | :-------------: |
| **Grocy**            |       Yes        |       Yes       |      Yes       | No (proxy) | Bad (spec lies) |   Yes (ours)    |
| **KitchenOwl**       |        No        |       No        |      Yes       |    Yes     |     No docs     |       No        |
| **HomeBox**          | Possessions only |       No        |       No       |    Yes     | Good (Swagger)  | Yes (read-only) |
| **Mealie**           |        No        |       No        |       No       |    Yes     |      Good       |       No        |
| **Pantry (netz-sg)** |       Yes        |       Yes       |      Yes       |     No     |     No spec     |       No        |

## Details

### Grocy (current)

- PHP/SQLite, server-side rendered frontend, REST API as secondary interface
- OpenAPI spec repeatedly diverges from actual behavior (empty enums, wrong
  response types, optional fields that crash when omitted)
- No batch API — frontend bypasses the API entirely via direct DB queries
- Auth via Authentik reverse proxy outpost

### KitchenOwl (~3k stars, active)

- Native OIDC, native mobile apps, community Helm chart
- Shopping list + recipe manager only — no stock/inventory, no expiry dates

### HomeBox (~5.7k stars, active)

- Native OIDC/SSO, auto-generated Swagger docs, Helm charts, Go-based
- Has MCP server (`jeeves5454/Homebox-mcp`, read-only)
- Designed for household possessions (tools, electronics, furniture), not consumables
- Could complement Grocy for durable goods

### Mealie (~8k+ stars, active)

- Excellent native OIDC with group-based access control
- Purely recipe and meal planning — no inventory at all

### Pantry by netz-sg (very new, low stars)

- Closest feature match: pantry inventory with expiry dates, recipes, shopping lists
- Next.js/TypeScript/SQLite, JWT auth
- Extremely immature, single-user only, tiny community, no OIDC, no MCP server, no
  OpenAPI spec. High abandonment risk.
