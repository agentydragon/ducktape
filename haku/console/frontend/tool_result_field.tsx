import type { JSX } from "react";
import { CodeBlock } from "./code_block";
import { Field } from "./field";
import type { PreviewVariant } from "./tool_rendering/vocabulary";

export function ToolResultField({ result, variant }: { result: unknown; variant: PreviewVariant }): JSX.Element | null {
  if (result == null || variant !== "detailed") return null;
  return (
    <div className="haku-shell-fields">
      <Field label="Result">
        <CodeBlock language="json" value={JSON.stringify(result, null, 2)} />
      </Field>
    </div>
  );
}
