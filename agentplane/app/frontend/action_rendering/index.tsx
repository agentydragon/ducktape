// How an Action's call draws for the operator. Widgets for particular Actions are registered by the
// Action's identity `(group, name)`: one for its request (the arguments), one for its response (the
// value its tool returned), or both. The group is the name the Action Service configures a backend
// under, so an entry follows that name rather than the server's own. Adding one is a module beside
// this for the group's widgets and a row in REGISTRY. Each dispatch safeParses once and hands the
// widget typed data, and falls back where no widget matches.
import type { ReactNode } from "react";

import { type CallToolResult, CallToolResultView, toolValue } from "../call_tool_result";
import type { ActionRequestView } from "../client";
import { renderPreview, type ArgumentsPreview } from "./entry";
import { renderResultPreview, type ResultPreview } from "./result_entry";
import { execArgumentsPreview, execResultPreview } from "./ssh";

type ActionIdentity = ActionRequestView["action"];

interface ActionRendering {
  arguments?: ArgumentsPreview;
  result?: ResultPreview;
}

// Maps rather than object literals, so no group or Action name reaches `Object.prototype`.
const REGISTRY: ReadonlyMap<string, ReadonlyMap<string, ActionRendering>> = new Map([
  // x/ssh_mcp_server/server.py, under the group name staging configures it as.
  ["ssh", new Map([["exec", { arguments: execArgumentsPreview, result: execResultPreview }]])],
]);

/** An Action's arguments drawn by its own widget when its schema parses them; otherwise `null`, which
 * every card shows as their JSON. The fallback is chosen here rather than by the cards, so a
 * rendering of arguments no widget takes can replace that `null` without them changing. */
export function renderArguments(action: ActionIdentity, args: unknown): ReactNode | null {
  const preview = REGISTRY.get(action.group)?.get(action.name)?.arguments;
  return preview ? renderPreview(preview, args) : null;
}

/** An MCP tool's stored result drawn by the Action's own widget when its schema parses the tool's
 * value; otherwise the way the tool answered. */
export function renderMcpResult(action: ActionIdentity, result: CallToolResult): ReactNode {
  const preview = REGISTRY.get(action.group)?.get(action.name)?.result;
  const value = toolValue(result);
  const drawn = preview && value !== undefined ? renderResultPreview(preview, value) : null;
  return drawn ?? <CallToolResultView result={result} />;
}
