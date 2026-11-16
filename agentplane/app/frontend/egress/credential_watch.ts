import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import { WatchCredentialsResponseSchema } from "../../../egress/api/admin/egress_admin_pb";
import type { Credential } from "../../../egress/api/rules/egress_rules_pb";
import { followWatch } from "../watch_stream";

/** Public metadata is independent of the policy resources and may be unavailable. */
export function useCredentialMetadata(): Map<string, Credential> | null {
  const [credentials, setCredentials] = useState<Map<string, Credential> | null>(null);
  useEffect(() => {
    let active = true;
    let current: Map<string, Credential> | null = null;
    let pending: Map<string, Credential> | null = new Map();
    const clear = (): void => {
      current = null;
      pending = new Map();
      if (active) setCredentials(null);
    };
    const stop = followWatch("/upstream/egress/v1/credentials:watch", {
      onOpen: clear,
      onConnection: (connection) => {
        if (connection.phase !== "live") clear();
      },
      onFrame: (json) => {
        if (!active) return;
        const frame = fromJson(WatchCredentialsResponseSchema, json as JsonValue).frame;
        switch (frame.case) {
          case "snapshot": {
            if (pending === null) throw new Error("Unexpected credential snapshot");
            for (const credential of frame.value.credentials) {
              if (!credential.name || !credential.uid || pending.has(credential.name))
                throw new Error("Invalid credential snapshot");
              pending.set(credential.name, credential);
            }
            return;
          }
          case "snapshotComplete":
            if (pending === null) throw new Error("Unexpected credential snapshot completion");
            current = pending;
            pending = null;
            break;
          case "upsert":
            if (!current || !frame.value.name || !frame.value.uid) throw new Error("Invalid credential upsert");
            current = new Map(current).set(frame.value.name, frame.value);
            break;
          case "removed": {
            if (!current || !frame.value.name || !frame.value.uid) throw new Error("Invalid credential removal");
            const next = new Map(current);
            if (next.get(frame.value.name)?.uid === frame.value.uid) next.delete(frame.value.name);
            current = next;
            break;
          }
          default:
            throw new Error("Empty credential watch frame");
        }
        setCredentials(current);
      },
    });
    return () => {
      active = false;
      stop();
    };
  }, []);
  return credentials;
}
