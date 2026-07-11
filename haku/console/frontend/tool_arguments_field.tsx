import { Field } from "./field.tsx";
import { toolPreview } from "./tool_previews/index.tsx";
import { clampBlock, type PreviewVariant } from "./tool_previews/variant.tsx";

// A compact raw-JSON fallback shows only the first few lines (for a tool with no custom
// widget); the detailed view shows it in full.
const COMPACT_JSON_LINES = 6;

/** Arguments field for a tool-call approval/result: a per-tool-type widget (the tool_previews/
 * per-server modules) when one matches, else the generic raw-JSON view. `variant` picks the
 * compact form (a scannable summary for drawer cards) or the detailed form (the expanded view,
 * which also offers the raw JSON behind a disclosure even when a custom widget rendered it).
 * Shared by the approval drawer and the past-tool-calls history view. */
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
  return (
    <Field label="Arguments">
      {nice ?? (
        <pre className="haku-shell-json">
          {variant === "compact" ? clampBlock(argumentsJson, COMPACT_JSON_LINES) : argumentsJson}
        </pre>
      )}
      {/* When a custom widget rendered, the detailed view still offers the raw JSON behind a
          disclosure; the raw-JSON fallback above already is the JSON, so it needs none. */}
      {nice && variant === "detailed" && (
        <details className="haku-shell-disclosure">
          <summary>Raw arguments</summary>
          <pre className="haku-shell-json">{argumentsJson}</pre>
        </details>
      )}
    </Field>
  );
}
