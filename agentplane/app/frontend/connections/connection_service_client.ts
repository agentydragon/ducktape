/** Browser adapters for ConnectionService's generated HTTP/JSON facade. */
import { create, fromJson, fromJsonString, toJson, type JsonValue } from "@bufbuild/protobuf";

import { fetchText } from "../transport/client";
import { required } from "../transport/response_fields";
import {
  DecideConnectionEnrollmentRequestSchema,
  DecideConnectionEnrollmentResponseSchema,
  EnrollmentAllowSchema,
  GrantStatus,
  ListConnectionIdentitiesResponseSchema,
  ListConnectionsResponseSchema,
  NewConnectionSchema,
  PreviewConnectionEnrollmentRequestSchema,
  PreviewConnectionEnrollmentResponseSchema,
  RebindConnectionRequestSchema,
  RebindConnectionResponseSchema,
  ReconnectConnectionSchema,
  UnbindConnectionRequestSchema,
  UnbindConnectionResponseSchema,
  UpdateConnectionRequestSchema,
  UpdateConnectionResponseSchema,
  WatchConnectionsResponseSchema,
  type Connection,
  type WatchConnectionsResponse,
  type DecideConnectionEnrollmentResponse,
  type PreviewConnectionEnrollmentResponse,
} from "../../../mcp_edge/api/connection/connection_service_pb";
import { Verdict } from "../../../action_service/api/contracts/action_service_pb";
import type { Identity } from "../../../proto/identity_pb";

const OPERATOR = "/upstream/connections/v1/operator";
const MAX_INT64 = 9_223_372_036_854_775_807n;

export type ConsentPreview = {
  enrollment: PreviewConnectionEnrollmentResponse;
  identities: Identity[];
  connections: Connection[];
};

type ConsentConnectionChoice =
  | { kind: "new"; displayName: string }
  | { kind: "reconnect"; connectionId: string; expectedVersion: bigint; authorityChangeConfirmed: true };

export type ConsentDecision =
  | { verdict: "deny"; expectedVersion: bigint }
  | {
      verdict: "allow";
      expectedVersion: bigint;
      identity: Identity;
      connection: ConsentConnectionChoice;
    };

type FacadeMethod = "GET" | "POST" | "PATCH";

async function callFacade(method: FacadeMethod, path: string, body?: JsonValue): Promise<string> {
  const headers = new Headers({ Accept: "application/json" });
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(body);
  }
  return fetchText(path, init);
}

function expectedVersion(value: bigint): bigint {
  if (value < 1n) throw new Error("Connection version must be a positive integer");
  if (value > MAX_INT64) throw new Error("Connection version exceeds the protobuf int64 range");
  return value;
}

function validateConnection(connection: Connection): Connection {
  required(connection.createdAt, "Connection creation time");
  required(connection.updatedAt, "Connection update time");
  for (const grant of connection.grants) {
    required(grant.createdAt, "grant creation time");
    switch (grant.status) {
      case GrantStatus.PENDING:
      case GrantStatus.ACTIVE:
      case GrantStatus.REVOKED:
        break;
      case GrantStatus.UNSPECIFIED:
      default:
        throw new Error(`ConnectionService returned unknown grant status ${grant.status}`);
    }
  }
  return connection;
}

function validateConnections(connections: Connection[]): Connection[] {
  return connections.map(validateConnection);
}

function responseConnection(connection: Connection | undefined): Connection {
  return validateConnection(required(connection, "Connection"));
}

export function readConnectionWatchFrame(value: JsonValue): WatchConnectionsResponse {
  const frame = fromJson(WatchConnectionsResponseSchema, value);
  switch (frame.frame.case) {
    case "snapshot":
      frame.frame.value.connections.forEach(validateConnection);
      break;
    case "upsert":
      validateConnection(frame.frame.value);
      break;
    case undefined:
      throw new Error("ConnectionService watch sent an empty frame");
  }
  return frame;
}

export async function listConnections(): Promise<Connection[]> {
  const response = fromJsonString(ListConnectionsResponseSchema, await callFacade("GET", `${OPERATOR}/connections`));
  return validateConnections(response.connections);
}

export async function connectionIdentities(): Promise<Identity[]> {
  const response = fromJsonString(
    ListConnectionIdentitiesResponseSchema,
    await callFacade("GET", `${OPERATOR}/connection-identities`)
  );
  return response.identities;
}

export async function renameConnection(connection: Connection, displayName: string): Promise<Connection> {
  const request = create(UpdateConnectionRequestSchema, {
    expectedVersion: expectedVersion(connection.version),
    displayName,
  });
  const body = toJson(UpdateConnectionRequestSchema, request);
  const response = fromJsonString(
    UpdateConnectionResponseSchema,
    await callFacade("PATCH", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}`, body)
  );
  return responseConnection(response.connection);
}

export async function rebindConnection(connection: Connection, identity: Identity): Promise<Connection> {
  const request = create(RebindConnectionRequestSchema, {
    expectedVersion: expectedVersion(connection.version),
    identity,
  });
  const body = toJson(RebindConnectionRequestSchema, request);
  const response = fromJsonString(
    RebindConnectionResponseSchema,
    await callFacade("POST", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}/rebind`, body)
  );
  return responseConnection(response.connection);
}

export async function unbindConnection(connection: Connection): Promise<Connection> {
  const request = create(UnbindConnectionRequestSchema, {
    expectedVersion: expectedVersion(connection.version),
  });
  const body = toJson(UnbindConnectionRequestSchema, request);
  const response = fromJsonString(
    UnbindConnectionResponseSchema,
    await callFacade("POST", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}/unbind`, body)
  );
  return responseConnection(response.connection);
}

export async function previewConsent(handle: string): Promise<ConsentPreview> {
  const request = create(PreviewConnectionEnrollmentRequestSchema);
  const body = toJson(PreviewConnectionEnrollmentRequestSchema, request);
  const [previewValue, identitiesValue, connectionsValue] = await Promise.all([
    callFacade("POST", `${OPERATOR}/connection-enrollments/${encodeURIComponent(handle)}/preview`, body),
    callFacade("GET", `${OPERATOR}/connection-identities`),
    callFacade("GET", `${OPERATOR}/connections`),
  ]);
  const enrollment = fromJsonString(PreviewConnectionEnrollmentResponseSchema, previewValue);
  required(enrollment.expiresAt, "enrollment expiry");
  const identities = fromJsonString(ListConnectionIdentitiesResponseSchema, identitiesValue);
  const connections = fromJsonString(ListConnectionsResponseSchema, connectionsValue);
  return {
    enrollment,
    identities: identities.identities,
    connections: validateConnections(connections.connections),
  };
}

export async function decideConsent(
  handle: string,
  decision: ConsentDecision
): Promise<DecideConnectionEnrollmentResponse> {
  const request =
    decision.verdict === "deny"
      ? create(DecideConnectionEnrollmentRequestSchema, {
          expectedVersion: expectedVersion(decision.expectedVersion),
          decision: { case: "deny", value: {} },
        })
      : create(DecideConnectionEnrollmentRequestSchema, {
          expectedVersion: expectedVersion(decision.expectedVersion),
          decision: {
            case: "allow",
            value: create(EnrollmentAllowSchema, {
              identity: decision.identity,
              connection:
                decision.connection.kind === "new"
                  ? {
                      case: "newConnection",
                      value: create(NewConnectionSchema, { displayName: decision.connection.displayName }),
                    }
                  : {
                      case: "reconnect",
                      value: create(ReconnectConnectionSchema, {
                        connectionId: decision.connection.connectionId,
                        expectedVersion: expectedVersion(decision.connection.expectedVersion),
                        authorityChangeConfirmed: decision.connection.authorityChangeConfirmed,
                      }),
                    },
            }),
          },
        });
  const body = toJson(DecideConnectionEnrollmentRequestSchema, request);
  const response = fromJsonString(
    DecideConnectionEnrollmentResponseSchema,
    await callFacade("POST", `${OPERATOR}/connection-enrollments/${encodeURIComponent(handle)}/decision`, body)
  );
  switch (response.verdict) {
    case Verdict.ALLOW:
    case Verdict.DENY:
      break;
    case Verdict.UNSPECIFIED:
    default:
      throw new Error(`ConnectionService returned unknown consent verdict ${response.verdict}`);
  }
  return response;
}
