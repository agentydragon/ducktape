import { Field } from "./field.tsx";
import type { PreviewVariant } from "./tool_rendering/vocabulary.tsx";
import { unwrapToolResult } from "./tool_rendering/result_entry.tsx";
import { toolResultPreview } from "./tool_rendering/index.tsx";

/** The result of a finished tool call: a per-tool-type widget (the tool_rendering/ per-server responses
 * modules) over the unwrapped CallToolResult payload when one matches, else the raw-JSON
 * "Result" field — but only in detailed, so compact cards stay skimmable (a compact card shows
 * a result only when a widget makes it self-describing, mirroring how compact never shows raw
 * arguments JSON of a widget-rendered call). Detailed always offers the exact stored envelope
 * behind a `Raw result` disclosure once a widget rendered. Renders nothing while there is no
 * result yet. Shared by the approvals panel's recent cards and the history view. */
export function ToolResultField({
  serverId,
  toolName,
  result,
  variant,
}: {
  serverId: string;
  toolName: string;
  result: unknown;
  variant: PreviewVariant;
}) {
  if (result == null) return null;
  const nice = toolResultPreview(serverId, toolName, unwrapToolResult(result), variant);
  if (!nice) {
    if (variant !== "detailed") return null;
    return (
      <div className="haku-shell-fields">
        <Field label="Result">
          <pre className="haku-shell-json">{JSON.stringify(result, null, 2)}</pre>
        </Field>
      </div>
    );
  }
  return (
    <>
      {nice}
      {variant === "detailed" && (
        <details className="haku-shell-disclosure">
          <summary>Raw result</summary>
          {/* The disclosure stays byte-exact-ish (the stored envelope, not the unwrapped
              payload), so the widget's ranking never costs the real, copyable result. */}
          <pre className="haku-shell-json">{JSON.stringify(result, null, 2)}</pre>
        </details>
      )}
    </>
  );
}
