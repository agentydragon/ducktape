# Home Assistant API Proxy

A FastAPI service that proxies the Home Assistant REST API with per-token,
per-entity access control.

## Why

Home Assistant has a granular permission engine (per-entity, per-device,
per-area, per-domain with read/control/edit actions), but tokens always inherit
their user's full permissions. Only 3 hardcoded groups exist (admin, user,
read-only) with no UI or API to create custom groups. The fine-grained policy
system is largely unused.

This proxy fills the gap: each proxy token gets its own policy specifying
exactly which entities it can read or control.

## Policy Model

Policies are evaluated in priority order — first match wins:

1. `entity_ids` — exact entity ID
2. `device_ids` — entity's parent device (via HA entity/device registry)
3. `area_ids` — device's area
4. `domains` — entity ID prefix (`light.`, `switch.`, etc.)
5. `all` — blanket fallback

Each level maps to an `AccessRule` with `read` and `control` booleans
(default: deny).

## Proxied Endpoints

| Endpoint                                    | Behavior                                    |
| ------------------------------------------- | ------------------------------------------- |
| `GET /api/states`                           | Filters returned states by read policy      |
| `GET /api/states/{id}`                      | Checks read permission for entity           |
| `POST /api/services/{domain}/{service}`     | Resolves targets, checks control permission |
| `GET /api/`, `/api/config`, `/api/services` | Pass-through (auth only)                    |
| Everything else                             | Blocked (403)                               |

Service calls without entity/device/area targets (e.g. `homeassistant.restart`)
are blocked as admin-level operations.
