import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { listAgents } from "./client";

export type AgentNames = ReadonlyMap<string, string>;

const EMPTY_AGENT_NAMES: AgentNames = new Map();
const AgentNamesContext = createContext<AgentNames>(EMPTY_AGENT_NAMES);

/** Load the operator's Agent names once for tool-call argument previews. */
export function AgentNamesProvider({
  children,
  initialNames,
  load = true,
}: {
  children: ReactNode;
  initialNames?: AgentNames;
  load?: boolean;
}): JSX.Element {
  const [names, setNames] = useState<AgentNames>(initialNames ?? EMPTY_AGENT_NAMES);

  useEffect(() => {
    if (!load) return;
    let alive = true;
    void listAgents().then(
      ({ agents }) => {
        if (alive) setNames(new Map(agents.map((agent) => [agent.agent_id, agent.display_name])));
      },
      () => {
        // The surrounding tool-call surface remains useful if the optional name lookup fails;
        // the AgentName component keeps the UUID in a title as the diagnostic detail.
      }
    );
    return () => {
      alive = false;
    };
  }, [load]);

  return <AgentNamesContext.Provider value={names}>{children}</AgentNamesContext.Provider>;
}

/** Render a friendly Agent name while retaining the UUID as non-default detail. */
export function AgentName({ agentId, displayName }: { agentId: string; displayName?: string }): JSX.Element {
  const names = useContext(AgentNamesContext);
  const name = displayName ?? names.get(agentId) ?? "Unknown";
  return <span title={`Agent UUID: ${agentId}`}>{name}</span>;
}
