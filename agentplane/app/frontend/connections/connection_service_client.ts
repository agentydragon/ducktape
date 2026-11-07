/** Browser adapters for ConnectionService's generated HTTP/JSON facade. */
import { create, fromJson, fromJsonString, toJson, type JsonObject, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";

import { fetchWithLogin, HttpError } from "../client";
import { type CallerServiceAccount, type Connection, type ConnectionGrant } from "./models";
import {
  DecideConnectionEnrollmentRequestSchema,
  DecideConnectionEnrollmentResponseSchema,
  EnrollmentAllowSchema,
  GrantStatus,
  ListCallerServiceAccountsResponseSchema,
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
  type Connection as ConnectionMessage,
  type Grant as GrantMessage,
} from "../../../mcp_edge/connection_service_pb";
import { Verdict } from "../../../action_service/action_service_pb";
import { ServiceAccountSchema, type ServiceAccount } from "../../../proto/identity_pb";

const OPERATOR = "/upstream/connections/v1/operator";
const MAX_INT64 = 9_223_372_036_854_775_807n;

export interface ConsentPreview {
  enrollment: {
    client_id: string;
    client_name: string | null;
    redirect_uri: string;
    expires_at: string;
    version: string;
  };
  service_accounts: CallerServiceAccount[];
  connections: Connection[];
}

export type ConsentDecision =
  | {
      verdict: "allow";
      expected_version: string;
      service_account: CallerServiceAccount;
      connection:
        | { kind: "new"; display_name: string }
        | {
            kind: "reconnect";
            connection_id: string;
            expected_version: string;
            authority_change_confirmed: true;
          };
    }
  | { verdict: "deny"; expected_version: string };

export interface ConsentResult {
  verdict: "allow" | "deny";
  redirect_url: string | null;
}

type FacadeMethod = "GET" | "POST" | "PATCH";
type ProtoTimestamp = NonNullable<ConnectionMessage["createdAt"]>;

function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`ConnectionService response omitted ${field}`);
  return value;
}

function readJsonObject(value: JsonValue, label: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

function parseJsonOrText(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function callFacade(method: FacadeMethod, path: string, body?: JsonValue): Promise<string> {
  const headers = new Headers({ Accept: "application/json" });
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(body);
  }
  const response = await fetchWithLogin(path, init);
  const text = await response.text();
  if (!response.ok) throw new HttpError(response, parseJsonOrText(text));
  return text;
}

function withoutPathField(body: JsonObject, field: string): JsonObject {
  const result = { ...body };
  delete result[field];
  return result;
}

function versionFromUi(value: string): bigint {
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error("Connection version must be a positive decimal integer");
  const version = BigInt(value);
  if (version > MAX_INT64) throw new Error("Connection version exceeds the protobuf int64 range");
  return version;
}

function timestampToIso(value: ProtoTimestamp | undefined, field: string): string {
  const jsonValue = toJson(TimestampSchema, required(value, field));
  if (typeof jsonValue !== "string") throw new Error(`ConnectionService returned an invalid ${field}`);
  return jsonValue;
}

function optionalTimestampToIso(value: ProtoTimestamp | undefined, field: string): string | null {
  return value === undefined ? null : timestampToIso(value, field);
}

function serviceAccountToUi(value: ServiceAccount | undefined): CallerServiceAccount {
  if (value === undefined) throw new Error("ConnectionService response omitted a ServiceAccount");
  return { namespace: value.namespace, name: value.name };
}

function grantStatusToUi(status: GrantStatus): ConnectionGrant["status"] {
  switch (status) {
    case GrantStatus.PENDING:
      return "pending";
    case GrantStatus.ACTIVE:
      return "active";
    case GrantStatus.REVOKED:
      return "revoked";
    case GrantStatus.UNSPECIFIED:
      throw new Error("ConnectionService returned an unspecified grant status");
    default:
      throw new Error(`ConnectionService returned unknown grant status ${status}`);
  }
}

function adaptGrant(message: GrantMessage): ConnectionGrant {
  return {
    id: message.id,
    connection_id: message.connectionId,
    revision: message.revision,
    caller: serviceAccountToUi(message.caller),
    issuer: message.issuer,
    client_id: message.clientId,
    status: grantStatusToUi(message.status),
    created_at: timestampToIso(message.createdAt, "grant creation time"),
    activated_at: optionalTimestampToIso(message.activatedAt, "grant activation time"),
    revoked_at: optionalTimestampToIso(message.revokedAt, "grant revocation time"),
  };
}

export function adaptConnection(message: ConnectionMessage): Connection {
  return {
    id: message.id,
    display_name: message.displayName,
    version: message.version.toString(),
    created_at: timestampToIso(message.createdAt, "Connection creation time"),
    updated_at: timestampToIso(message.updatedAt, "Connection update time"),
    grants: message.grants.map(adaptGrant),
    bound_caller: message.boundCaller === undefined ? null : serviceAccountToUi(message.boundCaller),
  };
}

export function adaptConnectionWatchFrame(
  value: JsonValue
):
  | { kind: "snapshot"; connections: Connection[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; connection: Connection }
  | { kind: "reconnect_required" } {
  const message = fromJson(WatchConnectionsResponseSchema, value);
  switch (message.frame.case) {
    case "snapshot":
      return { kind: "snapshot", connections: message.frame.value.connections.map(adaptConnection) };
    case "snapshotComplete":
      return { kind: "snapshot_complete" };
    case "upsert":
      return { kind: "upsert", connection: adaptConnection(message.frame.value) };
    case "reconnectRequired":
      return { kind: "reconnect_required" };
    case undefined:
      throw new Error("ConnectionService watch sent an empty frame");
  }
}

export async function listConnections(): Promise<Connection[]> {
  const response = fromJsonString(ListConnectionsResponseSchema, await callFacade("GET", `${OPERATOR}/connections`));
  return response.connections.map(adaptConnection);
}

export async function connectionServiceAccounts(): Promise<CallerServiceAccount[]> {
  const response = fromJsonString(
    ListCallerServiceAccountsResponseSchema,
    await callFacade("GET", `${OPERATOR}/caller-service-accounts`)
  );
  return response.serviceAccounts.map((account) => serviceAccountToUi(account));
}

function responseConnection(message: ConnectionMessage | undefined): Connection {
  if (message === undefined) throw new Error("ConnectionService response omitted its Connection");
  return adaptConnection(message);
}

export async function renameConnection(connection: Connection, displayName: string): Promise<Connection> {
  const request = create(UpdateConnectionRequestSchema, {
    connectionId: connection.id,
    expectedVersion: versionFromUi(connection.version),
    displayName,
  });
  const body = withoutPathField(
    readJsonObject(toJson(UpdateConnectionRequestSchema, request), "update request"),
    "connectionId"
  );
  const response = fromJsonString(
    UpdateConnectionResponseSchema,
    await callFacade("PATCH", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}`, body)
  );
  return responseConnection(response.connection);
}

export async function rebindConnection(connection: Connection, caller: CallerServiceAccount): Promise<Connection> {
  const request = create(RebindConnectionRequestSchema, {
    connectionId: connection.id,
    expectedVersion: versionFromUi(connection.version),
    serviceAccount: create(ServiceAccountSchema, { namespace: caller.namespace, name: caller.name }),
  });
  const body = withoutPathField(
    readJsonObject(toJson(RebindConnectionRequestSchema, request), "rebind request"),
    "connectionId"
  );
  const response = fromJsonString(
    RebindConnectionResponseSchema,
    await callFacade("POST", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}/rebind`, body)
  );
  return responseConnection(response.connection);
}

export async function unbindConnection(connection: Connection): Promise<Connection> {
  const request = create(UnbindConnectionRequestSchema, {
    connectionId: connection.id,
    expectedVersion: versionFromUi(connection.version),
  });
  const body = withoutPathField(
    readJsonObject(toJson(UnbindConnectionRequestSchema, request), "unbind request"),
    "connectionId"
  );
  const response = fromJsonString(
    UnbindConnectionResponseSchema,
    await callFacade("POST", `${OPERATOR}/connections/${encodeURIComponent(connection.id)}/unbind`, body)
  );
  return responseConnection(response.connection);
}

export async function previewConsent(handle: string): Promise<ConsentPreview> {
  const request = create(PreviewConnectionEnrollmentRequestSchema, { handle });
  const body = withoutPathField(
    readJsonObject(toJson(PreviewConnectionEnrollmentRequestSchema, request), "preview request"),
    "handle"
  );
  const [previewValue, callersValue, connectionsValue] = await Promise.all([
    callFacade("POST", `${OPERATOR}/connection-enrollments/${encodeURIComponent(handle)}/preview`, body),
    callFacade("GET", `${OPERATOR}/caller-service-accounts`),
    callFacade("GET", `${OPERATOR}/connections`),
  ]);
  const preview = fromJsonString(PreviewConnectionEnrollmentResponseSchema, previewValue);
  const callers = fromJsonString(ListCallerServiceAccountsResponseSchema, callersValue);
  const connections = fromJsonString(ListConnectionsResponseSchema, connectionsValue);
  return {
    enrollment: {
      client_id: preview.clientId,
      client_name: preview.clientName ?? null,
      redirect_uri: preview.redirectUri,
      expires_at: timestampToIso(preview.expiresAt, "enrollment expiry"),
      version: preview.version.toString(),
    },
    service_accounts: callers.serviceAccounts.map((account) => serviceAccountToUi(account)),
    connections: connections.connections.map(adaptConnection),
  };
}

export async function decideConsent(handle: string, decision: ConsentDecision): Promise<ConsentResult> {
  const expectedVersion = versionFromUi(decision.expected_version);
  const request =
    decision.verdict === "deny"
      ? create(DecideConnectionEnrollmentRequestSchema, {
          handle,
          expectedVersion,
          decision: { case: "deny", value: {} },
        })
      : create(DecideConnectionEnrollmentRequestSchema, {
          handle,
          expectedVersion,
          decision: {
            case: "allow",
            value: create(EnrollmentAllowSchema, {
              serviceAccount: create(ServiceAccountSchema, {
                namespace: decision.service_account.namespace,
                name: decision.service_account.name,
              }),
              connection:
                decision.connection.kind === "new"
                  ? {
                      case: "newConnection",
                      value: create(NewConnectionSchema, { displayName: decision.connection.display_name }),
                    }
                  : {
                      case: "reconnect",
                      value: create(ReconnectConnectionSchema, {
                        connectionId: decision.connection.connection_id,
                        expectedVersion: versionFromUi(decision.connection.expected_version),
                        authorityChangeConfirmed: decision.connection.authority_change_confirmed,
                      }),
                    },
            }),
          },
        });
  const body = withoutPathField(
    readJsonObject(toJson(DecideConnectionEnrollmentRequestSchema, request), "decision request"),
    "handle"
  );
  const response = fromJsonString(
    DecideConnectionEnrollmentResponseSchema,
    await callFacade("POST", `${OPERATOR}/connection-enrollments/${encodeURIComponent(handle)}/decision`, body)
  );
  let verdict: ConsentResult["verdict"];
  switch (response.verdict) {
    case Verdict.ALLOW:
      verdict = "allow";
      break;
    case Verdict.DENY:
      verdict = "deny";
      break;
    case Verdict.UNSPECIFIED:
    default:
      throw new Error("ConnectionService returned an unspecified consent verdict");
  }
  return { verdict, redirect_url: response.redirectUrl ?? null };
}
