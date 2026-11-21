import { Text } from "@mantine/core";
import { z } from "zod";

import { OutputBlock } from "../../../ui/shell_command_view";
import { defineResultPreview, type ResultPreview } from "../result_entry";

const textEnvelope = z.object({
  content: z.array(z.object({ type: z.literal("text"), text: z.string() })).length(1),
  structuredContent: z.never().optional(),
});

export function kubernetesTextResult(title: string, empty: string, sentinels?: RegExp): ResultPreview {
  return defineResultPreview(textEnvelope, ({ result }) => {
    const { text } = result.content[0];
    return text === "" || sentinels?.test(text) ? (
      <Text size="sm" c="dimmed">
        {empty}
      </Text>
    ) : (
      <OutputBlock name={title} text={text} raw releaseWhenFar surface="framed" />
    );
  });
}
