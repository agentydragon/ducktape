import type { Connection } from "../models";

export function sampleConnection(): Connection {
  return {
    id: "10000000-0000-4000-8000-000000000001",
    display_name: "Claude desktop",
    version: "2",
    created_at: "2026-09-09T12:00:00Z",
    updated_at: "2026-09-09T12:01:00Z",
    grants: [
      {
        id: "20000000-0000-4000-8000-000000000001",
        connection_id: "10000000-0000-4000-8000-000000000001",
        revision: 1,
        caller: { namespace: "agentplane-test", name: "personal" },
        issuer: "https://actions.example.test",
        client_id: "registered-client-123",
        status: "active",
        created_at: "2026-09-09T12:00:00Z",
        activated_at: "2026-09-09T12:01:00Z",
        revoked_at: null,
      },
    ],
    bound_caller: { namespace: "agentplane-test", name: "personal" },
  };
}
