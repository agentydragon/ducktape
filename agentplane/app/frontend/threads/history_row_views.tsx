import { Box, Flex, Stack, Text } from "@mantine/core";
import { type JSX, type ReactNode, useCallback, useEffect, useState } from "react";

import { Markdown } from "../../../../util/frontend/markdown";
import { type ThreadEntity, useThreadSync } from "./thread_sync";
import {
  summarizeLifecycleGroup,
  summarizeRun,
  summarizeSetup,
  setupProgress,
  type HistoryRow,
  type RunContent,
} from "./history_rows";
import { RetainedDisclosure, useRetainedDisclosure } from "../../../../util/frontend/retained_disclosures";
import { EntityCard, itemStatus, pendingSentMessage } from "./thread_cards";
import { StepLine } from "./step_line";
import { ProjectedControlCommand } from "./thread_commands";
import { DisclosureCard } from "../../../../util/frontend/disclosure";

/** A group of history rows, with the disclosure frame shared by runs and lifecycle groups. */
function CollapsibleRows({
  id,
  summary,
  steps,
  children,
}: {
  id: string;
  summary: ReactNode;
  /** Whether the rows are one-line steps, packed together, rather than cards. */
  steps: boolean;
  children: ReactNode;
}): JSX.Element {
  const [open] = useRetainedDisclosure(id);
  return (
    <DisclosureCard open={open}>
      <RetainedDisclosure id={id} summary={summary}>
        <Stack gap={steps ? 0 : "xs"} className={steps ? "agentplane-run-steps" : undefined}>
          {children}
        </Stack>
      </RetainedDisclosure>
    </DisclosureCard>
  );
}

function EntityCards({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  return (
    <>
      {entities.map((entity) => (
        <EntityCard key={entity.entityId} threadId={threadId} entity={entity} live={live(entity)} />
      ))}
    </>
  );
}

function ReasoningSummaryPart({
  entityId,
  reference,
  onText,
}: {
  entityId: string;
  reference: NonNullable<ThreadEntity["textRef"]>;
  onText: (entityId: string, text: string) => void;
}): null {
  const { body } = useThreadSync().usePayload(reference);
  useEffect(() => onText(entityId, body ?? ""), [body, entityId, onText]);
  return null;
}

function ReasoningGroup({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: [ThreadEntity, ...ThreadEntity[]];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  const id = `${first.projectionEpoch}:${first.entityKind}:${first.entityId}:reasoning-group`;
  const [open, setOpen] = useRetainedDisclosure(id);
  const [texts, setTexts] = useState<ReadonlyMap<string, string>>(() => new Map());
  const onText = useCallback((entityId: string, text: string) => {
    setTexts((previous) => {
      if (previous.get(entityId) === text) return previous;
      const next = new Map(previous);
      next.set(entityId, text);
      return next;
    });
  }, []);
  const summary = entities.map((entity) => texts.get(entity.entityId) ?? "").join("");

  return (
    <DisclosureCard open={open}>
      <StepLine
        title="Reasoning"
        preview={
          <>
            <Markdown source={summary} singleLine size="sm" />
            {entities.map(
              (entity) =>
                entity.textRef && (
                  <ReasoningSummaryPart
                    key={entity.entityId}
                    entityId={entity.entityId}
                    reference={entity.textRef}
                    onText={onText}
                  />
                )
            )}
          </>
        }
        expandable
        open={open}
        onOpenChange={setOpen}
      >
        <Stack gap={0} className="agentplane-run-steps">
          <EntityCards threadId={threadId} entities={entities} live={live} />
        </Stack>
      </StepLine>
    </DisclosureCard>
  );
}

function RunContentView({
  content,
  threadId,
  live,
}: {
  content: RunContent;
  threadId: string;
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  if (content.kind === "item")
    return <EntityCard threadId={threadId} entity={content.entity} live={live(content.entity)} />;
  if (content.entities.length === 1)
    return <EntityCard threadId={threadId} entity={content.entities[0]} live={live(content.entities[0])} />;
  return <ReasoningGroup threadId={threadId} entities={content.entities} live={live} />;
}

function RunView({
  threadId,
  entities,
  contents,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  contents: RunContent[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:run`}
      summary={
        <Flex component="span" display="inline-flex" gap="xs" align="center" wrap="wrap">
          <Text size="sm" c="dimmed">
            {summarizeRun(entities)}
          </Text>
          {itemStatus(entities, entities.some(live))}
        </Flex>
      }
      steps
    >
      {contents.map((content) => (
        <RunContentView
          key={content.kind === "item" ? content.entity.entityId : content.entities[0].entityId}
          content={content}
          threadId={threadId}
          live={live}
        />
      ))}
    </CollapsibleRows>
  );
}

/** Consecutive mundane lifecycle observations (turn started, harness started, an ordinary turn
 * completion, ...), collapsed to their comma-joined labels. Expanding reveals each one exactly as
 * it reads standing alone, evidence toggle included. */
function LifecycleGroupView({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:lifecycle`}
      summary={
        // Unlike RunView's inline-flex Flex, a plain Text defaults to a block <p> -- inside the
        // Accordion control's flex label, that wraps below the disclosure chevron.
        <Text span size="xs" c="dimmed">
          {summarizeLifecycleGroup(entities)}
        </Text>
      }
      steps={false}
    >
      <EntityCards threadId={threadId} entities={entities} live={live} />
    </CollapsibleRows>
  );
}

function SetupView({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:setup`}
      summary={
        <Box component="span" style={{ display: "grid", gap: 2 }}>
          <Text span size="xs" c="dimmed">
            {summarizeSetup(entities)}
          </Text>
          {setupProgress(entities).map((line, index) => (
            <Text span size="xs" lineClamp={1} key={index} style={{ overflowWrap: "anywhere" }}>
              {line}
            </Text>
          ))}
        </Box>
      }
      steps={false}
    >
      <EntityCards threadId={threadId} entities={entities} live={live} />
    </CollapsibleRows>
  );
}

export interface HistoryRowViewProps {
  threadId: string;
  row: HistoryRow;
  live: (entity: ThreadEntity) => boolean;
  inputEchoes?: ReadonlyMap<string, string>;
  onInputEchoLoaded?: (commandId: string) => void;
}

export function HistoryRowView({
  threadId,
  row,
  live,
  inputEchoes,
  onInputEchoLoaded,
}: HistoryRowViewProps): JSX.Element {
  const [first] = row.entities;
  if (row.kind === "setup") return <SetupView threadId={threadId} entities={row.entities} live={live} />;
  const lone = row.kind === "entity" || row.entities.length === 1;
  if (lone) {
    const echoCommandId = pendingSentMessage(first)
      ? first.entityId
      : first.entityKind === "confirmed_input" && "origin_command_ids" in first.state
        ? first.state.origin_command_ids.find((id) => inputEchoes?.has(id))
        : undefined;
    const fallbackText = echoCommandId === undefined ? undefined : inputEchoes?.get(echoCommandId);
    return first.entityKind === "command" && !pendingSentMessage(first) ? (
      <ProjectedControlCommand threadId={threadId} row={first} />
    ) : (
      <EntityCard
        threadId={threadId}
        entity={first}
        live={live(first)}
        fallbackText={fallbackText}
        onFallbackLoaded={
          fallbackText === undefined || echoCommandId === undefined || !onInputEchoLoaded
            ? undefined
            : () => onInputEchoLoaded(echoCommandId)
        }
      />
    );
  }
  return row.kind === "run" ? (
    <RunView threadId={threadId} entities={row.entities} contents={row.contents} live={live} />
  ) : (
    <LifecycleGroupView threadId={threadId} entities={row.entities} live={live} />
  );
}
