import { toJson } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";
import { Decision_Outcome, Decision_Reason, type Decision } from "../../../egress/egress_admin_pb";

/** An unrecognized outcome is neither an admission nor a refusal. */
export function decisionOutcome(decision: Decision): "allow" | "deny" | "unknown" {
  if (decision.outcome === Decision_Outcome.ALLOW) return "allow";
  if (decision.outcome === Decision_Outcome.DENY) return "deny";
  return "unknown";
}

/** A refusal as the proxy's `x-agentplane-egress` header spells it; "unknown" for one this bundle does not name. */
export function decisionReason(decision: Decision): string | null {
  if (decision.reason === undefined) return null;
  const name: string | undefined = Decision_Reason[decision.reason];
  if (name === undefined || decision.reason === Decision_Reason.UNSPECIFIED) return "unknown";
  return name.toLowerCase().replaceAll("_", "-");
}

export function decisionAt(decision: Decision): string {
  return decision.at ? (toJson(TimestampSchema, decision.at) as string) : "0001-01-01T00:00:00Z";
}

/** The requests of one outcome to one destination by one method, decided the same way. */
export interface DecisionGroup {
  /** Names the group in a URL; stable across refreshes while the group exists. */
  key: string;
  method: string;
  host: string;
  port: number;
  /** The policy that admitted them, or the proxy's refusal; the two are never both set. */
  why: string | null;
  firstAt: string;
  lastAt: string;
  /** Whether any of them had a credential substituted. */
  substituted: boolean;
  /** Newest first. */
  members: Decision[];
}

function why(decision: Decision): string | null {
  return decisionOutcome(decision) === "allow" ? (decision.policy ?? null) : decisionReason(decision);
}

/** Groups a log served oldest first. Refusals come first, so one cannot scroll away under allowed
 * traffic; within each outcome the group heard from most recently is first. */
export function groupDecisions(decisions: Decision[]): DecisionGroup[] {
  const groups = new Map<string, DecisionGroup>();
  for (const decision of decisions) {
    const key = [
      decisionOutcome(decision),
      decision.method,
      `${decision.host}:${decision.port}`,
      why(decision) ?? "",
    ].join("|");
    const group = groups.get(key);
    if (group === undefined) {
      groups.set(key, {
        key,
        method: decision.method,
        host: decision.host,
        port: decision.port,
        why: why(decision),
        firstAt: decisionAt(decision),
        lastAt: decisionAt(decision),
        substituted: decision.substituted,
        members: [decision],
      });
    } else {
      group.lastAt = decisionAt(decision);
      group.substituted ||= decision.substituted;
      group.members.unshift(decision);
    }
  }
  return [...groups.values()].sort(
    (a, b) =>
      Number(decisionOutcome(a.members[0]) === "allow") - Number(decisionOutcome(b.members[0]) === "allow") ||
      b.lastAt.localeCompare(a.lastAt)
  );
}
