import type { components } from "../api/schema";
import { api, ok } from "../client";
import type { ActionRequestView } from "./types";

export type { ActionRequestView } from "./types";

export type BoundPolicyView = components["schemas"]["BoundPolicyView"];
export type ActionPolicyView = components["schemas"]["ActionPolicyView"];
export type ActionPolicyUnavailable = components["schemas"]["ActionPolicyUnavailable"];
export type ActionPolicyBindingView = components["schemas"]["ActionPolicyBindingView"];
export type ActionPolicySetView = components["schemas"]["ActionPolicySetView"];
export type EffectivePolicyView = components["schemas"]["EffectivePolicyView"];
export type ReadyConditionView = components["schemas"]["ReadyConditionView"];
export type ActionHistoryPage = components["schemas"]["ActionHistoryPage"];
export type ActionState = components["schemas"]["ActionState"];
export type Verdict = components["schemas"]["Verdict"];

export type ActionGroupView = components["schemas"]["ActionGroupView"];

export async function listActionGroups(): Promise<ActionGroupView[]> {
  return ok(await api.GET("/action-groups"));
}

export async function getAction(requestId: string): Promise<ActionRequestView> {
  return ok(await api.GET("/actions/{request_id}", { params: { path: { request_id: requestId } } }));
}

export async function actionHistory(cursor?: string): Promise<ActionHistoryPage> {
  return ok(await api.GET("/actions/history", { params: { query: { cursor, limit: 50 } } }));
}

export async function decideAction(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView> {
  return ok(
    await api.POST("/actions/{request_id}/decision", {
      params: { path: { request_id: request.id } },
      body: { verdict, expected_version: request.version, idempotency_key: crypto.randomUUID(), decision_note: null },
    })
  );
}
