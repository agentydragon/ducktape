import { api, ok } from "./client";
import type { components } from "./api/schema";

export type ConsentPreview = components["schemas"]["ConsentPreview"];
export type ConsentDecision = components["schemas"]["ConsentAllow"] | components["schemas"]["ConsentDeny"];
export type ConsentResult = components["schemas"]["EnrollmentDecisionResult"];

export async function previewConsent(handle: string): Promise<ConsentPreview> {
  return ok(await api.POST("/connection-enrollments/{handle}/preview", { params: { path: { handle } } }));
}

export async function decideConsent(handle: string, decision: ConsentDecision): Promise<ConsentResult> {
  return ok(
    await api.POST("/connection-enrollments/{handle}/decision", { params: { path: { handle } }, body: decision })
  );
}
