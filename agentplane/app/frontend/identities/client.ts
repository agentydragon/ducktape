import type { Identity } from "../../../policy/v1/policy_service_pb";
import { displayableError, fetchWithLogin } from "../client";

const PRECONDITION_FAILURE = "type.googleapis.com/google.rpc.PreconditionFailure";

/** How a delete ended: done, refused for the bindings that still name the identity, or failed. */
export type DeleteOutcome = { case: "deleted" } | { case: "referenced"; bindings: string[] } | { case: "failed"; error: string };

/** The PolicyBindings a gRPC-Gateway error body's `PreconditionFailure` names, if it carries one. */
function referencingBindings(body: unknown): string[] | null {
  if (typeof body !== "object" || body === null || !("details" in body) || !Array.isArray(body.details)) return null;
  const failure: unknown = body.details.find(
    (detail: unknown) => typeof detail === "object" && detail !== null && "@type" in detail && detail["@type"] === PRECONDITION_FAILURE
  );
  if (typeof failure !== "object" || failure === null || !("violations" in failure) || !Array.isArray(failure.violations))
    return null;
  return failure.violations.flatMap((violation: unknown) =>
    typeof violation === "object" &&
    violation !== null &&
    "type" in violation &&
    violation.type === "PolicyBinding" &&
    "subject" in violation &&
    typeof violation.subject === "string"
      ? [violation.subject]
      : []
  );
}

/** Deletes exactly this incarnation; the Policy Controller rechecks Flux ownership and references. */
export async function deleteIdentity(identity: Identity): Promise<DeleteOutcome> {
  const { namespace, name, uid } = identity.identity ?? {};
  if (!namespace || !name || !uid) return { case: "failed", error: "Identity has no namespace, name and UID" };
  const response = await fetchWithLogin(
    `/upstream/policy/v1/identities/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}?${new URLSearchParams({ uid })}`,
    { method: "DELETE" }
  );
  if (response.ok) return { case: "deleted" };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { case: "failed", error: `Policy Service returned HTTP ${response.status}` };
  }
  const bindings = referencingBindings(body);
  if (bindings?.length) return { case: "referenced", bindings };
  const message = typeof body === "object" && body !== null && "message" in body ? body.message : body;
  return { case: "failed", error: displayableError(message) };
}
