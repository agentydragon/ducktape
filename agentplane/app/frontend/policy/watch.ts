import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import {
  WatchPolicyBindingsResponseSchema,
  WatchPolicySetsResponseSchema,
  type PolicyBinding,
  type PolicySet,
} from "../../../policy/v1/policy_service_pb";
import type { ServiceAccount } from "../../../proto/identity_pb";
import { followWatch } from "../watch_stream";

/** The two independently owned resource collections shown together by the policy UI. */
export type PolicyResources = { bindings: PolicyBinding[]; sets: PolicySet[] };

type State = { key: string; resources: PolicyResources | null; error: string | null };

export function usePolicyResources(
  subject: Pick<ServiceAccount, "namespace" | "name"> | undefined,
  fence: string | undefined
): { resources: PolicyResources | null; error: string | null } {
  const namespace = subject?.namespace;
  const name = subject?.name;
  const key = namespace && name ? `${namespace}\0${name}\0${fence ?? ""}` : null;
  const [state, setState] = useState<State | null>(null);
  useEffect(() => {
    if (!namespace || !name || key === null) return;
    let active = true;
    let bindings = new Map<string, PolicyBinding>();
    let sets = new Map<string, PolicySet>();
    let bindingPending: Map<string, PolicyBinding> | null = new Map();
    let setPending: Map<string, PolicySet> | null = new Map();
    let bindingReady = false;
    let setReady = false;
    let bindingError: string | null = null;
    let setError: string | null = null;
    const publish = (): void => {
      if (!active) return;
      setState({
        key,
        resources: bindingReady && setReady ? { bindings: [...bindings.values()], sets: [...sets.values()] } : null,
        error: bindingError ?? setError,
      });
    };
    publish();
    const base = "/upstream/policy/v1";
    const query = new URLSearchParams({ "subject.namespace": namespace, "subject.name": name });
    const stopBindings = followWatch(`${base}/policy-bindings:watch?${query}`, {
      onOpen: () => {
        bindingPending = new Map();
        bindingReady = false;
        bindingError = null;
        publish();
      },
      onConnection: (connection) => {
        if (connection.phase !== "live") {
          bindingReady = false;
          bindingError =
            connection.phase === "reconnecting" ? (connection.lastError ?? "Policy binding watch disconnected") : null;
          publish();
        }
      },
      onFrame: (json) => {
        const frame = fromJson(WatchPolicyBindingsResponseSchema, json as JsonValue).frame;
        switch (frame.case) {
          case "snapshot":
            if (bindingPending === null) throw new Error("Unexpected PolicyBinding snapshot");
            for (const item of frame.value.bindings) {
              if (!item.identity?.name || !item.identity.uid) throw new Error("PolicyBinding missing identity");
              bindingPending.set(item.identity.name, item);
            }
            break;
          case "snapshotComplete":
            if (bindingPending === null) throw new Error("Unexpected PolicyBinding snapshot completion");
            bindings = bindingPending;
            bindingPending = null;
            bindingReady = true;
            publish();
            break;
          case "upsert":
            if (bindingPending !== null || !frame.value.identity?.name || !frame.value.identity.uid)
              throw new Error("Invalid PolicyBinding upsert");
            bindings.set(frame.value.identity.name, frame.value);
            publish();
            break;
          case "removed":
            if (bindingPending !== null) throw new Error("PolicyBinding removal before snapshot completion");
            if (bindings.get(frame.value.name)?.identity?.uid === frame.value.uid) bindings.delete(frame.value.name);
            publish();
            break;
          default:
            throw new Error("Empty PolicyBinding watch frame");
        }
      },
    });
    const stopSets = followWatch(`${base}/policy-sets:watch`, {
      onOpen: () => {
        setPending = new Map();
        setReady = false;
        setError = null;
        publish();
      },
      onConnection: (connection) => {
        if (connection.phase !== "live") {
          setReady = false;
          setError =
            connection.phase === "reconnecting" ? (connection.lastError ?? "PolicySet watch disconnected") : null;
          publish();
        }
      },
      onFrame: (json) => {
        const frame = fromJson(WatchPolicySetsResponseSchema, json as JsonValue).frame;
        switch (frame.case) {
          case "snapshot":
            if (setPending === null) throw new Error("Unexpected PolicySet snapshot");
            for (const item of frame.value.policySets) {
              if (!item.identity?.name || !item.identity.uid) throw new Error("PolicySet missing identity");
              setPending.set(item.identity.name, item);
            }
            break;
          case "snapshotComplete":
            if (setPending === null) throw new Error("Unexpected PolicySet snapshot completion");
            sets = setPending;
            setPending = null;
            setReady = true;
            publish();
            break;
          case "upsert":
            if (setPending !== null || !frame.value.identity?.name || !frame.value.identity.uid)
              throw new Error("Invalid PolicySet upsert");
            sets.set(frame.value.identity.name, frame.value);
            publish();
            break;
          case "removed":
            if (setPending !== null) throw new Error("PolicySet removal before snapshot completion");
            if (sets.get(frame.value.name)?.identity?.uid === frame.value.uid) sets.delete(frame.value.name);
            publish();
            break;
          default:
            throw new Error("Empty PolicySet watch frame");
        }
      },
    });
    return () => {
      active = false;
      stopBindings();
      stopSets();
    };
  }, [namespace, name, fence, key]);
  return state?.key === key ? state : { resources: null, error: null };
}
