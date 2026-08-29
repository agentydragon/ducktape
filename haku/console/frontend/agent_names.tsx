import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { listAgents } from "./client";

export type AgentNames = ReadonlyMap<string, string>;

export type AgentIdentity = Readonly<{
  agentId: string;
  displayName: string;
}>;

const EMPTY_AGENT_NAMES: AgentNames = new Map();
const AgentNamesContext = createContext<AgentNames>(EMPTY_AGENT_NAMES);
const ToolCallAgentContext = createContext<AgentIdentity | null>(null);

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

export function useAgentNames(): AgentNames {
  return useContext(AgentNamesContext);
}

/** Render a friendly Agent name while retaining the UUID as non-default detail. */
export function AgentName({ agentId, displayName }: { agentId: string; displayName?: string }): JSX.Element {
  const names = useAgentNames();
  const name = displayName ?? names.get(agentId) ?? "Unknown";
  return <span title={`Agent UUID: ${agentId}`}>{name}</span>;
}

/** Make the trusted Agent caller available to tool result widgets without changing their wire data. */
export function ToolCallAgentProvider({
  agentId,
  displayName,
  children,
}: {
  agentId: string | null;
  displayName: string;
  children: ReactNode;
}): JSX.Element {
  const identity = agentId === null ? null : { agentId, displayName };
  return <ToolCallAgentContext.Provider value={identity}>{children}</ToolCallAgentContext.Provider>;
}

export function useToolCallAgent(): AgentIdentity | null {
  return useContext(ToolCallAgentContext);
}
