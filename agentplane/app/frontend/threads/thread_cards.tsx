import { Alert, Badge, Box, Button, Group, Paper, Stack, Text, type MantineColor } from "@mantine/core";
import { ItemKind, RecoveryDisposition } from "../../../protocol/event_pb";
import { type JSX, type ReactNode, useLayoutEffect, useRef, useState } from "react";

import { HighlightedText } from "../json_view";
import { Markdown } from "../markdown";
import { lifecyclePresentation } from "./history_rows";
import { EvidencePanel, EvidenceToggle } from "./thread_evidence";
import { RetainedDisclosure, useRetainedDisclosure } from "./retained_disclosures";
import { useThreadSync, type PayloadRef, type ThreadEntity } from "./thread_sync";

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
  const { body, error, retry } = useThreadSync().usePayload(reference);
  return (
    <>
      {error && (
        <p role="alert">
          Payload synchronization stopped: {error} <button onClick={retry}>Retry payload synchronization</button>
        </p>
      )}
      {body === null ? (
        <Text c="dimmed">Loading complete revision…</Text>
      ) : (
        <FormattedBody body={body} format={format} streaming={streaming} />
      )}
    </>
  );
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
  open,
  overflows,
  setOpen,
  onOverflowChange,
}: {
  reference: PayloadRef;
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

  const summary = (
    <div className="agentplane-reasoning-summary">
      <Text component="span" className="agentplane-reasoning-title" c="dimmed">
        Reasoning
      </Text>
      <div className="agentplane-reasoning-preview" ref={preview}>
        {body === null ? (
          <Text component="span" c="dimmed">
            {error ? "Preview unavailable" : "Loading preview…"}
          </Text>
        ) : (
          <Markdown source={body} singleLine />
        )}
      </div>
    </div>
  );

  return (
    <div className="agentplane-reasoning-row">
      {overflows && body !== null && body.trim().length > 0 ? (
        <details
          className="agentplane-reasoning-details"
          open={open}
          onToggle={(event) => setOpen(event.currentTarget.open)}
        >
          <summary>{summary}</summary>
          {open && <Markdown source={body} />}
        </details>
      ) : (
        <div className="agentplane-reasoning-static">{summary}</div>
      )}
      {error && (
        <button className="agentplane-reasoning-retry" onClick={retry} type="button">
          Retry
        </button>
      )}
    </div>
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
  const [discardedOpen] = useRetainedDisclosure(discardedId);
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
  const tool = entity.state.kind === ItemKind.TOOL_CALL;
  const reasoning = entity.state.kind === ItemKind.REASONING;
  const streamingText =
    entity.state.kind === ItemKind.ASSISTANT_TEXT &&
    entity.state.completion === null &&
    entity.state.recovery === null &&
    live;
  const body = (
    <>
      {tool || (entity.state.completion === null && !streamingText) || entity.state.recovery !== null ? (
        <Group justify="space-between" mb="xs" wrap="nowrap">
          <Group gap="xs">
            {tool && <Badge variant="light">{entity.state.tool_name || "tool"}</Badge>}
            <ItemStatus items={[entity]} live={live} />
          </Group>
          <EvidenceToggle entity={entity} />
        </Group>
      ) : (
        <EvidenceToggle entity={entity} style={{ position: "absolute", top: 4, right: 4 }} />
      )}
      {entity.state.recovery === RecoveryDisposition.UNKNOWN && (
        <Text size="sm" c="dimmed" mb="xs" style={{ overflowWrap: "anywhere" }}>
          Whether this content remains in the model's context could not be determined.
          {entity.state.recovery_reason && ` ${entity.state.recovery_reason}`}
        </Text>
      )}
      {entity.state.recovery === RecoveryDisposition.REVISED && (
        <Text size="sm" c="dimmed" mb="xs">
          Showing the content retained for continuation. Earlier observations are available in Evidence.
          {tool && " Recovery content does not establish a tool execution outcome."}
        </Text>
      )}
      {reasoning ? (
        entity.textRef ? (
          discardedId === null ? (
            <ReasoningPreview
              reference={entity.textRef}
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
      {entity.argumentsRef && <LazyBody label="Arguments" reference={entity.argumentsRef} format="code" />}
      {entity.outputRef && (
        <LazyBody
          label={entity.state.recovery === RecoveryDisposition.REVISED ? "Continuation output" : "Output"}
          reference={entity.outputRef}
          format="code"
        />
      )}
      <EvidencePanel threadId={threadId} entity={entity} />
    </>
  );
  if (discardedId !== null) {
    return (
      <CollapsibleCard open={discardedOpen}>
        <RetainedDisclosure
          id={discardedId}
          summary={
            <Text component="span" size="sm" c="dimmed">
              {tool ? `${entity.state.tool_name || "Tool"}: discarded context` : "Discarded output"}
              {" (not retained in model context)"}
            </Text>
          }
        >
          <Stack gap="xs" mt="xs">
            {tool && (
              <Text size="sm" c="dimmed">
                Discarded context does not undo tool side effects or change its recorded execution outcome.
              </Text>
            )}
            {body}
          </Stack>
        </RetainedDisclosure>
      </CollapsibleCard>
    );
  }
  // Assistant text carries no role label and no card: it reads as the reply by position, across
  // from the user's right-aligned bubble. A tool call is boxed unconditionally, labelled by its
  // tool; a standalone reasoning step is boxed only once its own disclosure opens, like a
  // collapsed run -- collapsed, it is already just the one "Reasoning" line.
  if (tool) {
    return (
      <Paper p="sm" withBorder style={{ position: "relative" }}>
        {body}
      </Paper>
    );
  }
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
export function ItemStatus({ items, live }: { items: ThreadEntity[]; live: boolean }): JSX.Element {
  const states = items.flatMap((item) => ("kind" in item.state ? [item.state] : []));
  const unfinished = states.some((state) => state.completion === null && state.recovery === null);
  const interrupted = states.some((state) => state.completion === null && state.recovery !== null);
  const recoveries = [...new Set(states.flatMap((state) => (state.recovery === null ? [] : [state.recovery])))];
  return (
    <>
      {unfinished && (
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
      {states.some((state) => state.tool_succeeded === false) && (
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
      p={open ? "sm" : "xs"}
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
