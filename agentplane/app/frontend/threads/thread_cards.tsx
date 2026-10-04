import { Alert, Badge, Box, Button, Group, Paper, Stack, Text, type MantineColor } from "@mantine/core";
import { ItemKind, RecoveryDisposition } from "../../../protocol/event_pb";
import { type JSX, type ReactNode, useLayoutEffect, useRef, useState } from "react";

import { ClampedBlock, lineCount } from "../clamped_block";
import { InlineCode } from "../code_block";
import { COMMAND_MAX_HEIGHT_REM, CommandCallView, OutputBlock } from "../command_view";
import { HighlightedText } from "../json_view";
import { Markdown } from "../markdown";
import { RawSwitch } from "../raw_switch";
import { oneLine, parseCommandCall } from "./command_calls";
import { lifecyclePresentation } from "./history_rows";
import { EvidencePanel, EvidenceToggle } from "./thread_evidence";
import { RetainedDisclosure, useRetainedDisclosure } from "./retained_disclosures";
import { StepLine, type StepMark } from "./step_line";
import { useThreadSync, type Payload, type PayloadRef, type ThreadEntity } from "./thread_sync";

type ItemState = Extract<ThreadEntity["state"], { kind: number }>;

/** How a body renders: the agent's prose (assistant text, reasoning) as Markdown, tool arguments and
 * output as code (highlighted when it is JSON), and the operator's own input verbatim, as typed. */
type BodyFormat = "markdown" | "code" | "text";

export function Body({
  reference,
  format,
  streaming = false,
}: {
  reference: PayloadRef | null;
  format: BodyFormat;
  streaming?: boolean;
}): JSX.Element {
  if (!reference) return <Text c="dimmed">Body not observed</Text>;
  return <PayloadText reference={reference} format={format} streaming={streaming} />;
}

function PayloadText({
  reference,
  format,
  streaming,
}: {
  reference: PayloadRef;
  format: BodyFormat;
  streaming: boolean;
}): JSX.Element {
  return (
    <PayloadView reference={reference}>
      {(body) => <FormattedBody body={body} format={format} streaming={streaming} />}
    </PayloadView>
  );
}

/** A payload's body to `children` once it has arrived; until then, or if it stops arriving, what
 * says so. */
function PayloadView({
  reference,
  children,
}: {
  reference: PayloadRef;
  children: (body: string) => ReactNode;
}): JSX.Element {
  const { body, error, retry } = useThreadSync().usePayload(reference);
  return (
    <>
      {error && (
        <p role="alert">
          Payload synchronization stopped: {error} <button onClick={retry}>Retry payload synchronization</button>
        </p>
      )}
      {body === null ? (
        <Text c="dimmed" aria-busy={error === null}>
          Loading complete revision…
        </Text>
      ) : (
        children(body)
      )}
    </>
  );
}

/** A payload as it is loading, to `children`; `null` where there is no payload to load. Which hook
 * runs cannot depend on whether a reference exists, so the one that needs it is a component. */
function OptionalPayload({
  reference,
  children,
}: {
  reference: PayloadRef | null;
  children: (payload: Payload | null) => ReactNode;
}): JSX.Element {
  return reference ? <LoadedPayload reference={reference}>{children}</LoadedPayload> : <>{children(null)}</>;
}

function LoadedPayload({
  reference,
  children,
}: {
  reference: PayloadRef;
  children: (payload: Payload) => ReactNode;
}): JSX.Element {
  return <>{children(useThreadSync().usePayload(reference))}</>;
}

function FormattedBody({
  body,
  format,
  streaming,
}: {
  body: string;
  format: BodyFormat;
  streaming: boolean;
}): JSX.Element {
  switch (format) {
    case "markdown":
      return <Markdown source={body} streaming={streaming} />;
    case "code":
      return <HighlightedText text={body} />;
    case "text":
      return <VerbatimText text={body} />;
  }
}

export function VerbatimText({ text }: { text: string }): JSX.Element {
  return (
    <Text className="agentplane-verbatim" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {text}
    </Text>
  );
}

/** The operator's input, including provisional sends. Actions sit in the gutter before the bubble;
 * local text uses no event evidence until runner admission gives it an ordered Event. */
export function UserInputBubble({
  threadId,
  entity,
  commandId,
  text,
  status,
  statusColor = "dimmed",
  error,
  pending = false,
  phase,
  action,
}: {
  threadId?: string;
  entity?: ThreadEntity;
  commandId?: string;
  text?: string;
  status?: string;
  statusColor?: MantineColor;
  error?: string;
  pending?: boolean;
  phase?: "local" | "pending" | "failed" | "noop" | "confirmed";
  action?: { label: string; onClick: () => void };
}): JSX.Element {
  return (
    <Group
      className="agentplane-user-message-row"
      justify="flex-end"
      align="flex-start"
      gap="xs"
      wrap="nowrap"
      style={{ width: "100%" }}
      data-command-id={commandId ?? entity?.entityId}
    >
      {action && (
        <Button size="xs" variant="subtle" onClick={action.onClick} aria-label={action.label}>
          {action.label}
        </Button>
      )}
      {entity && <EvidenceToggle entity={entity} />}
      <Paper
        className="agentplane-user-bubble"
        data-message-phase={phase}
        data-has-action={action ? "true" : undefined}
        p="sm"
        style={pending ? { fontStyle: "italic", opacity: 0.6 } : undefined}
      >
        {status && (
          <Text size="xs" c={statusColor} mb="xs" role="status">
            {status}
          </Text>
        )}
        {error && (
          <Text size="xs" c="red" mb="xs" role="alert">
            {error}
          </Text>
        )}
        {entity ? (
          <Body reference={entity.inputRef} format="text" />
        ) : text !== undefined ? (
          <VerbatimText text={text} />
        ) : null}
        {entity && threadId && <EvidencePanel threadId={threadId} entity={entity} />}
      </Paper>
    </Group>
  );
}

function payloadDisclosureId(reference: PayloadRef): string {
  return `${reference.projection_epoch}:${reference.owner_id}:${reference.field}`;
}

function LazyBody({ label, ...body }: { label: string; reference: PayloadRef; format: BodyFormat }): JSX.Element {
  const id = payloadDisclosureId(body.reference);
  return (
    <RetainedDisclosure id={id} summary={label}>
      <Body {...body} />
    </RetainedDisclosure>
  );
}

function ReasoningPreview({
  reference,
  mark,
  status,
  open,
  overflows,
  setOpen,
  onOverflowChange,
}: {
  reference: PayloadRef;
  /** How the step stands, shown on its title while it is folded. */
  mark: StepMark | undefined;
  /** The same, as badges, once it is open. */
  status: ReactNode;
  open: boolean;
  overflows: boolean;
  setOpen: (open: boolean) => void;
  onOverflowChange: (overflows: boolean) => void;
}): JSX.Element {
  const { body, error, retry } = useThreadSync().usePayload(reference);
  const preview = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const element = preview.current;
    const content = element?.querySelector<HTMLElement>(".agentplane-markdown--single-line");
    if (body === null || !element || !content) {
      onOverflowChange(false);
      return;
    }
    const measure = () => {
      // The visible wrappers clip the line, which makes their scroll and Range bounds report only
      // the visible portion. Measure a hidden copy outside that layout at its intrinsic width.
      const measurement = content.cloneNode(true) as HTMLElement;
      const style = getComputedStyle(content);
      Object.assign(measurement.style, {
        position: "fixed",
        left: "-100000px",
        top: "0",
        width: "max-content",
        maxWidth: "none",
        overflow: "visible",
        textOverflow: "clip",
        whiteSpace: "nowrap",
        visibility: "hidden",
        pointerEvents: "none",
        font: style.font,
        lineHeight: style.lineHeight,
        letterSpacing: style.letterSpacing,
        wordSpacing: style.wordSpacing,
      });
      document.body.append(measurement);
      let contentWidth: number;
      try {
        contentWidth = measurement.getBoundingClientRect().width;
      } finally {
        measurement.remove();
      }
      onOverflowChange(contentWidth > element.clientWidth + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    observer.observe(content);
    return () => observer.disconnect();
  }, [body, onOverflowChange]);

  const expandable = overflows && body !== null && body.trim().length > 0;
  return (
    <StepLine
      title="Reasoning"
      mark={open && expandable ? undefined : mark}
      trailing={open && expandable ? status : undefined}
      preview={
        body === null ? (
          <Text component="span" c="dimmed" aria-busy={!error}>
            {error ? "Preview unavailable" : "Loading preview…"}
          </Text>
        ) : (
          <Markdown source={body} singleLine />
        )
      }
      previewRef={preview}
      aside={
        error && (
          <button className="agentplane-step-retry" onClick={retry} type="button">
            Retry
          </button>
        )
      }
      expandable={expandable}
      open={open}
      onOpenChange={setOpen}
    >
      {body !== null && <Markdown source={body} />}
    </StepLine>
  );
}

/** A sent message once the server has accepted it and is working on it, but before the harness's
 * own confirmed_input entity lands -- the moment the message is fully ordered in history (it has a
 * cursor) and no longer needs the composer's Retry/Dismiss affordances, so it can read as the
 * eventual bubble rather than as a command awaiting an outcome. */
export function pendingSentMessage(entity: ThreadEntity): boolean {
  return (
    entity.entityKind === "command" &&
    "outcome" in entity.state &&
    entity.state.operation === "submit_input" &&
    entity.state.outcome === "pending"
  );
}

/** What a recovery leaves unsaid about whether an item's content is in the model's context. */
function RecoveryNotes({ state, tool }: { state: ItemState; tool: boolean }): JSX.Element {
  return (
    <>
      {state.recovery === RecoveryDisposition.UNKNOWN && (
        <Text size="sm" c="dimmed" mb="xs" style={{ overflowWrap: "anywhere" }}>
          Whether this content remains in the model&apos;s context could not be determined.
          {state.recovery_reason && ` ${state.recovery_reason}`}
        </Text>
      )}
      {state.recovery === RecoveryDisposition.REVISED && (
        <Text size="sm" c="dimmed" mb="xs">
          Showing the content retained for continuation. Earlier observations are available in Evidence.
          {tool && " Recovery content does not establish a tool execution outcome."}
        </Text>
      )}
    </>
  );
}

/** Content the model no longer holds, folded behind a disclosure that says so. */
function DiscardedCard({ id, summary, children }: { id: string; summary: string; children: ReactNode }): JSX.Element {
  const [open] = useRetainedDisclosure(id);
  return (
    <CollapsibleCard open={open}>
      <RetainedDisclosure
        id={id}
        summary={
          <Text component="span" size="sm" c="dimmed">
            {summary}
            {" (not retained in model context)"}
          </Text>
        }
      >
        <Stack gap="xs" mt="xs">
          {children}
        </Stack>
      </RetainedDisclosure>
    </CollapsibleCard>
  );
}

/** A tool call as one line, like a reasoning step, opening to its arguments and output: a shell
 * command as highlighted shell, and anything else as the JSON it holds. Each is capped in height,
 * and a command's Raw switch shows its JSON in place of the shell. All of it is kept where it is
 * left, since rows leave the DOM as the history scrolls. */
function ToolCard({
  threadId,
  entity,
  state,
  live,
  discardedId,
}: {
  threadId: string;
  entity: ThreadEntity;
  state: ItemState;
  live: boolean;
  discardedId: string | null;
}): JSX.Element {
  const id = `${entity.projectionEpoch}:${entity.entityKind}:${entity.entityId}:tool`;
  const [open, setOpen] = useRetainedDisclosure(id);
  const [raw, setRaw] = useRetainedDisclosure(`${id}:raw`);
  const input = useRetainedDisclosure(`${id}:input`);
  const output = useRetainedDisclosure(`${id}:output`);
  const disclosable =
    entity.argumentsRef !== null || entity.outputRef !== null || entity.textRef !== null || state.recovery !== null;
  const opened = open && disclosable;
  // How a folded line shows what the badges below show once it is open.
  const mark: StepMark | undefined =
    state.tool_succeeded === false
      ? "failed"
      : state.completion === null && state.recovery === null
        ? live
          ? "streaming"
          : "incomplete"
        : undefined;
  // TODO: only if folded lines ever need optimizing. Opening a run reads every call's whole arguments to
  // draw its one-line summary (`PayloadShape.want` fetches whole bodies); a bounded summary computed in
  // the thread projection would spare a folded row the fetch.
  return (
    <OptionalPayload reference={entity.argumentsRef}>
      {(args) => {
        const argumentsBody = args?.body ?? null;
        const call = argumentsBody === null ? null : parseCommandCall(state.tool_name, argumentsBody);
        const rawSwitch = call && <RawSwitch raw={raw} onChange={setRaw} />;
        const detail = (
          <Stack gap="xs">
            <RecoveryNotes state={state} tool />
            {entity.textRef && <Body reference={entity.textRef} format="markdown" />}
            {args && argumentsBody === null && (
              <Text c="dimmed" aria-busy={!args.error}>
                Loading complete revision…
              </Text>
            )}
            {argumentsBody !== null &&
              (call && !raw ? (
                <CommandCallView
                  description={call.description}
                  command={call.command}
                  notes={call.notes}
                  expansion={input}
                />
              ) : (
                <div>
                  <Text size="xs" c="dimmed" mb={4}>
                    Arguments
                  </Text>
                  <ClampedBlock
                    maxHeightRem={COMMAND_MAX_HEIGHT_REM}
                    lines={lineCount(argumentsBody)}
                    expansion={input}
                  >
                    <HighlightedText text={argumentsBody} />
                  </ClampedBlock>
                </div>
              ))}
            {entity.outputRef && (
              <PayloadView reference={entity.outputRef}>
                {(text) => (
                  <OutputBlock
                    name={state.recovery === RecoveryDisposition.REVISED ? "Continuation output" : "Output"}
                    text={text}
                    expansion={output}
                  />
                )}
              </PayloadView>
            )}
          </Stack>
        );
        if (discardedId !== null) {
          return (
            <DiscardedCard id={discardedId} summary={`${state.tool_name || "Tool"}: discarded context`}>
              <Text size="sm" c="dimmed">
                Discarded context does not undo tool side effects or change its recorded execution outcome.
              </Text>
              <Group justify="space-between" wrap="nowrap">
                <Group gap="xs">
                  <ItemStatus items={[entity]} live={live} />
                </Group>
                <Group gap="xs" wrap="nowrap">
                  {rawSwitch}
                  <EvidenceToggle entity={entity} />
                </Group>
              </Group>
              {detail}
              <EvidencePanel threadId={threadId} entity={entity} />
            </DiscardedCard>
          );
        }
        return (
          <CollapsibleCard open={opened} stableInlineSize>
            <StepLine
              title={call?.label ?? (state.tool_name || "tool")}
              mark={opened ? undefined : mark}
              preview={
                args &&
                (argumentsBody === null ? (
                  <Text component="span" c="dimmed" aria-busy={!args.error}>
                    {args.error ? "Preview unavailable" : "Loading preview…"}
                  </Text>
                ) : call && !call.description ? (
                  <InlineCode text={call.summary} />
                ) : (
                  <Text component="span" ff={call ? undefined : "monospace"}>
                    {call?.summary ?? oneLine(argumentsBody)}
                  </Text>
                ))
              }
              trailing={<ItemStatus items={[entity]} live={live} titleMarked={!opened} />}
              aside={
                args?.error && (
                  <button className="agentplane-step-retry" onClick={args.retry} type="button">
                    Retry
                  </button>
                )
              }
              controls={rawSwitch}
              expandable={disclosable}
              open={open}
              onOpenChange={setOpen}
            >
              <Box mt="xs">{detail}</Box>
            </StepLine>
            <EvidenceToggle entity={entity} style={{ position: "absolute", top: 4, right: 4 }} />
            <EvidencePanel threadId={threadId} entity={entity} />
          </CollapsibleCard>
        );
      }}
    </OptionalPayload>
  );
}

export function EntityCard({
  threadId,
  entity,
  live,
}: {
  threadId: string;
  entity: ThreadEntity;
  live: boolean;
}): JSX.Element {
  // Computed unconditionally (hooks can't follow the entity-kind branches below): null, and so
  // always closed, for anything but a reasoning step with a body to disclose.
  const reasoningTextRef = "kind" in entity.state && entity.state.kind === ItemKind.REASONING ? entity.textRef : null;
  const [reasoningOpen, setReasoningOpen] = useRetainedDisclosure(
    reasoningTextRef && payloadDisclosureId(reasoningTextRef)
  );
  const [reasoningOverflows, setReasoningOverflows] = useState(false);
  const discardedId =
    "kind" in entity.state && entity.state.recovery === RecoveryDisposition.ABSENT
      ? `${entity.projectionEpoch}:${entity.entityId}:discarded`
      : null;
  if (entity.entityKind === "confirmed_input") {
    return <UserInputBubble threadId={threadId} entity={entity} phase="confirmed" />;
  }
  if (pendingSentMessage(entity)) {
    return (
      <UserInputBubble threadId={threadId} entity={entity} phase="pending" pending status="Saved · awaiting effect" />
    );
  }
  if (entity.entityKind === "lifecycle" && "observation" in entity.state) {
    const { label, prominent, diagnostic } = lifecyclePresentation(entity.state.observation, entity.state.event);
    const wrapped = { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;
    if (!prominent) {
      return (
        <Stack gap={0} style={{ position: "relative" }}>
          <Text c="dimmed">{label}</Text>
          <EvidenceToggle entity={entity} style={{ position: "absolute", top: 0, right: 0 }} />
          {diagnostic && (
            <Text c="dimmed" style={wrapped}>
              {diagnostic}
            </Text>
          )}
          <EvidencePanel threadId={threadId} entity={entity} />
        </Stack>
      );
    }
    return (
      <Alert color="red" title={label} role="alert" style={{ position: "relative" }}>
        <EvidenceToggle entity={entity} style={{ position: "absolute", top: 8, right: 8 }} />
        {diagnostic && (
          <Text size="sm" style={wrapped}>
            {diagnostic}
          </Text>
        )}
        <EvidencePanel threadId={threadId} entity={entity} />
      </Alert>
    );
  }
  if (entity.entityKind === "command" || entity.entityKind === "view_state") return <></>;
  if (!("kind" in entity.state)) return <></>;
  if (entity.state.kind === ItemKind.TOOL_CALL) {
    return <ToolCard threadId={threadId} entity={entity} state={entity.state} live={live} discardedId={discardedId} />;
  }
  const reasoning = entity.state.kind === ItemKind.REASONING;
  // A reasoning step that is still unfinished shows it on its title, as a tool call does; only a
  // recovery, which the title has no mark for, still takes a row of badges above it.
  const reasoningMark: StepMark | undefined =
    reasoning && entity.state.completion === null && entity.state.recovery === null
      ? live
        ? "streaming"
        : "incomplete"
      : undefined;
  const streamingText =
    entity.state.kind === ItemKind.ASSISTANT_TEXT &&
    entity.state.completion === null &&
    entity.state.recovery === null &&
    live;
  const body = (
    <>
      {(!reasoning && entity.state.completion === null && !streamingText) || entity.state.recovery !== null ? (
        <Group justify="space-between" mb="xs" wrap="nowrap">
          <Group gap="xs">
            <ItemStatus items={[entity]} live={live} />
          </Group>
          <EvidenceToggle entity={entity} />
        </Group>
      ) : (
        <EvidenceToggle entity={entity} style={{ position: "absolute", top: 4, right: 4 }} />
      )}
      <RecoveryNotes state={entity.state} tool={false} />
      {reasoning ? (
        entity.textRef ? (
          discardedId === null ? (
            <ReasoningPreview
              reference={entity.textRef}
              mark={reasoningMark}
              status={<ItemStatus items={[entity]} live={live} />}
              open={reasoningOpen}
              overflows={reasoningOverflows}
              setOpen={setReasoningOpen}
              onOverflowChange={setReasoningOverflows}
            />
          ) : (
            <LazyBody label="Reasoning" reference={entity.textRef} format="markdown" />
          )
        ) : (
          <Text c="dimmed">Reasoning</Text>
        )
      ) : (
        entity.textRef && <Body reference={entity.textRef} format="markdown" streaming={streamingText} />
      )}
      <EvidencePanel threadId={threadId} entity={entity} />
    </>
  );
  if (discardedId !== null) {
    return (
      <DiscardedCard id={discardedId} summary="Discarded output">
        {body}
      </DiscardedCard>
    );
  }
  // Assistant text carries no role label and no card: it reads as the reply by position, across
  // from the user's right-aligned bubble. A standalone reasoning step is boxed only once its own
  // disclosure opens, like a collapsed run -- collapsed, it is already just the one "Reasoning"
  // line.
  if (reasoning)
    return (
      <CollapsibleCard open={reasoningOpen && reasoningOverflows} stableInlineSize>
        {body}
      </CollapsibleCard>
    );
  return <Box style={{ position: "relative" }}>{body}</Box>;
}

/** Whether any of `items` is unfinished -- streaming while `live`, otherwise never completed in
 * the retained history -- and whether any tool call among them failed. */
export function ItemStatus({
  items,
  live,
  titleMarked = false,
}: {
  items: ThreadEntity[];
  live: boolean;
  /** Leave out the Failed and Streaming/Incomplete badges, for a line whose title shows them. */
  titleMarked?: boolean;
}): JSX.Element {
  const states = items.flatMap((item) => ("kind" in item.state ? [item.state] : []));
  const unfinished = states.some((state) => state.completion === null && state.recovery === null);
  const interrupted = states.some((state) => state.completion === null && state.recovery !== null);
  const recoveries = [...new Set(states.flatMap((state) => (state.recovery === null ? [] : [state.recovery])))];
  return (
    <>
      {!titleMarked && unfinished && (
        <Badge role="img" aria-label={live ? "Streaming" : "Incomplete"}>
          {live ? "Streaming" : "Incomplete"}
        </Badge>
      )}
      {interrupted && (
        <Badge role="img" aria-label="Interrupted">
          Interrupted
        </Badge>
      )}
      {recoveries.map((recovery) => {
        const { label, color } = recoveryPresentation(recovery);
        return (
          <Badge key={recovery} color={color} role="img" aria-label={label}>
            {label}
          </Badge>
        );
      })}
      {states.some((state) => state.recovery !== null && state.tool_succeeded === true) && (
        <Badge color="green" role="img" aria-label="Succeeded">
          Succeeded
        </Badge>
      )}
      {!titleMarked && states.some((state) => state.tool_succeeded === false) && (
        <Badge color="red" role="img" aria-label="Failed">
          Failed
        </Badge>
      )}
    </>
  );
}

function recoveryPresentation(recovery: number): { label: string; color: string } {
  switch (recovery) {
    case RecoveryDisposition.RETAINED:
      return { label: "Retained in context", color: "gray" };
    case RecoveryDisposition.ABSENT:
      return { label: "Not retained in context", color: "gray" };
    case RecoveryDisposition.REVISED:
      return { label: "Revised for continuation", color: "blue" };
    default:
      return { label: "Retention unknown", color: "yellow" };
  }
}

/** The collapsible shell a run, a lifecycle group, or a standalone reasoning step shares: collapsed,
 * it shows nothing but its one line -- a run/group's `summary`, or reasoning's own "Reasoning"
 * disclosure -- so the full card padding and border its opened content warrants would only pad out
 * that one line. */
export function CollapsibleCard({
  open,
  children,
  stableInlineSize = false,
}: {
  open: boolean;
  children: ReactNode;
  stableInlineSize?: boolean;
}): JSX.Element {
  return (
    <Paper
      data-open={open}
      // A folded step line is one line, so it needs little more than the line.
      p={open ? "sm" : stableInlineSize ? 2 : "xs"}
      withBorder={open && !stableInlineSize}
      style={{
        position: "relative",
        ...(stableInlineSize && {
          paddingInline: "var(--mantine-spacing-xs)",
          boxShadow: open ? "inset 0 0 0 1px var(--mantine-color-default-border)" : undefined,
        }),
      }}
    >
      {children}
    </Paper>
  );
}
