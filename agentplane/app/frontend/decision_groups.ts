import type { Decision } from "./client";

/** The requests of one outcome to one destination by one method, decided the same way. */
export interface DecisionGroup {
  /** Names the group in a URL; stable across refreshes while the group exists. */
  key: string;
  outcome: Decision["outcome"];
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
  return decision.outcome === "allow" ? decision.policy : decision.reason;
}

/** Groups a log served oldest first. Refusals come first, so one cannot scroll away under allowed
 * traffic; within each outcome the group heard from most recently is first. */
export function groupDecisions(decisions: Decision[]): DecisionGroup[] {
  const groups = new Map<string, DecisionGroup>();
  for (const decision of decisions) {
    const key = [decision.outcome, decision.method, `${decision.host}:${decision.port}`, why(decision) ?? ""].join("|");
    const group = groups.get(key);
    if (group === undefined) {
      groups.set(key, {
        key,
        outcome: decision.outcome,
        method: decision.method,
        host: decision.host,
        port: decision.port,
        why: why(decision),
        firstAt: decision.at,
        lastAt: decision.at,
        substituted: decision.substituted,
        members: [decision],
      });
    } else {
      group.lastAt = decision.at;
      group.substituted ||= decision.substituted;
      group.members.unshift(decision);
    }
  }
  return [...groups.values()].sort(
    (a, b) => Number(b.outcome === "deny") - Number(a.outcome === "deny") || b.lastAt.localeCompare(a.lastAt)
  );
}
