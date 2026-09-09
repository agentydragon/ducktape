import { api, displayableError } from "./client";
import type { components } from "./api/schema";

export type ConsentPreview = components["schemas"]["ConsentPreview"];
export type ConsentDecision = components["schemas"]["ConsentAllow"] | components["schemas"]["ConsentDeny"];
export type ConsentResult = components["schemas"]["EnrollmentDecisionResult"];

export interface ConsentService {
  preview(handle: string): Promise<ConsentPreview>;
  decide(handle: string, decision: ConsentDecision): Promise<ConsentResult>;
}

export const consentService: ConsentService = {
  async preview(handle) {
    const { data, error } = await api.POST("/connection-enrollments/{handle}/preview", {
      params: { path: { handle } },
    });
    if (error) throw new Error(displayableError(error));
    return data;
  },
  async decide(handle, decision) {
    const { data, error } = await api.POST("/connection-enrollments/{handle}/decision", {
      params: { path: { handle } },
      body: decision,
    });
    if (error) throw new Error(displayableError(error));
    return data;
  },
};
