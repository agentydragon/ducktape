import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import {
  CreateIdentityResponseSchema,
  GrantIdentityPolicyBindingResponseSchema,
  type Identity,
} from "../../../policy/v1/policy_service_pb";
import {
  ListSessionsResponseSchema,
  type Session,
} from "../../../runner_gateway/dispatcher/api/session/session_dispatcher_pb";
import { displayableError, fetchJson, fetchText, HttpError } from "../client";

const PRECONDITION_FAILURE = "type.googleapis.com/google.rpc.PreconditionFailure";

/** The kinds of object whose reference to an identity blocks its delete. */
const REFERENCE_KINDS = ["PolicyBinding", "WorkloadCredential"] as const;

/** An object naming an identity: a PolicyBinding by name, a WorkloadCredential as `namespace/name`. */
export interface Reference {
  kind: (typeof REFERENCE_KINDS)[number];
  name: string;
}

/** How a delete ended: done, refused for what still names the identity, or failed. */
export type DeleteOutcome =
  { case: "deleted" } | { case: "referenced"; references: Reference[] } | { case: "failed"; error: string };

function isReferenceKind(type: unknown): type is Reference["kind"] {
  return REFERENCE_KINDS.some((kind) => kind === type);
}

/** The references a gRPC-Gateway error body's `PreconditionFailure` names, if it carries one. */
function references(body: unknown): Reference[] | null {
  if (typeof body !== "object" || body === null || !("details" in body) || !Array.isArray(body.details)) return null;
  const failure: unknown = body.details.find(
    (detail: unknown) =>
      typeof detail === "object" && detail !== null && "@type" in detail && detail["@type"] === PRECONDITION_FAILURE
  );
  if (
    typeof failure !== "object" ||
    failure === null ||
    !("violations" in failure) ||
    !Array.isArray(failure.violations)
  )
    return null;
  return failure.violations.flatMap((violation: unknown): Reference[] =>
    typeof violation === "object" &&
    violation !== null &&
    "type" in violation &&
    isReferenceKind(violation.type) &&
    "subject" in violation &&
    typeof violation.subject === "string"
      ? [{ kind: violation.type, name: violation.subject }]
      : []
  );
}

/** Deletes exactly this incarnation; the Policy Controller rechecks Flux ownership and references. */
export async function deleteIdentity(identity: Identity): Promise<DeleteOutcome> {
  const { namespace, name, uid } = identity.identity ?? {};
  if (!namespace || !name || !uid) return { case: "failed", error: "Identity has no namespace, name and UID" };
  try {
    await fetchText(
      `/upstream/policy/v1/identities/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}?${new URLSearchParams({ uid })}`,
      { method: "DELETE" }
    );
  } catch (error) {
    if (!(error instanceof HttpError)) return { case: "failed", error: displayableError(error) };
    const named = references(error.body);
    if (named?.length) return { case: "referenced", references: named };
    const body = error.body;
    const message = typeof body === "object" && body !== null && "message" in body ? body.message : null;
    return { case: "failed", error: typeof message === "string" ? message : error.message };
  }
  return { case: "deleted" };
}

/** Read every Dispatcher page before claiming that no other Session names an Identity. */
export async function listIdentitySessions(): Promise<Session[]> {
  const sessions: Session[] = [];
  const visited = new Set<string>();
  let token = "";
  do {
    const query = new URLSearchParams({ limit: "100" });
    if (token) query.set("pageToken", token);
    const response = fromJson(
      ListSessionsResponseSchema,
      await fetchJson(`/upstream/dispatcher/v1/sessions?${query}`, { headers: { Accept: "application/json" } })
    );
    sessions.push(...response.sessions);
    token = response.nextPageToken;
    if (token && visited.has(token)) throw new Error("Dispatcher repeated a Session page token");
    visited.add(token);
  } while (token);
  return sessions;
}

function policyPost(body: Record<string, JsonValue>): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

/** Creates an Identity in the policy namespace; its creator deletes it. */
export async function createIdentity(name: string, description: string): Promise<Identity> {
  const response = fromJson(
    CreateIdentityResponseSchema,
    await fetchJson("/upstream/policy/v1/identities", policyPost({ name, description }))
  );
  if (!response.identity?.identity?.uid || response.identity.identity.name !== name)
    throw new Error("PolicyService returned an invalid created Identity");
  return response.identity;
}

/** Binds PolicySets to exactly this Identity incarnation, in a binding the Identity owns. */
export async function grantIdentityPolicyBinding(identity: Identity, policySets: string[]): Promise<void> {
  const { name, uid } = identity.identity ?? {};
  if (!name || !uid) throw new Error("Identity has no name and UID");
  fromJson(
    GrantIdentityPolicyBindingResponseSchema,
    await fetchJson(
      `/upstream/policy/v1/identities/${encodeURIComponent(name)}/policy-bindings`,
      policyPost({ identityUid: uid, policySets })
    )
  );
}
