import { currentAgentId, getAgentIdFromUrl, setAgentId } from "../shared/router";
import { startAgentsSubscription, stopAgentsSubscription } from "./agents/stores";
import { connectAgentMcp, disconnectAgentMcp } from "./chat/stores";

export function initAgentUiController(): () => void {
  // Authoritative bootstrap: read agent_id from URL before subscribing
  let bootstrapped = false;
  try {
    const fromUrl = getAgentIdFromUrl();
    if (fromUrl) setAgentId(fromUrl);
  } finally {
    bootstrapped = true;
  }

  // Start agents MCP subscription immediately so the sidebar populates on refresh
  startAgentsSubscription();

  let lastId: string | null = null;
  const unsub = currentAgentId.subscribe((id) => {
    // Ignore emissions until URL bootstrap completes
    if (!bootstrapped) return;
    if (id === lastId) return;
    lastId = id ?? null;
    if (typeof id === "string" && id.length > 0) {
      // Agent selected: connect to agent MCP (connectAgentMcp handles disconnect internally)
      // Defer to next microtask to avoid racing with URL/store updates
      queueMicrotask(() => connectAgentMcp(id));
    } else {
      // No agent selected: disconnect agent MCP
      if (lastId !== null) void disconnectAgentMcp();
    }
  });

  return () => {
    unsub();
    stopAgentsSubscription();
  };
}
