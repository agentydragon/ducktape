import { Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { ShellCommandView } from "../../ui/shell_command_view";
import { TextBlock } from "../../ui/text_block";
import { definePreview, type ArgumentsPreview } from "./entry";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";
import { DispatchDetails, SessionLink, TurnDetails, sentence, sessionTurn } from "./session_delegation";

const name = z.string().min(1);
const sandbox = z.strictObject({ name, uid: name });
const sandboxArgs = z.strictObject({ sandbox });
const sessionArgs = sandboxArgs.extend({ session_id: name });
const commandArgs = sessionArgs.extend({ command_id: name });
const empty = z.strictObject({});

function details(label: string, args: ArgumentsPreview, result: ResultPreview): ActionPresentation {
  return { fallbackLabel: label, pane: { opened: args }, details: { arguments: args, nativeResult: result } };
}

function onlyResult(label: string, result: ResultPreview): ActionPresentation {
  return {
    fallbackLabel: label,
    details: { arguments: definePreview(empty, () => null), nativeResult: result },
    compactCardExpands: expandsForRejectedArguments(empty),
  };
}

function SessionTarget({ sandboxName, sessionId }: { sandboxName: string; sessionId: string }): ReactNode {
  return (
    <Group gap="xs">
      <Text size="sm">Sandbox {sandboxName}</Text>
      <SessionLink id={sessionId} />
    </Group>
  );
}

const launch = z.strictObject({
  name,
  template: name,
  policy_sets: z.array(name).optional(),
  inbox_tags: z.array(name).optional(),
  bootstrap: z.string().optional(),
  wake_on_send: z.boolean().optional(),
  idle_suspend_after_seconds: z.number().nullable().optional(),
  launcher_grants: z.array(name).optional(),
});

function Grants({ title, grants }: { title: string; grants?: string[] }): ReactNode {
  return (
    <Text size="sm">
      {title} ·{" "}
      {grants?.length
        ? grants.map((grant) => (grant === "send_message" ? "Send inbox messages" : grant)).join(", ")
        : "None"}
    </Text>
  );
}

const launchPreview = definePreview(launch, ({ args }) => (
  <Stack gap="xs">
    <Text size="sm" fw={600}>
      {args.name} · Template {args.template}
    </Text>
    <Grants title="Sandbox policy sets" grants={args.policy_sets} />
    <Grants title="Launcher grants" grants={args.launcher_grants} />
    {args.inbox_tags?.length ? <Text size="sm">Inbox tags · {args.inbox_tags.join(", ")}</Text> : null}
    <Text size="sm">{args.wake_on_send ? "Wake on incoming work" : "Wake on incoming work is off"}</Text>
    {args.idle_suspend_after_seconds && (
      <Text size="sm">Suspend after {args.idle_suspend_after_seconds} seconds idle</Text>
    )}
    {args.bootstrap && (
      <ShellCommandView command={args.bootstrap} notes={["Runs once before the first Session"]} surface="framed" />
    )}
  </Stack>
));

const launchResult = defineResultPreview(
  z.object({ sandbox, launcher_grants: z.array(name).nullable() }),
  ({ result }) => (
    <Stack gap={4}>
      <Text size="sm">Sandbox {result.sandbox.name} is available for Session requests.</Text>
      <Grants title="Launcher grants" grants={result.launcher_grants ?? undefined} />
    </Stack>
  )
);

const presets = defineResultPreview(
  z.object({
    presets: z.record(
      name,
      z.object({
        title: name,
        launch: z.object({ template: name, policy_sets: z.array(name).optional() }),
        create_session: z.object({ spec: z.object({ model: name, harness: name, reasoning_effort: name.optional() }) }),
      })
    ),
  }),
  ({ result }) => {
    const entries = Object.entries(result.presets);
    return (
      <Stack gap="xs">
        {entries.length === 0 && <Text size="sm">No launch presets offered.</Text>}
        {entries.map(([key, preset]) => (
          <Stack key={key} gap={4}>
            <Text size="sm" fw={600}>
              {preset.title}
            </Text>
            <Text size="sm">
              Template {preset.launch.template} · {preset.create_session.spec.model}
            </Text>
            <Grants title="Sandbox policy sets" grants={preset.launch.policy_sets} />
          </Stack>
        ))}
      </Stack>
    );
  }
);

const createSession = definePreview(
  sessionArgs.extend({
    spec: z.strictObject({
      harness: name,
      model: name,
      cwd: name,
      reasoning_effort: name.optional(),
      instructions: z.string().optional(),
    }),
    setup_script: z.string().nullish(),
  }),
  ({ args }) => (
    <Stack gap="xs">
      <Text size="sm">Sandbox {args.sandbox.name}</Text>
      <Text size="sm">
        {sentence(args.spec.harness.replace(/^HARNESS_/, ""))} · {args.spec.model}
        {args.spec.reasoning_effort && ` · ${args.spec.reasoning_effort} effort`}
      </Text>
      <Text size="sm">Working directory {args.spec.cwd}</Text>
      {args.setup_script && (
        <ShellCommandView command={args.setup_script} notes={["Runs before the Session starts"]} surface="framed" />
      )}
      {args.spec.instructions && (
        <TextBlock heading="Standing instructions" text={args.spec.instructions} format="prose" />
      )}
      <Text size="sm" c="dimmed">
        Creates a Session without starting a turn.
      </Text>
    </Stack>
  )
);
const materialization = z.object({ materialization: name.optional(), reason: z.string().optional() });
const created = defineResultPreview(
  z.object({ session_id: name, status: materialization.optional() }),
  ({ result }) => (
    <Stack gap={4}>
      <Text size="sm">
        Session created
        {result.status?.materialization &&
          ` · ${sentence(result.status.materialization.replace(/^SESSION_MATERIALIZATION_/, ""))}`}
      </Text>
      {result.status?.reason && <Text size="sm">{result.status.reason}</Text>}
      <SessionLink id={result.session_id} />
    </Stack>
  )
);

const dispatch = defineResultPreview(
  z.object({ session_id: name, state: z.string().optional(), reason: z.string().optional() }),
  ({ result }) => <DispatchDetails value={result} />
);
const listing = z.object({
  session_id: name,
  feed_status: name.optional(),
  feed_error: z.string().optional(),
  last_turn_status: name.optional(),
  runner: z
    .object({ harness: name.optional(), harness_state: name.optional(), active_turn_id: name.optional() })
    .optional(),
});

function SessionListing({ value }: { value: z.infer<typeof listing> }): ReactNode {
  return (
    <Stack gap={4}>
      <SessionLink id={value.session_id} />
      {value.runner?.harness && <Text size="sm">{sentence(value.runner.harness.replace(/^HARNESS_/, ""))}</Text>}
      {value.runner?.active_turn_id && <Badge variant="light">Turn active</Badge>}
      {value.last_turn_status && (
        <Text size="sm">Last turn · {sentence(value.last_turn_status.replace(/^TURN_STATUS_/, ""))}</Text>
      )}
      {value.feed_error ? (
        <Text size="sm">{value.feed_error}</Text>
      ) : (
        value.feed_status && <Text size="sm">History · {sentence(value.feed_status.replace(/^FEED_STATUS_/, ""))}</Text>
      )}
    </Stack>
  );
}

const sessionView = defineResultPreview(
  z.object({
    sandbox_ready: z.object({ status: name, reason: z.string(), message: z.string().optional() }).optional(),
    status: materialization.optional(),
    session: listing.optional(),
    dispatches: z.array(z.unknown()).nullable(),
  }),
  ({ result }) => (
    <Stack gap="xs">
      {result.sandbox_ready && (
        <Text size="sm">
          {result.sandbox_ready.status === "True" ? "Sandbox ready" : "Sandbox not ready"}
          {result.sandbox_ready.message && ` · ${result.sandbox_ready.message}`}
        </Text>
      )}
      {result.status?.materialization && (
        <Text size="sm">
          Session · {sentence(result.status.materialization.replace(/^SESSION_MATERIALIZATION_/, ""))}
        </Text>
      )}
      {result.status?.reason && <Text size="sm">{result.status.reason}</Text>}
      {result.session ? <SessionListing value={result.session} /> : <Text size="sm">No Session history observed.</Text>}
      {result.dispatches?.length ? <Text size="sm">Recent dispatches · {result.dispatches.length}</Text> : null}
    </Stack>
  )
);
const sessions = defineResultPreview(z.object({ sessions: z.array(listing).nullable() }), ({ result }) => (
  <Stack gap="md">
    {result.sessions?.length ? (
      result.sessions.map((value) => <SessionListing key={value.session_id} value={value} />)
    ) : (
      <Text size="sm">No Sessions observed.</Text>
    )}
  </Stack>
));
const turns = defineResultPreview(z.object({ turns: z.array(sessionTurn).nullable() }), ({ result }) => (
  <Stack gap="sm">
    {result.turns?.length ? (
      result.turns.map((value) => <TurnDetails key={value.turn_id} item={value} />)
    ) : (
      <Text size="sm">No turns returned.</Text>
    )}
  </Stack>
));
const wait = defineResultPreview(
  z.object({ completed: z.boolean(), turn: z.object({ status: name, error: z.string().optional() }).optional() }),
  ({ result }) => (
    <Stack gap={4}>
      <Text size="sm">{result.completed ? "Turn completed" : "No completion observed before the wait ended"}</Text>
      {result.turn?.status && <Text size="sm">{sentence(result.turn.status.replace(/^TURN_STATUS_/, ""))}</Text>}
      {result.turn?.error && <Text size="sm">{result.turn.error}</Text>}
    </Stack>
  )
);

export const orchestrationRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["launch_sandbox", details("Launch Session Sandbox", launchPreview, launchResult)],
  ["list_launch_presets", onlyResult("List launch presets", presets)],
  ["create_session", details("Create Session in Sandbox", createSession, created)],
  [
    "get_session",
    details(
      "Inspect Sandbox Session",
      definePreview(sessionArgs, ({ args }) => (
        <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
      )),
      sessionView
    ),
  ],
  [
    "list_sessions",
    details(
      "List Sandbox Sessions",
      definePreview(sandboxArgs, ({ args }) => <Text size="sm">Sandbox {args.sandbox.name}</Text>),
      sessions
    ),
  ],
  [
    "change_model",
    details(
      "Change Session model",
      definePreview(commandArgs.extend({ model: name }), ({ args }) => (
        <Stack gap={4}>
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <Text size="sm">Use {args.model} for future turns</Text>
        </Stack>
      )),
      dispatch
    ),
  ],
  [
    "change_reasoning_effort",
    details(
      "Change Session reasoning effort",
      definePreview(commandArgs.extend({ effort: name }), ({ args }) => (
        <Stack gap={4}>
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <Text size="sm">Set reasoning effort to {args.effort} for future turns</Text>
        </Stack>
      )),
      dispatch
    ),
  ],
  [
    "submit_input",
    details(
      "Submit input to Sandbox Session",
      definePreview(commandArgs.extend({ text: name }), ({ args }) => (
        <Stack gap="xs">
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <TextBlock heading="Message" text={args.text} format="prose" />
        </Stack>
      )),
      dispatch
    ),
  ],
  [
    "interrupt_turn",
    details(
      "Interrupt Sandbox Session turn",
      definePreview(commandArgs.extend({ turn_id: name }), ({ args }) => (
        <Stack gap={4}>
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <Text size="sm">Observed turn {args.turn_id}</Text>
        </Stack>
      )),
      dispatch
    ),
  ],
  [
    "read_turns",
    details(
      "Read Sandbox Session turns",
      definePreview(sessionArgs.extend({ count: z.number() }), ({ args }) => (
        <Stack gap={4}>
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <Text size="sm">Up to {args.count} recent turns</Text>
        </Stack>
      )),
      turns
    ),
  ],
  [
    "wait_for_turn",
    details(
      "Wait for Sandbox Session turn",
      definePreview(sessionArgs.extend({ resume_token: name, wait_seconds: z.number() }), ({ args }) => (
        <Stack gap={4}>
          <SessionTarget sandboxName={args.sandbox.name} sessionId={args.session_id} />
          <Text size="sm">Up to {args.wait_seconds} seconds</Text>
        </Stack>
      )),
      wait
    ),
  ],
]);
