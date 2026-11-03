/** Test-only builder for the generated egress decision carried by the browser watch. */
import { create, fromJson } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";

import { Decision_Outcome, DecisionSchema, type Decision } from "../../../egress/egress_admin_pb";

export type DecisionFixture = Omit<Partial<Decision>, "at" | "host"> & { at: string; host: string };

export function decisionFixture({ at, ...fields }: DecisionFixture): Decision {
  return create(DecisionSchema, {
    method: "GET",
    port: 443,
    outcome: Decision_Outcome.ALLOW,
    substituted: false,
    ...fields,
    eventId: fields.eventId ?? `${at}/${fields.method ?? "GET"}/${fields.host}`,
    at: fromJson(TimestampSchema, at),
  });
}
