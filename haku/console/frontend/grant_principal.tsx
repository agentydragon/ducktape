import type { JSX } from "react";
import { AgentName } from "./agent_names";
import type { GrantPrincipal } from "./client";

export type { GrantPrincipal } from "./client";

/** Render a grant principal by Agent name, retaining IDs only where they are the useful identity. */
export function GrantPrincipalLabel({ principal }: { principal: GrantPrincipal }): import("react").JSX.Element {
  switch (principal.kind) {
    case "agent":
      return (
        <>
          Agent <AgentName agentId={principal.agent_id} />
        </>
      );
    case "access_profile":
      return <>Access profile {principal.access_profile_id}</>;
  }
}
