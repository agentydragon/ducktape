import type { components } from "../api/schema";
import type { CallerServiceAccount } from "../client";

export type ConnectionGrant = {
  id: string;
  connection_id: string;
  revision: number;
  caller: CallerServiceAccount;
  issuer: string;
  client_id: string;
  status: "pending" | "active" | "revoked";
  created_at: string;
  activated_at: string | null;
  revoked_at: string | null;
};

/** The ConnectionService proto adapted to the existing UI's snake_case view. */
export type Connection = {
  id: string;
  display_name: string;
  /** Protobuf int64 is kept as a decimal string so updates do not lose precision. */
  version: string;
  created_at: string;
  updated_at: string;
  grants: ConnectionGrant[];
  bound_caller: CallerServiceAccount | null;
};

export type CallerGrantView = components["schemas"]["CallerGrantView"];
export type CallerGrantReader = (account: CallerServiceAccount, signal: AbortSignal) => Promise<CallerGrantView>;
