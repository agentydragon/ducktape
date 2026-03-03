"""FastMCP auth configuration for the approval gate.

All tokens (operator JWT via SPA OAuth2 PKCE flow, agent JWT via
client_credentials) are verified as JWTs against the same JWKS endpoint.
Scopes in the JWT determine capabilities:

  propose — agent: wrapped backend tools, withdraw_action
  decide  — operator: approve_action, reject_action
  read    — both: list_actions, resource reads
"""

from __future__ import annotations

PROPOSE_SCOPE = "propose"
DECIDE_SCOPE = "decide"
READ_SCOPE = "read"
