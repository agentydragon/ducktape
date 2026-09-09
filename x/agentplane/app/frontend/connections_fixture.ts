import type { Connection } from "./client";

export function sampleConnection(): Connection {
  return {
    id: "10000000-0000-4000-8000-000000000001",
    display_name: "Claude desktop",
    version: 2,
    created_at: "2026-09-09T12:00:00Z",
    updated_at: "2026-09-09T12:01:00Z",
    grants: [
      {
        id: "20000000-0000-4000-8000-000000000001",
        connection_id: "10000000-0000-4000-8000-000000000001",
        revision: 1,
        identity_id: "personal",
        issuer: "https://actions.example.test",
        client_id: "registered-client-123",
        status: "active",
        activation_deadline: "2026-09-09T12:15:00Z",
        created_at: "2026-09-09T12:00:00Z",
        activated_at: "2026-09-09T12:01:00Z",
        revoked_at: null,
      },
    ],
  };
}
