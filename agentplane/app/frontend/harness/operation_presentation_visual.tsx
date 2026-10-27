import { Stack } from "@mantine/core";
import type { JSX } from "react";

import { JsonBlock, TextBlock } from "../text_block";
import { CodeBlock } from "../../../../util/frontend/code_block";
import { ShellCommandView, OutputBlock } from "../shell_command_view";

const manifest = [
  "apiVersion: v1",
  "kind: ConfigMap",
  "metadata:",
  "  name: demo-settings",
  "  namespace: test-apps",
  "data:",
  ...Array.from({ length: 18 }, (_, index) => `  setting-${String(index + 1).padStart(2, "0")}: enabled`),
].join("\n");

const retainedText = Array.from(
  { length: 48 },
  (_, index) => `line-${String(index + 1).padStart(2, "0")}: ${"wrap-me-".repeat(18)}`
).join("\n");

export function OperationPresentationVisual(): JSX.Element {
  return (
    <main style={{ padding: "var(--mantine-spacing-md)" }}>
      <Stack
        id="operation-presentation-surface"
        className="agentplane-action-operation-host"
        data-operation-density="detail"
        gap="sm"
        maw={760}
        mx="auto"
      >
        <TextBlock heading="Applied manifest" language="yaml" text={manifest} />
        <TextBlock
          heading="Description"
          format="prose"
          text={
            "The pod reported a zero-width\u200b marker in its response. The text stays selectable while the marker remains visible."
          }
          clamp={false}
        />
        <JsonBlock heading="Recorded data" value={{ phase: "Running", replicas: 2 }} clamp={false} />
        <ShellCommandView
          argv={["printf", "hello from argv", "line 2"]}
          notes={["Container app"]}
          releaseWhenFar
          surface="framed"
        />
        <OutputBlock name="stdout" text={"hello from the selected container\n"} raw releaseWhenFar surface="framed" />
      </Stack>
      <div style={{ height: "1800px" }} aria-hidden="true" />
      <section id="parked-code-section" aria-label="Long code sample">
        <CodeBlock text={retainedText} presentation="block" releaseWhenFar />
      </section>
      <div style={{ height: "3000px" }} aria-hidden="true" />
    </main>
  );
}
