import { fromJson } from "@bufbuild/protobuf";
import { useEffect, useState } from "react";

import { WatchCredentialsResponseSchema, type CredentialSnapshot } from "../../../egress/api/admin/egress_admin_pb";
import type { Credential } from "../../../egress/api/rules/egress_rules_pb";
import { WatchCollection } from "../transport/watch_collection";
import { followWatch } from "../transport/watch_stream";

/** Public metadata is independent of the policy resources and may be unavailable. */
export function useCredentialMetadata(): ReadonlyMap<string, Credential> | null {
  const [credentials, setCredentials] = useState<ReadonlyMap<string, Credential> | null>(null);
  useEffect(() => {
    let active = true;
    const credentialKey = (identity: Pick<Credential, "name" | "uid">): string | undefined =>
      identity.uid ? identity.name : undefined;
    const collection = new WatchCollection<Credential, Pick<Credential, "name" | "uid">>({
      kind: "Credential",
      key: credentialKey,
      removal: { key: credentialKey, matches: (held, identity) => held.uid === identity.uid },
    });
    const clear = (): void => {
      collection.beginSnapshot();
      if (active) setCredentials(null);
    };
    const stop = followWatch("/upstream/egress/v1/credentials:watch", {
      onOpen: clear,
      onConnection: (connection) => {
        if (connection.phase !== "live") clear();
      },
      onFrame: (json) => {
        if (!active) return;
        const { frame } = fromJson(WatchCredentialsResponseSchema, json);
        if (collection.apply<CredentialSnapshot>(frame, (chunk) => chunk.credentials))
          setCredentials(collection.snapshotByKey());
      },
    });
    return () => {
      active = false;
      stop();
    };
  }, []);
  return credentials;
}
