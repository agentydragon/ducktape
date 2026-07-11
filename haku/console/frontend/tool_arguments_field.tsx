import { Field } from "./field.tsx";
import { toolPreview } from "./tool_previews/index.tsx";
import { clampBlock, type PreviewVariant } from "./tool_previews/variant.tsx";

// A compact raw-JSON fallback shows only the first few lines (for a tool with no custom
// widget); the detailed view shows it in full.
const COMPACT_JSON_LINES = 6;

/** The arguments of a tool call: a per-tool-type widget (the tool_previews/ per-server modules)
 * when one matches — rendered directly, since it's self-describing — else the generic raw-JSON
 * view, which keeps an "Arguments" label so it isn't mistaken for a result. `variant` picks the
 * compact (skim) or detailed form; detailed always offers the raw JSON behind a disclosure even
 * when a widget rendered. Shared by the approval drawer and the past-tool-calls history view. */
export function ToolArgumentsField({
  serverId,
  toolName,
  args,
  argumentsJson,
  variant,
}: {
  serverId: string;
  toolName: string;
  args: Record<string, unknown>;
  argumentsJson: string;
  variant: PreviewVariant;
}) {
  const nice = toolPreview(serverId, toolName, args, variant);
  if (!nice) {
    return (
      <Field label="Arguments">
        <pre className="haku-shell-json">
          {variant === "compact" ? clampBlock(argumentsJson, COMPACT_JSON_LINES) : argumentsJson}
        </pre>
      </Field>
    );
  }
  return (
    <>
      {nice}
      {variant === "detailed" && (
        <details className="haku-shell-disclosure">
          <summary>Raw arguments</summary>
          <pre className="haku-shell-json">{argumentsJson}</pre>
        </details>
      )}
    </>
  );
}
