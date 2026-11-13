import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

import {
  ConnectionSchema,
  GrantSchema,
  GrantStatus,
  type Connection,
} from "../../../../mcp_edge/api/connection/connection_service_pb";
import { IdentitySchema } from "../../../../proto/identity_pb";

export function sampleConnection(): Connection {
  const personal = create(IdentitySchema, { name: "personal", uid: "30000000-0000-4000-8000-000000000001" });
  return create(ConnectionSchema, {
    id: "10000000-0000-4000-8000-000000000001",
    displayName: "Claude desktop",
    version: 2n,
    createdAt: timestampFromDate(new Date("2026-09-09T12:00:00Z")),
    updatedAt: timestampFromDate(new Date("2026-09-09T12:01:00Z")),
    grants: [
      create(GrantSchema, {
        id: "20000000-0000-4000-8000-000000000001",
        connectionId: "10000000-0000-4000-8000-000000000001",
        revision: 1,
        identity: personal,
        issuer: "https://actions.example.test",
        clientId: "registered-client-123",
        status: GrantStatus.ACTIVE,
        createdAt: timestampFromDate(new Date("2026-09-09T12:00:00Z")),
        activatedAt: timestampFromDate(new Date("2026-09-09T12:01:00Z")),
      }),
    ],
    boundIdentity: personal,
  });
}
