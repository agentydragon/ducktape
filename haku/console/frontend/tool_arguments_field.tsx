import type { JSX } from "react";
import { CodeBlock } from "./code_block";
import { Field } from "./field";
import { JsonPreview } from "./json_preview";
import { toolPreview } from "./tool_rendering/index";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

function RawArgumentsDisclosure({ argumentsJson }: { argumentsJson: string }): JSX.Element {
  return (
    <details className="haku-shell-disclosure">
      <summary>Raw arguments</summary>
      <CodeBlock language="json" value={argumentsJson} />
    </details>
  );
}

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
}): JSX.Element {
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
