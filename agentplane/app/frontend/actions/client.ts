import type { components } from "../api/schema";
import { api, httpError } from "../client";

export type ActionPolicyView = components["schemas"]["ActionPolicyView"];
export type ActionPolicyUnavailable = components["schemas"]["ActionPolicyUnavailable"];
export type ActionPolicyBindingView = components["schemas"]["ActionPolicyBindingView"];
export type ActionPolicySetView = components["schemas"]["ActionPolicySetView"];
export type EffectivePolicyView = components["schemas"]["EffectivePolicyView"];
export type ReadyConditionView = components["schemas"]["ReadyConditionView"];
export type ActionRequestView = components["schemas"]["ActionRequestView"];
export type ActionState = components["schemas"]["ActionState"];
export type Verdict = components["schemas"]["Verdict"];

export type ActionGroupView = components["schemas"]["ActionGroupView"];

export interface ActionGroupService {
  list(): Promise<ActionGroupView[]>;
}

export const actionGroupService: ActionGroupService = {
  async list() {
    const { data, error, response } = await api.GET("/action-groups");
    if (error) throw new Error(httpError(response, error));
    return data;
  },
};

export interface ActionService {
  list(): Promise<ActionRequestView[]>;
  decide(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView>;
}

export const actionService: ActionService = {
  async list(): Promise<ActionRequestView[]> {
    const { data, error, response } = await api.GET("/actions");
    if (error) throw new Error(httpError(response, error));
    return data;
  },

  async decide(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView> {
    const { data, error, response } = await api.POST("/actions/{request_id}/decision", {
      params: { path: { request_id: request.id } },
      body: { verdict, expected_version: request.version, idempotency_key: crypto.randomUUID(), decision_note: null },
    });
    if (error) throw new Error(httpError(response, error));
    return data;
  },
};
