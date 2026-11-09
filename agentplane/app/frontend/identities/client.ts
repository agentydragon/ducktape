import type { Identity } from "../../../policy/v1/policy_service_pb";
import { displayableError, fetchText, HttpError } from "../client";

const PRECONDITION_FAILURE = "type.googleapis.com/google.rpc.PreconditionFailure";

/** How a delete ended: done, refused for the bindings that still name the identity, or failed. */
export type DeleteOutcome =
  { case: "deleted" } | { case: "referenced"; bindings: string[] } | { case: "failed"; error: string };

/** The PolicyBindings a gRPC-Gateway error body's `PreconditionFailure` names, if it carries one. */
function referencingBindings(body: unknown): string[] | null {
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
  try {
    await fetchText(
      `/upstream/policy/v1/identities/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}?${new URLSearchParams({ uid })}`,
      { method: "DELETE" }
    );
  } catch (error) {
    if (!(error instanceof HttpError)) return { case: "failed", error: displayableError(error) };
    const bindings = referencingBindings(error.body);
    if (bindings?.length) return { case: "referenced", bindings };
    const body = error.body;
    const message = typeof body === "object" && body !== null && "message" in body ? body.message : null;
    return { case: "failed", error: typeof message === "string" ? message : error.message };
  }
  return { case: "deleted" };
}
