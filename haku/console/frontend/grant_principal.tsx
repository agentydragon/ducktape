import { AgentName, useToolCallAgent } from "./agent_names";
import type { AgentGrant } from "./client";

export type GrantPrincipal = AgentGrant["grant"]["subject"];

/** Render a grant principal by Agent name, retaining IDs only where they are the useful identity. */
export function GrantPrincipalLabel({ principal }: { principal: GrantPrincipal }): JSX.Element {
  const toolCallAgent = useToolCallAgent();
  switch (principal.kind) {
    case "agent":
      return (
        <>
          Agent <AgentName agentId={principal.agent_id} />
        </>
      );
    case "session":
      return (
        <>
          Session {principal.session_id}
          {toolCallAgent && (
            <>
              {" · "}Agent <AgentName agentId={toolCallAgent.agentId} displayName={toolCallAgent.displayName} />
            </>
          )}
        </>
      );
    case "access_profile":
      return <>Access profile {principal.access_profile_id}</>;
  }
}
