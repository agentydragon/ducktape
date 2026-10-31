import { Alert, Badge, Box, Button, Group, Stack, Text, type MantineColor } from "@mantine/core";
import { ItemKind, RecoveryDisposition } from "../../../proto/event_pb";
import { type CSSProperties, type JSX, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";

import { ClampedBlock, lineCount } from "../../../../util/frontend/clamped_block";
import { InlineCode } from "../../../../util/frontend/code_block";
import { DisclosureBody, DisclosureCard } from "../../../../util/frontend/disclosure";
import { UserMessage, VerbatimText } from "../../../../util/frontend/message";
import { SHELL_COMMAND_MAX_HEIGHT_REM, ShellCommandView, OutputBlock } from "../shell_command_view";
import { HighlightedText } from "../json_view";
import { Markdown } from "../../../../util/frontend/markdown";
import { RetainedDisclosure, useRetainedDisclosure } from "../../../../util/frontend/retained_disclosures";
import { RawSwitch } from "../raw_switch";
import { oneLine, parseShellCall, type ShellCall } from "./shell_calls";
import { CommandProgress } from "./command_progress";
import { lifecyclePresentation } from "./history_rows";
import { EvidencePanel, EvidenceToggle } from "./thread_evidence";
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
  fallbackText,
  onBodyLoaded,
}: {
  reference: PayloadRef | null;
  format: BodyFormat;
  streaming?: boolean;
  fallbackText?: string;
  onBodyLoaded?: () => void;
}): JSX.Element {
  if (!reference) {
    return fallbackText !== undefined ? (
      <VerbatimText text={fallbackText} />
    ) : (
      <Text c="dimmed">Body not observed</Text>
    );
  }
  return (
    <PayloadText
      reference={reference}
      format={format}
      streaming={streaming}
      fallbackText={fallbackText}
      onBodyLoaded={onBodyLoaded}
    />
  );
}

function PayloadText({
  reference,
  format,
  streaming,
  fallbackText,
  onBodyLoaded,
}: {
  reference: PayloadRef;
  format: BodyFormat;
  streaming: boolean;
  fallbackText?: string;
  onBodyLoaded?: () => void;
}): JSX.Element {
  return (
    <PayloadView reference={reference} fallbackText={fallbackText} onBodyLoaded={onBodyLoaded}>
      {(body) => <FormattedBody body={body} format={format} streaming={streaming} />}
    </PayloadView>
  );
}

/** A payload's body to `children` once it has arrived; until then, or if it stops arriving, what
 * says so. */
function PayloadView({
  reference,
  children,
  fallbackText,
  onBodyLoaded,
}: {
  reference: PayloadRef;
  children: (body: string) => ReactNode;
  fallbackText?: string;
  onBodyLoaded?: () => void;
}): JSX.Element {
  const { body, error, retry } = useThreadSync().usePayload(reference);
  const loadedReference = `${reference.projection_epoch}:${reference.owner_id}:${reference.field}:${reference.generation}:${reference.revision_cursor}`;
  const reportedLoadedReference = useRef<string | null>(null);
  useEffect(() => {
    if (body !== null && reportedLoadedReference.current !== loadedReference) {
      reportedLoadedReference.current = loadedReference;
      onBodyLoaded?.();
    }
  }, [body, loadedReference, onBodyLoaded]);
  return (
    <>
      {error && (
        <p role="alert">
          Payload synchronization stopped: {error}. Retrying automatically.{" "}
          <button onClick={retry}>Retry payload synchronization now</button>
        </p>
      )}
      {body === null && fallbackText !== undefined ? (
        <VerbatimText text={fallbackText} />
      ) : body === null ? (
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

/** The operator's input, including provisional sends. Progress and action sit in the
 * gutter before the bubble, so the bubble reads the same when they go; local text
 * uses no event evidence until runner admission gives it an ordered Event. */
export function UserInputBubble({
  threadId,
  entity,
  commandId,
  text,
  fallbackText,
  onFallbackLoaded,
  progress,
  pending = false,
  phase,
  action,
}: {
  threadId?: string;
  entity?: ThreadEntity;
  commandId?: string;
  text?: string;
  fallbackText?: string;
  onFallbackLoaded?: () => void;
  progress?: JSX.Element;
  pending?: boolean;
  phase?: "local" | "pending" | "failed" | "noop" | "confirmed";
  action?: { label: string; onClick: () => void };
}): JSX.Element {
  return (
    <Stack gap="xs" style={{ width: "100%" }}>
      <UserMessage
        rowAttributes={{ "data-command-id": commandId ?? entity?.entityId }}
        bubbleAttributes={{
          className: "agentplane-evidence-owner",
          "data-message-phase": phase,
          "data-has-action": action ? "true" : undefined,
          "data-has-aside": progress || action ? "true" : undefined,
        }}
        pending={pending}
        aside={
          progress || action ? (
            <>
              {progress}
              {action && (
                <Button size="xs" variant="subtle" onClick={action.onClick} aria-label={action.label}>
                  {action.label}
                </Button>
              )}
            </>
          ) : undefined
        }
        overlay={entity && <EvidenceToggle entity={entity} placement={progress || action ? "overlay" : "bubble"} />}
      >
        {entity ? (
          <Body
            reference={entity.inputRef}
            format="text"
            fallbackText={fallbackText}
            onBodyLoaded={fallbackText === undefined ? undefined : onFallbackLoaded}
          />
        ) : text !== undefined ? (
          <VerbatimText text={text} />
        ) : null}
      </UserMessage>
      {entity && threadId && <EvidencePanel threadId={threadId} entity={entity} />}
    </Stack>
  );
}

function payloadDisclosureId(reference: PayloadRef): string {
  return `${reference.projection_epoch}:${reference.owner_id}:${reference.field}`;
}

function LazyBody({ label, ...body }: { label: string; reference: PayloadRef; format: BodyFormat }): JSX.Element {
  const id = payloadDisclosureId(body.reference);
  return (
    <RetainedDisclosure id={id} summary={label}>
      <DisclosureBody>
        <Body {...body} />
      </DisclosureBody>
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
  summaryAside,
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
  summaryAside?: ReactNode;
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
          <Text component="span" size="sm" c="dimmed" aria-busy={!error}>
            {error ? "Preview unavailable" : "Loading preview…"}
          </Text>
        ) : (
          <Markdown source={body} singleLine size="sm" />
        )
      }
      previewRef={preview}
      summaryAside={summaryAside}
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
      {body !== null && (
        <DisclosureBody>
          <Markdown source={body} />
        </DisclosureBody>
      )}
    </StepLine>
  );
}

/** A sent message once the server has accepted it and is working on it, but before the harness's
 * own confirmed_input entity lands -- the moment the message is fully ordered in history (it has a
 * cursor) and no longer needs the composer's delivery affordances, so it can read as the
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
    <DisclosureCard open={open}>
      <RetainedDisclosure
        id={id}
        summary={
          <Text component="span" size="sm" c="dimmed">
            {summary}
            {" (not retained in model context)"}
          </Text>
        }
      >
        <Stack gap={0} className="agentplane-evidence-owner">
          {children}
        </Stack>
      </RetainedDisclosure>
    </DisclosureCard>
  );
}

function ShellCallExecutionMetadata({ call }: { call: ShellCall }): JSX.Element | null {
  if (!call.invocation && !call.cwd) return null;
  return (
    <Text size="xs" c="dimmed" style={{ flex: "1 1 12rem", minWidth: 0, overflowWrap: "anywhere" }}>
      {call.invocation && (
        <>
          Run with <InlineCode text={call.invocation} />
        </>
      )}
      {call.cwd && (
        <>
          {call.invocation ? " · " : ""}cwd <InlineCode text={call.cwd} />
        </>
      )}
    </Text>
  );
}

function ShellCallControls({
  call,
  raw,
  onRawChange,
  hasOutput,
  fullWidth = true,
  children,
}: {
  call: ShellCall;
  raw: boolean;
  onRawChange: (raw: boolean) => void;
  hasOutput: boolean;
  fullWidth?: boolean;
  children?: ReactNode;
}): JSX.Element {
  return (
    <Group
      gap="xs"
      justify="space-between"
      align="center"
      wrap="wrap"
      style={fullWidth ? { width: "100%" } : undefined}
    >
      <ShellCallExecutionMetadata call={call} />
      <Group gap="xs" wrap="nowrap" style={{ marginInlineStart: "auto" }}>
        <RawSwitch
          raw={raw}
          onChange={onRawChange}
          ariaLabel={hasOutput ? "Show raw arguments and output" : "Show raw arguments"}
        />
        {children}
      </Group>
    </Group>
  );
}

/** A tool call as one line, like a reasoning step, opening to its arguments and output: a shell
 * command as highlighted shell, and anything else as the JSON it holds. Each is capped in height,
 * and a command's Raw switch shows the recorded arguments and output. All of it is kept where it is
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
  const outputDisclosure = useRetainedDisclosure(`${id}:output:disclosure`, true);
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
        const call = argumentsBody === null ? null : parseShellCall(state.tool_name, argumentsBody);
        const detail = (
          <Stack gap={0}>
            {(state.recovery === RecoveryDisposition.UNKNOWN ||
              state.recovery === RecoveryDisposition.REVISED ||
              entity.textRef ||
              (args && argumentsBody === null)) && (
              <DisclosureBody>
                <RecoveryNotes state={state} tool />
                {entity.textRef && <Body reference={entity.textRef} format="markdown" />}
                {args && argumentsBody === null && (
                  <Text c="dimmed" aria-busy={!args.error}>
                    Loading complete revision…
                  </Text>
                )}
              </DisclosureBody>
            )}
            {argumentsBody !== null &&
              (call && !raw ? (
                <ShellCommandView command={call.command} notes={call.notes} expansion={input} />
              ) : (
                <div>
                  <ClampedBlock
                    maxHeightRem={SHELL_COMMAND_MAX_HEIGHT_REM}
                    lines={lineCount(argumentsBody)}
                    label="Arguments"
                    header={
                      <Text size="sm" c="dimmed">
                        Arguments
                      </Text>
                    }
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
                    raw={raw}
                    expansion={output}
                    disclosure={outputDisclosure}
                  />
                )}
              </PayloadView>
            )}
          </Stack>
        );
        if (discardedId !== null) {
          return (
            <DiscardedCard id={discardedId} summary={`${state.tool_name || "Tool"}: discarded context`}>
              <DisclosureBody>
                <Text size="sm" c="dimmed">
                  Discarded context does not undo tool side effects or change its recorded execution outcome.
                </Text>
                <Group justify="space-between" wrap="wrap">
                  <Group gap="xs">{itemStatus([entity], live)}</Group>
                  {call ? (
                    <ShellCallControls
                      call={call}
                      raw={raw}
                      onRawChange={setRaw}
                      hasOutput={entity.outputRef !== null}
                      fullWidth={false}
                    >
                      <EvidenceToggle entity={entity} placement="inline" />
                    </ShellCallControls>
                  ) : (
                    <Group gap="xs" wrap="nowrap">
                      <EvidenceToggle entity={entity} placement="inline" />
                    </Group>
                  )}
                </Group>
              </DisclosureBody>
              {detail}
              <EvidencePanel threadId={threadId} entity={entity} />
            </DiscardedCard>
          );
        }
        return (
          <DisclosureCard open={opened} className="agentplane-evidence-owner">
            <StepLine
              title={call?.label ?? (state.tool_name || "tool")}
              mark={opened ? undefined : mark}
              preview={
                args &&
                (argumentsBody === null ? (
                  <Text component="span" size="sm" c="dimmed" aria-busy={!args.error}>
                    {args.error ? "Preview unavailable" : "Loading preview…"}
                  </Text>
                ) : call && !call.description ? (
                  <InlineCode text={call.summary} language="bash" />
                ) : (
                  <Text component="span" size="sm" ff={call ? undefined : "monospace"}>
                    {call?.summary ?? oneLine(argumentsBody)}
                  </Text>
                ))
              }
              trailing={itemStatus([entity], live, !opened, opened)}
              aside={
                args?.error && (
                  <button className="agentplane-step-retry" onClick={args.retry} type="button">
                    Retry
                  </button>
                )
              }
              controls={
                call && (
                  <ShellCallControls call={call} raw={raw} onRawChange={setRaw} hasOutput={entity.outputRef !== null} />
                )
              }
              summaryAside={<EvidenceToggle entity={entity} placement="inline" />}
              expandable={disclosable}
              open={open}
              onOpenChange={setOpen}
            >
              {detail}
            </StepLine>
            <EvidencePanel threadId={threadId} entity={entity} />
          </DisclosureCard>
        );
      }}
    </OptionalPayload>
  );
}

export function EntityCard({
  threadId,
  entity,
  live,
  fallbackText,
  onFallbackLoaded,
}: {
  threadId: string;
  entity: ThreadEntity;
  live: boolean;
  fallbackText?: string;
  onFallbackLoaded?: () => void;
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
    return (
      <UserInputBubble
        threadId={threadId}
        entity={entity}
        fallbackText={fallbackText}
        onFallbackLoaded={onFallbackLoaded}
        phase="confirmed"
      />
    );
  }
  if (pendingSentMessage(entity)) {
    return (
      <UserInputBubble
        threadId={threadId}
        entity={entity}
        fallbackText={fallbackText}
        onFallbackLoaded={onFallbackLoaded}
        phase="pending"
        pending
        progress={<CommandProgress stage="admitted" subject="input" />}
      />
    );
  }
  if (entity.entityKind === "lifecycle" && "observation" in entity.state) {
    const { label, prominent, diagnostic } = lifecyclePresentation(entity.state.observation, entity.state.event);
    const wrapped = { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } as const;
    if (!prominent) {
      return (
        <Stack gap={0} className="agentplane-evidence-owner">
          <Text c="dimmed">{label}</Text>
          <EvidenceToggle
            entity={entity}
            placement="overlay"
            style={{ "--agentplane-evidence-overlay-inset": "0px" } as CSSProperties}
          />
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
      <Alert color="red" role="alert" className="agentplane-evidence-owner">
        <EvidenceToggle
          entity={entity}
          placement="overlay"
          style={{ "--agentplane-evidence-overlay-inset": "var(--mantine-spacing-xs)" } as CSSProperties}
        />
        <Text size="sm" style={wrapped}>
          <Text span fw={700} c="var(--alert-color)">
            {label}
          </Text>
          {diagnostic && ` ${diagnostic}`}
        </Text>
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
  const status = itemStatus([entity], live);
  const evidenceToggleInReasoningStep = reasoningTextRef !== null && discardedId === null;
  const recoveryDetails = (
    <>
      {status &&
        ((!reasoning && entity.state.completion === null && !streamingText) || entity.state.recovery !== null) && (
          <Group gap="xs" mb="xs">
            {status}
          </Group>
        )}
      <RecoveryNotes state={entity.state} tool={false} />
    </>
  );
  const body = (
    <>
      {!evidenceToggleInReasoningStep && <EvidenceToggle entity={entity} placement="overlay" />}
      {discardedId !== null ? <DisclosureBody>{recoveryDetails}</DisclosureBody> : recoveryDetails}
      {reasoning ? (
        entity.textRef ? (
          discardedId === null ? (
            <ReasoningPreview
              reference={entity.textRef}
              mark={reasoningMark}
              status={itemStatus([entity], live, false, true)}
              open={reasoningOpen}
              overflows={reasoningOverflows}
              setOpen={setReasoningOpen}
              onOverflowChange={setReasoningOverflows}
              summaryAside={
                evidenceToggleInReasoningStep ? <EvidenceToggle entity={entity} placement="inline" /> : undefined
              }
            />
          ) : (
            <LazyBody label="Reasoning" reference={entity.textRef} format="markdown" />
          )
        ) : (
          <Text size="sm" c="dimmed">
            Reasoning
          </Text>
        )
      ) : (
        entity.textRef &&
        (discardedId !== null ? (
          <DisclosureBody>
            <Body reference={entity.textRef} format="markdown" streaming={streamingText} />
          </DisclosureBody>
        ) : (
          <Body reference={entity.textRef} format="markdown" streaming={streamingText} />
        ))
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
      <DisclosureCard open={reasoningOpen && reasoningOverflows} className="agentplane-evidence-owner">
        {body}
      </DisclosureCard>
    );
  return <Box className="agentplane-evidence-owner">{body}</Box>;
}

interface StatusBadge {
  label: string;
  color?: MantineColor;
}

function isShellCall(toolName: string): boolean {
  // Claude and Codex name the same kind of call differently in their native protocols.
  return toolName === "Bash" || toolName === "commandExecution";
}

/** A badge says how items differ from the ordinary case -- ran to completion, still in the model's
 * context, no failure -- which wears none. */
function statusBadges(items: ThreadEntity[], live: boolean, titleMarked: boolean): StatusBadge[] {
  const states = items.flatMap((item) => ("kind" in item.state ? [item.state] : []));
  const badges: StatusBadge[] = [
    ...(!titleMarked && states.some((state) => state.completion === null && state.recovery === null)
      ? [{ label: live ? "Streaming" : "Incomplete" }]
      : []),
    ...(states.some((state) => state.completion === null && state.recovery !== null) ? [{ label: "Interrupted" }] : []),
    ...states.flatMap((state) => (state.recovery === null ? [] : (recoveryBadge(state.recovery) ?? []))),
    ...(!titleMarked &&
    states.some((state) => state.tool_succeeded === false && !(items.length > 1 && isShellCall(state.tool_name)))
      ? [{ label: "Failed", color: "red" }]
      : []),
  ];
  return [...new Map(badges.map((badge) => [badge.label, badge])).values()];
}

/** The badges for `items`, or `null` when none is out of the ordinary, so the caller can leave out
 * the row or gap it would hold them in. Unfinished items are streaming while `live`, otherwise
 * never completed in the retained history.
 *
 * `titleMarked` leaves out the Failed and Streaming/Incomplete badges, for a line whose title shows them. */
function compactStatusLabel(label: string): string {
  switch (label) {
    case "Revised for continuation":
      return "Revised";
    case "Retention unknown":
      return "Unknown";
    default:
      return label;
  }
}

export function itemStatus(
  items: ThreadEntity[],
  live: boolean,
  titleMarked = false,
  compactLabels = false
): JSX.Element | null {
  const badges = statusBadges(items, live, titleMarked);
  return badges.length === 0 ? null : (
    <>
      {badges.map(({ label, color }) => (
        <Badge key={label} color={color} role="img" aria-label={label} title={label}>
          {compactLabels ? compactStatusLabel(label) : label}
        </Badge>
      ))}
    </>
  );
}

/** `RETAINED` is what a recovery ordinarily leaves, so it has no badge. */
function recoveryBadge(recovery: number): StatusBadge | null {
  switch (recovery) {
    case RecoveryDisposition.RETAINED:
      return null;
    case RecoveryDisposition.ABSENT:
      return { label: "Not retained in context", color: "gray" };
    case RecoveryDisposition.REVISED:
      return { label: "Revised for continuation", color: "blue" };
    default:
      return { label: "Retention unknown", color: "yellow" };
  }
}
