import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import {
  WatchIdentitiesResponseSchema,
  WatchPolicyBindingsResponseSchema,
  WatchPolicySetsResponseSchema,
  type Identity,
  type PolicyBinding,
  type PolicyResourceIdentity,
  type PolicySet,
  type WatchPolicyBindingsRequest,
} from "../../../policy/v1/policy_service_pb";
import { followWatch } from "../watch_stream";

/** The two independently owned resource collections shown together by the policy UI. */
export type PolicyResources = { bindings: PolicyBinding[]; sets: PolicySet[] };

/** Which bindings to watch: the generated request's selector, never its unset case. */
export type BindingSelector = Exclude<WatchPolicyBindingsRequest["selector"], { case: undefined }>;

/** One PolicyService watch frame, whatever kind of resource the collection holds. */
type CollectionFrame<T> =
  | { case: "snapshot"; items: T[] }
  | { case: "snapshotComplete" }
  | { case: "upsert"; item: T }
  | { case: "removed"; removed: PolicyResourceIdentity }
  | { case: undefined };

/** A collection's state: its items once a snapshot completed and the stream is live, else null. */
type CollectionState<T> = { items: T[] | null; error: string | null };

/**
 * Follows one PolicyService resource watch: a snapshot committed on completion, then upserts and
 * UID-fenced removals. A reconnect discards the old snapshot until the new one completes.
 */
function followCollection<T extends { identity?: PolicyResourceIdentity }>(
  url: string,
  kind: string,
  decode: (json: JsonValue) => CollectionFrame<T>,
  onChange: (state: CollectionState<T>) => void
): () => void {
  let items = new Map<string, T>();
  let pending: Map<string, T> | null = new Map();
  let ready = false;
  let error: string | null = null;
  const publish = (): void => onChange({ items: ready ? [...items.values()] : null, error });
  return followWatch(url, {
    onOpen: () => {
      pending = new Map();
      ready = false;
      error = null;
      publish();
    },
    onConnection: (connection) => {
      if (connection.phase !== "live") {
        ready = false;
        error = connection.phase === "reconnecting" ? (connection.lastError ?? `${kind} watch disconnected`) : null;
        publish();
      }
    },
    onFrame: (json) => {
      const frame = decode(json as JsonValue);
      switch (frame.case) {
        case "snapshot":
          if (pending === null) throw new Error(`Unexpected ${kind} snapshot`);
          for (const item of frame.items) {
            if (!item.identity?.name || !item.identity.uid) throw new Error(`${kind} missing identity`);
            pending.set(item.identity.name, item);
          }
          break;
        case "snapshotComplete":
          if (pending === null) throw new Error(`Unexpected ${kind} snapshot completion`);
          items = pending;
          pending = null;
          ready = true;
          publish();
          break;
        case "upsert":
          if (pending !== null || !frame.item.identity?.name || !frame.item.identity.uid)
            throw new Error(`Invalid ${kind} upsert`);
          items.set(frame.item.identity.name, frame.item);
          publish();
          break;
        case "removed":
          if (pending !== null) throw new Error(`${kind} removal before snapshot completion`);
          if (items.get(frame.removed.name)?.identity?.uid === frame.removed.uid) items.delete(frame.removed.name);
          publish();
          break;
        default:
          throw new Error(`Empty ${kind} watch frame`);
      }
    },
  });
}

function bindingFrame(json: JsonValue): CollectionFrame<PolicyBinding> {
  const frame = fromJson(WatchPolicyBindingsResponseSchema, json).frame;
  switch (frame.case) {
    case "snapshot":
      return { case: "snapshot", items: frame.value.bindings };
    case "snapshotComplete":
      return { case: "snapshotComplete" };
    case "upsert":
      return { case: "upsert", item: frame.value };
    case "removed":
      return { case: "removed", removed: frame.value };
    default:
      return { case: undefined };
  }
}

function setFrame(json: JsonValue): CollectionFrame<PolicySet> {
  const frame = fromJson(WatchPolicySetsResponseSchema, json).frame;
  switch (frame.case) {
    case "snapshot":
      return { case: "snapshot", items: frame.value.policySets };
    case "snapshotComplete":
      return { case: "snapshotComplete" };
    case "upsert":
      return { case: "upsert", item: frame.value };
    case "removed":
      return { case: "removed", removed: frame.value };
    default:
      return { case: undefined };
  }
}

function identityFrame(json: JsonValue): CollectionFrame<Identity> {
  const frame = fromJson(WatchIdentitiesResponseSchema, json).frame;
  switch (frame.case) {
    case "snapshot":
      return { case: "snapshot", items: frame.value.identities };
    case "snapshotComplete":
      return { case: "snapshotComplete" };
    case "upsert":
      return { case: "upsert", item: frame.value };
    case "removed":
      return { case: "removed", removed: frame.value };
    default:
      return { case: undefined };
  }
}

/** The binding watch's query parameters, as gRPC-Gateway reads the request's selector. */
function selectorQuery(selector: BindingSelector): URLSearchParams {
  switch (selector.case) {
    case "subject":
      return new URLSearchParams({
        "subject.namespace": selector.value.namespace,
        "subject.name": selector.value.name,
      });
    case "identity":
      return new URLSearchParams({ identity: selector.value });
    case "everyIdentity":
      return new URLSearchParams({ everyIdentity: "true" });
  }
}

const BASE = "/upstream/policy/v1";

type State = { key: string; resources: PolicyResources | null; error: string | null };

export function usePolicyResources(
  selector: BindingSelector | undefined,
  fence: string | undefined
): { resources: PolicyResources | null; error: string | null } {
  const query = selector ? selectorQuery(selector).toString() : null;
  const key = query === null ? null : `${query}\0${fence ?? ""}`;
  const [state, setState] = useState<State | null>(null);
  useEffect(() => {
    if (query === null || key === null) return;
    let active = true;
    let bindings: CollectionState<PolicyBinding> = { items: null, error: null };
    let sets: CollectionState<PolicySet> = { items: null, error: null };
    const publish = (): void => {
      if (!active) return;
      setState({
        key,
        resources: bindings.items && sets.items ? { bindings: bindings.items, sets: sets.items } : null,
        error: bindings.error ?? sets.error,
      });
    };
    publish();
    const stopBindings = followCollection(
      `${BASE}/policy-bindings:watch?${query}`,
      "PolicyBinding",
      bindingFrame,
      (next) => {
        bindings = next;
        publish();
      }
    );
    const stopSets = followCollection(`${BASE}/policy-sets:watch`, "PolicySet", setFrame, (next) => {
      sets = next;
      publish();
    });
    return () => {
      active = false;
      stopBindings();
      stopSets();
    };
  }, [query, fence, key]);
  return state?.key === key ? state : { resources: null, error: null };
}

/** Every Identity in the policy namespace, once the watch's snapshot completed. */
export function useIdentities(): { identities: Identity[] | null; error: string | null } {
  const [state, setState] = useState<CollectionState<Identity>>({ items: null, error: null });
  useEffect(() => {
    let active = true;
    const stop = followCollection(`${BASE}/identities:watch`, "Identity", identityFrame, (next) => {
      if (active) setState(next);
    });
    return () => {
      active = false;
      stop();
    };
  }, []);
  return { identities: state.items, error: state.error };
}
