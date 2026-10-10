import { Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { CodeBlock } from "../../../code_block";
import { definePreview, type ArgumentsPreview } from "../entry";

const resourceApplyArguments = z.strictObject({ resource: z.string().min(1) });

function Collapsed(): JSX.Element {
  return <Text size="sm">Kubernetes manifest</Text>;
}

function Manifest({ args }: { args: z.infer<typeof resourceApplyArguments> }): JSX.Element {
  return <CodeBlock text={args.resource} language="yaml" />;
}

export const resourcesApplyCollapsed: ArgumentsPreview = definePreview(resourceApplyArguments, Collapsed);
export const resourcesApplyManifest: ArgumentsPreview = definePreview(resourceApplyArguments, Manifest);
