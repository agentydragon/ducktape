import { Text } from "@mantine/core";
import Ajv2020 from "ajv/dist/2020.js";
import type { JSX } from "react";

import type { LaunchRoutineArguments } from "../../../mcp/launch_routine_arguments";
import launchRoutineArgumentsSchema from "../../../mcp/launch_routine_arguments.schema.json";
import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { clampBlock, type PreviewVariant } from "../vocabulary";

export type { LaunchRoutineArguments };

const validate = new Ajv2020().compile<LaunchRoutineArguments>(launchRoutineArgumentsSchema);

export function validateLaunchRoutineArguments(args: unknown): args is LaunchRoutineArguments {
  return validate(args);
}

export function LaunchRoutinePreview({
  args,
  variant,
}: {
  args: LaunchRoutineArguments;
  variant: PreviewVariant;
}): JSX.Element {
  const text = args.text?.trim();
  const shown = text ? (variant === "compact" ? clampBlock(text, 3) : text) : null;
  return (
    <Field label="Instructions">
      {shown ? (
        <CodeBlock value={shown} />
      ) : (
        <Text size="sm" c="dimmed">
          (routine default)
        </Text>
      )}
    </Field>
  );
}
