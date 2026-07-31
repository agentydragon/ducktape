import { CodeBlock } from "./code_block";
import { Field } from "./field";
import { JsonPreview } from "./json_preview";
import { toolPreview } from "./tool_rendering/index";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

/** The exact raw-arguments JSON behind a collapsed disclosure, byte-exact (`JSON.stringify`) so
 * reflow/truncation never costs the real, copyable payload. Shared by `ToolArgumentsField` and a
 * combined call widget's own detailed body (tool_call_card.tsx). */
export function RawArgumentsDisclosure({ argumentsJson }: { argumentsJson: string }) {
  return (
    <details className="haku-shell-disclosure">
      <summary>Raw arguments</summary>
      <CodeBlock language="json" value={argumentsJson} />
    </details>
  );
}

/** The arguments of a tool call: a per-tool-type widget (the tool_rendering/ per-server requests modules)
 * when one matches — rendered directly, since it's self-describing — else the generic
 * syntax-highlighted JSON view (compact-printed + truncated in brief mode, full in detailed),
 * which keeps an "Arguments" label so it isn't mistaken for a result. `variant` picks the compact
 * (skim) or detailed form; detailed always offers the exact raw JSON behind a disclosure even when
 * a widget rendered. Shared by the approvals panel and the past-tool-calls history view. */
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
        <JsonPreview value={args} variant={variant} />
      </Field>
    );
  }
  return (
    <>
      {nice}
      {variant === "detailed" && <RawArgumentsDisclosure argumentsJson={argumentsJson} />}
    </>
  );
}
