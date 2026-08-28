import { CodeBlock } from "./code_block";
import { Field } from "./field";
import type { PreviewVariant } from "./tool_rendering/vocabulary";
import { unwrapToolResult } from "./tool_rendering/result_entry";
import { toolResultPreview } from "./tool_rendering/index";

/** The exact stored result envelope behind a collapsed disclosure — byte-exact-ish (the stored
 * envelope, not the unwrapped payload), so a widget's ranking never costs the real, copyable
 * result. Shared by `ToolResultField` and a combined call widget's own detailed body
 * (tool_call_card.tsx). */
export function RawResultDisclosure({ result }: { result: unknown }): JSX.Element {
  return (
    <details className="haku-shell-disclosure">
      <summary>Raw result</summary>
      <CodeBlock language="json" value={JSON.stringify(result, null, 2)} />
    </details>
  );
}

/** The result of a finished tool call: a per-server widget (tool_rendering/'s responses modules)
 * over the unwrapped CallToolResult payload when one matches, else the raw-JSON "Result" field —
 * detailed only, so a compact card shows a result only when a widget makes it self-describing.
 * Detailed always offers the exact stored envelope behind a `Raw result` disclosure once a widget
 * rendered. Renders nothing while there is no result yet. Shared by the approvals panel's recent
 * cards and the history view. */
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
}): JSX.Element | null {
  if (result == null) return null;
  const nice = toolResultPreview(serverId, toolName, unwrapToolResult(result), variant);
  if (!nice) {
    if (variant !== "detailed") return null;
    return (
      <div className="haku-shell-fields">
        <Field label="Result">
          <CodeBlock language="json" value={JSON.stringify(result, null, 2)} />
        </Field>
      </div>
    );
  }
  return (
    <>
      {nice}
      {variant === "detailed" && <RawResultDisclosure result={result} />}
    </>
  );
}
