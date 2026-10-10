import { Code, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "../chips";
import { definePreview, type ArgumentsPreview } from "../entry";

const podsExecArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  command: z.array(z.string()),
});

type PodsExecArguments = z.infer<typeof podsExecArguments>;

function shellQuote(arg: string): string {
  return /^[A-Za-z0-9_./:=,@+-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", "'\\''")}'`;
}

function commandText(args: PodsExecArguments): string {
  return args.command.map(shellQuote).join(" ");
}

function CommandLine({ args }: { args: PodsExecArguments }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
      <Code>$ {commandText(args)}</Code>
    </Text>
  );
}

function Collapsed({ args }: { args: PodsExecArguments }): JSX.Element {
  return <CommandLine args={args} />;
}

function Opened({ args }: { args: PodsExecArguments }): JSX.Element {
  return (
    <Stack gap="xs">
      <CommandLine args={args} />
      {args.container && <Chip label="container" value={args.container} />}
    </Stack>
  );
}

/** These are action-owned DOM renderers; the target is already in the action title. */
export const podsExecCollapsed: ArgumentsPreview = definePreview(podsExecArguments, Collapsed);
export const podsExecPane: ArgumentsPreview = definePreview(podsExecArguments, Opened);

function NoAdditionalOptions(): JSX.Element {
  return (
    <Text size="sm" c="dimmed">
      No additional options.
    </Text>
  );
}

const podsDeleteArguments = z.strictObject({ name: z.string().min(1), namespace: z.string().min(1).optional() });

export const podsDeletePane: ArgumentsPreview = definePreview(podsDeleteArguments, NoAdditionalOptions);
