import type { components } from "./openapi_schema";

export type EventRecord = Pick<
  components["schemas"]["Event"],
  "ordinal" | "timestamp" | "kind" | "parsed" | "raw_base64"
> & {
  parse_error?: components["schemas"]["Event"]["parse_error"];
};

export type SessionPage<T> = Omit<components["schemas"]["SessionPage"], "data"> & { data: T[] };
export type EventPage<T> = Omit<components["schemas"]["EventPage"], "data"> & { data: T[] };

export type SessionQuery = {
  machineId?: string;
  harness?: components["schemas"]["Harness"];
  cursor?: string;
  limit?: number;
  parentSessionId?: string;
  kinds?: components["schemas"]["SessionKind"][];
};
