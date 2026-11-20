import { Anchor, Badge, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { z } from "zod";

import { definePreview, type ArgumentsPreview } from "./entry";
import type { ActionPresentation } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";
import { ShellCommandView } from "../../ui/shell_command_view";
import { TextBlock } from "../../ui/text_block";

const name = z.string().min(1);
const target = z.object({ name, namespace: name, uid: name });
const sessionId = z.object({ session_id: name });
const sessionCommand = sessionId.extend({ command_id: name });
const sourceError = z.object({ message: z.string() });

function sentence(value: string): string {
  const words = value.toLowerCase().replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function SessionLink({ id }: { id: string }): ReactNode {
  return (
    <Anchor component={Link} to={`/threads/${encodeURIComponent(id)}`} size="sm">
      Open Session
    </Anchor>
  );
}

function EnvironmentName({ value }: { value: z.infer<typeof target> }): ReactNode {
  return (
    <Text size="sm">
      <Text span fw={600}>
        {value.name}
      </Text>{" "}
      in {value.namespace}
    </Text>
  );
}

function presentation(label: string, args: ArgumentsPreview, result: ResultPreview): ActionPresentation {
  return { fallbackLabel: label, pane: { opened: args }, details: { arguments: args, nativeResult: result } };
}

const environment = z.object({
  target,
  configuration: z.object({ template: z.string().optional() }).optional(),
  observed_state: z
    .object({
      phase: z.string().optional(),
      operating_mode: z.string().optional(),
      readiness: z
        .object({ state: z.string().optional(), reason: z.string().optional(), detail: z.string().optional() })
        .optional(),
    })
    .optional(),
});

function Environment({ value }: { value: z.infer<typeof environment> }): ReactNode {
  const state = value.observed_state;
  const readiness = state?.readiness;
  return (
    <Stack gap={4}>
      <EnvironmentName value={value.target} />
      {value.configuration?.template && <Text size="sm">Template {value.configuration.template}</Text>}
      {state?.operating_mode && (
        <Badge variant="light">{sentence(state.operating_mode.replace(/^OPERATING_MODE_/, ""))}</Badge>
      )}
      {readiness?.state ? (
        <Text size="sm">
          {sentence(readiness.state.replace(/^READINESS_STATE_/, ""))}
          {readiness.detail && ` · ${readiness.detail}`}
        </Text>
      ) : (
        state?.phase && <Text size="sm">{sentence(state.phase.replace(/^SANDBOX_PHASE_/, ""))}</Text>
      )}
    </Stack>
  );
}

const environmentList = defineResultPreview(
  z.object({ items: z.array(environment).optional(), next_page_token: z.string().optional() }),
  ({ result }) => (
    <Stack gap="sm">
      {result.items?.length ? (
        result.items.map((item) => <Environment key={item.target.uid} value={item} />)
      ) : (
        <Text size="sm">No eligible environments.</Text>
      )}
      {result.next_page_token && (
        <Text size="sm" c="dimmed">
          More environments available.
        </Text>
      )}
    </Stack>
  )
);
const environmentInspect = definePreview(z.strictObject({ target: target.strict() }), ({ args }) => (
  <EnvironmentName value={args.target} />
));

const resumeLabels: Readonly<Record<string, string>> = {
  RESUME_OUTCOME_REQUESTED: "Resume requested",
  RESUME_OUTCOME_ALREADY_RUNNING: "Already running",
  RESUME_OUTCOME_STATE_CHANGED: "Environment state changed",
};

export const environmentRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "list",
    presentation(
      "List environments",
      definePreview(z.strictObject({ limit: z.number(), after: name.optional() }), ({ args }) => (
        <Text size="sm">
          Up to {args.limit} environments{args.after ? " · next page" : ""}
        </Text>
      )),
      environmentList
    ),
  ],
  [
    "get",
    presentation(
      "Inspect environment",
      environmentInspect,
      defineResultPreview(environment, ({ result }) => <Environment value={result} />)
    ),
  ],
  [
    "resume",
    presentation(
      "Resume environment",
      definePreview(z.strictObject({ target: target.strict(), resume_token: name }), ({ args }) => (
        <EnvironmentName value={args.target} />
      )),
      defineResultPreview(z.object({ target, outcome: name }), ({ result }) => (
        <Stack gap={4}>
          <EnvironmentName value={result.target} />
          <Text size="sm">{resumeLabels[result.outcome] ?? "Resume outcome unavailable"}</Text>
        </Stack>
      ))
    ),
  ],
]);

const deliveryLabels: Readonly<Record<string, string>> = {
  DELIVERY_STATE_QUEUED_IN_DISPATCHER: "Queued for delivery",
  DELIVERY_STATE_DELIVERING_TO_RUNNER: "Delivering to the runner",
  DELIVERY_STATE_RUNNER_ADMITTED: "Recorded by the runner",
  DELIVERY_STATE_FAILED: "Delivery failed",
  DELIVERY_STATE_CANCELLED_IN_DISPATCHER: "Cancelled before delivery",
  DELIVERY_STATE_UNKNOWN: "Delivery outcome unknown",
};

const dispatch = z.object({ session_id: name, state: z.string().optional(), reason: z.string().optional() });
const dispatchResult = defineResultPreview(z.object({ dispatch }), ({ result }) => (
  <Stack gap={4}>
    <Text size="sm">{deliveryLabels[result.dispatch.state ?? ""] ?? "Delivery state unavailable"}</Text>
    {result.dispatch.reason && <Text size="sm">{result.dispatch.reason}</Text>}
    <SessionLink id={result.dispatch.session_id} />
  </Stack>
));

const sessionSpec = z.strictObject({ harness: name, model: name, cwd: name, reasoning_effort: name.optional() });
const createSession = definePreview(
  sessionId.strict().extend({
    target: target.strict(),
    spec: sessionSpec,
    setup_script: z.string().nullable().optional(),
    additional_instructions: z.string().optional(),
  }),
  ({ args }) => (
    <Stack gap="xs">
      <EnvironmentName value={args.target} />
      <Text size="sm">
        {sentence(args.spec.harness.replace(/^HARNESS_/, ""))} · {args.spec.model}
        {args.spec.reasoning_effort && ` · ${args.spec.reasoning_effort} effort`}
      </Text>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Working directory {args.spec.cwd}
      </Text>
      {args.setup_script && (
        <ShellCommandView command={args.setup_script} notes={["Runs before the Session starts"]} surface="framed" />
      )}
      {args.additional_instructions && (
        <TextBlock heading="Standing instructions" format="prose" text={args.additional_instructions} />
      )}
      <Text size="sm" c="dimmed">
        Creates a Session without starting a turn.
      </Text>
    </Stack>
  )
);
const materialization = z.object({ materialization: name.optional(), reason: z.string().optional() });
const createdSession = defineResultPreview(
  z.object({ session: z.object({ session_id: name, status: materialization.optional() }) }),
  ({ result }) => (
    <Stack gap={4}>
      <Text size="sm">
        Session created
        {result.session.status?.materialization &&
          ` · ${sentence(result.session.status.materialization.replace(/^SESSION_MATERIALIZATION_/, ""))}`}
      </Text>
      {result.session.status?.reason && <Text size="sm">{result.session.status.reason}</Text>}
      <SessionLink id={result.session.session_id} />
    </Stack>
  )
);

const source = z.object({ kind: name, error: sourceError.optional() });
const sessionSnapshot = defineResultPreview(
  sessionId.extend({
    session: source.extend({ value: z.object({ status: materialization.optional() }).optional() }),
    history: source,
    read_error: sourceError.optional(),
  }),
  ({ result }) => (
    <Stack gap={4}>
      <SessionLink id={result.session_id} />
      <Text size="sm">
        Dispatcher · {sentence(result.session.kind)}
        {result.session.value?.status?.materialization &&
          ` · ${sentence(result.session.value.status.materialization.replace(/^SESSION_MATERIALIZATION_/, ""))}`}
      </Text>
      {result.session.error && <Text size="sm">{result.session.error.message}</Text>}
      <Text size="sm">History · {sentence(result.history.kind)}</Text>
      {result.history.error && <Text size="sm">{result.history.error.message}</Text>}
      {result.read_error && <Text size="sm">{result.read_error.message}</Text>}
    </Stack>
  )
);

const coverage = z.object({ gaps: z.array(z.unknown()).nullish(), error: sourceError.optional() });
function Coverage({ value }: { value: z.infer<typeof coverage> }): ReactNode {
  return (
    <>
      {Boolean(value.gaps?.length) && <Text size="sm">Some history is missing.</Text>}
      {value.error && <Text size="sm">{value.error.message}</Text>}
    </>
  );
}
const turn = z.object({
  turn_id: name,
  status: name.optional(),
  error: z.string().optional(),
  input: z.array(z.object({ text: z.string() })).nullish(),
  tool_calls: z.array(z.object({ name })).nullish(),
  final_assistant_message: z.string().optional(),
});
const turns = defineResultPreview(
  sessionId.extend(coverage.shape).extend({ turns: z.array(turn).nullable(), next_cursor: z.string().optional() }),
  ({ result }) => (
    <Stack gap="sm">
      <SessionLink id={result.session_id} />
      {result.turns?.length ? (
        result.turns.map((item) => (
          <Stack key={item.turn_id} gap={4}>
            <Text size="sm" fw={600}>
              {item.status ? sentence(item.status.replace(/^TURN_STATUS_/, "")) : "Unfinished turn"}
            </Text>
            {item.input?.map((input, index) => (
              <TextBlock key={index} heading="User" format="prose" text={input.text} />
            ))}
            {Boolean(item.tool_calls?.length) && (
              <Text size="sm">Tools · {item.tool_calls?.map((call) => call.name).join(", ")}</Text>
            )}
            {item.final_assistant_message && (
              <TextBlock heading="Assistant" format="prose" text={item.final_assistant_message} />
            )}
            {item.error && <Text size="sm">{item.error}</Text>}
          </Stack>
        ))
      ) : (
        <Text size="sm">No turns returned.</Text>
      )}
      <Coverage value={result} />
      {result.next_cursor && (
        <Text size="sm" c="dimmed">
          More turns available.
        </Text>
      )}
    </Stack>
  )
);

const observationLabels: Readonly<Record<string, string>> = {
  not_observed: "Input effect not observed",
  input_confirmed: "Input confirmed",
  turn_completed: "Turn completed",
  command_failed: "Input failed",
  command_no_op: "Input produced no change",
};
const observedInput = defineResultPreview(
  sessionId.extend({
    dispatch: source,
    observation: coverage.extend({
      kind: name,
      reason: z.string().optional(),
      turn: turn.omit({ input: true, tool_calls: true }).optional(),
    }),
  }),
  ({ result }) => (
    <Stack gap={4}>
      <SessionLink id={result.session_id} />
      <Text size="sm">Dispatcher · {sentence(result.dispatch.kind)}</Text>
      {result.dispatch.error && <Text size="sm">{result.dispatch.error.message}</Text>}
      <Text size="sm">{observationLabels[result.observation.kind] ?? "Input outcome unavailable"}</Text>
      {result.observation.reason && <Text size="sm">{result.observation.reason}</Text>}
      {result.observation.turn?.final_assistant_message && (
        <TextBlock heading="Assistant" format="prose" text={result.observation.turn.final_assistant_message} />
      )}
      {result.observation.turn?.error && <Text size="sm">{result.observation.turn.error}</Text>}
      <Coverage value={result.observation} />
    </Stack>
  )
);

export const sessionRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["create", presentation("Create Session", createSession, createdSession)],
  [
    "get",
    presentation(
      "Inspect Session",
      definePreview(sessionId.strict(), ({ args }) => <SessionLink id={args.session_id} />),
      sessionSnapshot
    ),
  ],
  [
    "submit_input",
    presentation(
      "Submit Session input",
      definePreview(sessionCommand.strict().extend({ target: target.strict(), text: name }), ({ args }) => (
        <Stack gap="xs">
          <EnvironmentName value={args.target} />
          <SessionLink id={args.session_id} />
          <TextBlock heading="Message" format="prose" text={args.text} />
        </Stack>
      )),
      dispatchResult
    ),
  ],
  [
    "observe_input",
    presentation(
      "Observe Session input",
      definePreview(
        sessionCommand.strict().extend({ wait_seconds: z.number(), resume_token: name.optional() }),
        ({ args }) => (
          <Stack gap={4}>
            <SessionLink id={args.session_id} />
            <Text size="sm">
              {args.wait_seconds ? `Wait up to ${args.wait_seconds} seconds` : "Check current observations"}
            </Text>
          </Stack>
        )
      ),
      observedInput
    ),
  ],
  [
    "cancel_input",
    presentation(
      "Cancel queued Session input",
      definePreview(sessionCommand.strict(), ({ args }) => (
        <Stack gap={4}>
          <SessionLink id={args.session_id} />
          <Text size="sm">Input {args.command_id.slice(0, 8)}</Text>
        </Stack>
      )),
      dispatchResult
    ),
  ],
  [
    "interrupt_turn",
    presentation(
      "Interrupt Session turn",
      definePreview(sessionCommand.strict().extend({ target: target.strict(), turn_id: name }), ({ args }) => (
        <Stack gap={4}>
          <EnvironmentName value={args.target} />
          <SessionLink id={args.session_id} />
          <Text size="sm">Observed turn {args.turn_id}</Text>
        </Stack>
      )),
      dispatchResult
    ),
  ],
  [
    "read_turns",
    presentation(
      "Read Session turns",
      definePreview(sessionId.strict().extend({ count: z.number(), page_cursor: name.optional() }), ({ args }) => (
        <Stack gap={4}>
          <SessionLink id={args.session_id} />
          <Text size="sm">
            Up to {args.count} turns{args.page_cursor ? " · next page" : ""}
          </Text>
        </Stack>
      )),
      turns
    ),
  ],
]);
