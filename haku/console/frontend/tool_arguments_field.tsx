import { Field } from "./field.tsx";
import { toolPreview } from "./tool_previews/index.tsx";

/** Arguments field for a tool-call approval/result: a per-tool-type widget
 * (the tool_previews/ per-server modules) when one matches, else the generic raw-JSON view.
 * Shared by the approval drawer and the past-tool-calls history view. */
export function ToolArgumentsField({
  serverId,
  toolName,
  args,
  argumentsJson,
}: {
  serverId: string;
  toolName: string;
  args: Record<string, unknown>;
  argumentsJson: string;
}) {
  const nice = toolPreview(serverId, toolName, args);
  return <Field label="Arguments">{nice ?? <pre className="haku-shell-json">{argumentsJson}</pre>}</Field>;
}
