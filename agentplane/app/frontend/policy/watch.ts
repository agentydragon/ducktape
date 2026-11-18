import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import {
  WatchIdentitiesResponseSchema,
  WatchPolicyBindingsResponseSchema,
  WatchPolicySetsResponseSchema,
  type Identity,
  type IdentityChunk,
  type PolicyBinding,
  type PolicyBindingChunk,
  type PolicyResourceIdentity,
  type PolicySet,
  type PolicySetChunk,
  type WatchPolicyBindingsRequest,
} from "../../../policy/v1/policy_service_pb";
import { WatchCollection, type CollectionFrame } from "../transport/watch_collection";
import { followWatch } from "../transport/watch_stream";

/** The two independently owned resource collections shown together by the policy UI. */
export type PolicyResources = { bindings: PolicyBinding[]; sets: PolicySet[] };

/** Which bindings to watch: the generated request's selector, never its unset case. */
export type BindingSelector = Exclude<WatchPolicyBindingsRequest["selector"], { case: undefined }>;

/** A collection's state: its items once a snapshot completed and the stream is live, else null. */
type CollectionState<T> = { items: T[] | null; error: string | null };

/**
 * Follows one PolicyService resource watch: a snapshot committed on completion, then upserts and
 * UID-fenced removals. A reconnect discards the old snapshot until the new one completes.
 */
function followCollection<T extends { identity?: PolicyResourceIdentity }, S>(
  url: string,
  kind: string,
  decode: (json: JsonValue) => CollectionFrame<T, S, PolicyResourceIdentity>,
  snapshotItems: (chunk: S) => T[],
  onChange: (state: CollectionState<T>) => void
): () => void {
  const collection = new WatchCollection<T, PolicyResourceIdentity>(
    kind,
    (item) => item.identity,
    (identity) => identity.name
  );
  return followWatch(url, {
    onOpen: () => {
      collection.beginSnapshot();
      onChange({ items: null, error: null });
    },
    onConnection: (connection) => {
      if (connection.phase !== "live") {
        const error =
          connection.phase === "reconnecting" ? (connection.lastError ?? `${kind} watch disconnected`) : null;
        onChange({ items: null, error });
      }
    },
    onFrame: (json) => {
      const items = collection.apply(decode(json), snapshotItems);
      if (items !== null) onChange({ items, error: null });
    },
  });
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
    const stopBindings = followCollection<PolicyBinding, PolicyBindingChunk>(
      `${BASE}/policy-bindings:watch?${query}`,
      "PolicyBinding",
      (json) => fromJson(WatchPolicyBindingsResponseSchema, json).frame,
      (chunk) => chunk.bindings,
      (next) => {
        bindings = next;
        publish();
      }
    );
    const stopSets = followCollection<PolicySet, PolicySetChunk>(
      `${BASE}/policy-sets:watch`,
      "PolicySet",
      (json) => fromJson(WatchPolicySetsResponseSchema, json).frame,
      (chunk) => chunk.policySets,
      (next) => {
        sets = next;
        publish();
      }
    );
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
    const stop = followCollection<Identity, IdentityChunk>(
      `${BASE}/identities:watch`,
      "Identity",
      (json) => fromJson(WatchIdentitiesResponseSchema, json).frame,
      (chunk) => chunk.identities,
      (next) => {
        if (active) setState(next);
      }
    );
    return () => {
      active = false;
      stop();
    };
  }, []);
  return { identities: state.items, error: state.error };
}
