import { Badge, Box, Button, Group, Stack, Text } from "@mantine/core";
import { useId, type JSX, type MouseEvent, type ReactNode } from "react";
import { VirtualizedHistory } from "../../../util/frontend/transcript/virtualized_history";
import { DisclosureBody, DisclosureCard } from "../../../util/frontend/disclosure";
import {
  RetainedDisclosure,
  RetainedDisclosureProvider,
  useRetainedDisclosure,
} from "../../../util/frontend/transcript/retained_disclosures";
import { InspectionToggle } from "../../../util/frontend/transcript/inspection";
import { UserMessage } from "../../../util/frontend/transcript/message";
import { StepLine } from "../../../util/frontend/transcript/step_line";
import { TextBlock } from "../../../util/frontend/transcript/text_block";
import { Markdown } from "../../../util/frontend/markdown";
import { recordLabel, RecordContent, rawRecordText } from "./event-content";
import {
  recordPresentation,
  summarizeActivity,
  transcriptRows,
  type ActivityStep,
  type TranscriptRow,
} from "./transcript-rows";
import type { EventRecord } from "./schema";

function formatDateTime(value: string | null): string {
  if (value === null) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ArchiveTranscript({
  scopeId,
  records,
  earlier,
  tail,
}: {
  scopeId: string;
  records: readonly EventRecord[];
  earlier: { available: boolean; loading: boolean; load: () => void };
  tail: ReactNode;
}): JSX.Element {
  return (
    <RetainedDisclosureProvider key={scopeId}>
      {earlier.available && (
        <Button variant="subtle" size="compact-sm" loading={earlier.loading} onClick={earlier.load}>
          Load earlier records
        </Button>
      )}
      <VirtualizedHistory
        scopeId={`archive:${scopeId}`}
        earlier={earlier}
        label="Conversation records"
        showJumpToLatest={true}
        rows={transcriptRows(records).map((row) => ({
          key: row.key,
          revision: `${row.kind}:${row.records.length}:${row.records[0].ordinal}:${row.kind === "activity" ? row.steps.map((step) => step.key).join(",") : ""}`,
          value: row,
          anchorKeys: [
            ...row.records.map((record) => `record:${record.ordinal}`),
            ...(row.kind === "activity"
              ? row.steps.flatMap((step) => [
                  `step:${step.key}`,
                  `reasoning:${step.key}`,
                  `step:${step.key}:input`,
                  `step:${step.key}:output`,
                ])
              : []),
          ],
          presentation: { kind: row.kind, entityCount: row.records.length },
        }))}
        renderRow={(row) => <TranscriptRowView row={row} />}
        tail={tail}
        onClick={revealRawRecord}
      />
    </RetainedDisclosureProvider>
  );
}

function TranscriptRowView({ row }: { row: TranscriptRow }): JSX.Element {
  const id = `group:${row.key}`;
  const [open] = useRetainedDisclosure(id);
  if (row.kind === "record") return <EventRow record={row.records[0]} />;
  if (row.kind === "activity" && row.steps.length === 1)
    return <ActivityStepView steps={row.steps} sourceRecords={row.records} />;
  return (
    <DisclosureCard open={open}>
      <RetainedDisclosure
        id={id}
        summary={
          row.kind === "activity" ? (
            <>
              <Text span size="sm" c="dimmed">
                {summarizeActivity(row.steps)}
              </Text>
              {row.steps.some((step) => step.kind === "tool" && step.failed) && (
                <Text span size="xs" c="red">
                  {" "}
                  · Failed
                </Text>
              )}
            </>
          ) : (
            <Text span size="xs" c="dimmed">
              Session details
            </Text>
          )
        }
      >
        <Stack gap={0} className="transcript-steps">
          {row.kind === "activity" ? (
            <ActivitySteps steps={row.steps} />
          ) : (
            row.records.map((record) => <EventRow key={record.ordinal} record={record} />)
          )}
          {row.kind === "activity" && (
            <SessionDetails records={row.records.filter((record) => recordPresentation(record) === "metadata")} />
          )}
        </Stack>
      </RetainedDisclosure>
    </DisclosureCard>
  );
}

function SessionDetails({ records }: { records: EventRecord[] }): JSX.Element | null {
  if (records.length === 0) return null;
  return (
    <RetainedDisclosure
      id={`details:${records.at(-1)!.ordinal}`}
      summary={
        <Text span size="xs" c="dimmed">
          Session details
        </Text>
      }
    >
      {records.map((record) => (
        <EventRow key={record.ordinal} record={record} />
      ))}
    </RetainedDisclosure>
  );
}

function ActivitySteps({ steps }: { steps: ActivityStep[] }): JSX.Element {
  const contents: ActivityStep[][] = [];
  for (const step of steps) {
    const last = contents.at(-1);
    if (step.kind === "reasoning" && last?.[0].kind === "reasoning") last.push(step);
    else contents.push([step]);
  }
  return (
    <>
      {contents.map((group) => (
        <ActivityStepView key={group.at(-1)!.key} steps={group} />
      ))}
    </>
  );
}

function ActivityStepView({
  steps,
  sourceRecords,
}: {
  steps: ActivityStep[];
  sourceRecords?: EventRecord[];
}): JSX.Element {
  const step = steps[0];
  const records =
    sourceRecords ??
    [...new Map(steps.flatMap((step) => step.records).map((record) => [record.ordinal, record])).values()].sort(
      (left, right) => left.ordinal - right.ordinal
    );
  const id = `step:${steps.at(-1)!.key}`;
  const [open, setOpen] = useRetainedDisclosure(id);
  const [raw, setRaw] = useRetainedDisclosure(`${id}:raw`);
  const inputExpansion = useRetainedDisclosure(`${id}:input`);
  const outputExpansion = useRetainedDisclosure(`${id}:output`);
  const panelId = useId();
  const reasoning = steps.flatMap((item) => (item.kind === "reasoning" ? [item.text] : [])).join("\n\n");
  const title =
    step.kind === "tool"
      ? step.missingCall
        ? "Tool result"
        : step.name
      : step.kind === "reasoning"
        ? "Reasoning"
        : step.kind === "subagent"
          ? `Agent message · ${step.author}`
          : step.kind === "attachment"
            ? step.title
            : recordLabel(records[0]);
  const preview =
    step.kind === "tool"
      ? (step.input ?? step.output ?? "No recorded input or output")
      : step.kind === "reasoning"
        ? reasoning
        : step.kind === "subagent"
          ? step.text
          : step.kind === "attachment"
            ? step.preview
            : "";
  return (
    <Box
      className="transcript-inspection-owner"
      data-activity-step={step.kind}
      data-open={open}
      data-history-anchor-key={id}
    >
      {records.map((record) => (
        <span key={record.ordinal} data-event-ordinal={record.ordinal} />
      ))}
      <DisclosureCard open={open}>
        <StepLine
          title={title}
          preview={
            step.kind === "reasoning" ? (
              <Markdown source={reasoning} singleLine size="sm" />
            ) : (
              <Text span size="sm">
                {preview.replace(/\s+/g, " ").slice(0, 240)}
              </Text>
            )
          }
          mark={step.kind === "tool" && step.failed ? "failed" : undefined}
          expandable
          open={open}
          onOpenChange={setOpen}
          summaryAside={
            <InspectionToggle
              open={raw}
              onChange={setRaw}
              label="Raw JSONL record"
              controls={panelId}
              placement="inline"
            />
          }
        >
          {step.kind === "tool" ? (
            <>
              {step.missingCall && (
                <DisclosureBody>
                  <Text size="xs" c="dimmed">
                    The call is outside the loaded records or was not captured.
                  </Text>
                </DisclosureBody>
              )}
              {step.input !== null && (
                <Box data-history-anchor-key={`${id}:input`}>
                  <TextBlock label="Arguments" text={step.input} expansion={inputExpansion} />
                </Box>
              )}
              {step.output !== null ? (
                <Box data-history-anchor-key={`${id}:output`}>
                  <TextBlock label="Output" text={step.output} expansion={outputExpansion} />
                </Box>
              ) : (
                <DisclosureBody>
                  <Text size="xs" c="dimmed">
                    No result in the loaded records.
                  </Text>
                </DisclosureBody>
              )}
            </>
          ) : step.kind === "reasoning" ? (
            <DisclosureBody>
              {steps.map(
                (item) =>
                  item.kind === "reasoning" && (
                    <Box key={item.key} data-history-anchor-key={`reasoning:${item.key}`}>
                      <Markdown source={item.text} />
                    </Box>
                  )
              )}
            </DisclosureBody>
          ) : step.kind === "subagent" ? (
            <DisclosureBody>
              {step.recipient !== null && (
                <Text size="xs" c="dimmed">
                  To {step.recipient}
                </Text>
              )}
              <Markdown source={step.text} />
            </DisclosureBody>
          ) : (
            <RecordContent record={records[0]} />
          )}
        </StepLine>
      </DisclosureCard>
      {raw && (
        <Box id={panelId}>
          {records.map((record) => (
            <Box key={record.ordinal}>
              <Text size="xs" c="dimmed">
                Line {record.ordinal} · {recordLabel(record)}
              </Text>
              <RawRecordPanel record={record} panelId={`${panelId}:${record.ordinal}`} />
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
}

/** Touch users reveal the same inspection action that hover or keyboard focus reveals. */
function revealRawRecord(event: MouseEvent<HTMLDivElement>): void {
  if (!(event.target instanceof Element) || event.target.closest("a, button, input, textarea, select")) return;
  if (window.getSelection()?.isCollapsed === false) return;
  event.currentTarget.querySelector("[data-inspection-revealed]")?.removeAttribute("data-inspection-revealed");
  event.target.closest(".transcript-inspection-owner")?.setAttribute("data-inspection-revealed", "");
}

function EventRow({ record }: { record: EventRecord }): JSX.Element {
  const [raw, setRaw] = useRetainedDisclosure(`raw:${record.ordinal}`);
  const panelId = useId();
  const presentation = recordPresentation(record);
  const metadata = `Line ${record.ordinal} · ${formatDateTime(record.timestamp)}`;
  const inspection = (
    <InspectionToggle
      open={raw}
      onChange={setRaw}
      label="Raw JSONL record"
      controls={panelId}
      placement={presentation === "user" ? "bubble" : "overlay"}
    />
  );
  return (
    <Box data-event-ordinal={record.ordinal} data-history-anchor-key={`record:${record.ordinal}`}>
      {presentation === "user" ? (
        <UserMessage
          bubbleAttributes={{ className: "transcript-inspection-owner", title: metadata }}
          overlay={inspection}
        >
          <RecordContent record={record} />
        </UserMessage>
      ) : (
        <Box className="archive-transcript-record transcript-inspection-owner" title={metadata}>
          {inspection}
          <Stack gap={4} className="archive-transcript-record-body">
            {presentation !== "assistant" && presentation !== "activity" && presentation !== "compaction" && (
              <Group gap="xs">
                <Text size="xs" c="dimmed">
                  {recordLabel(record)} · {record.ordinal}
                </Text>
                {record.parse_error != null && (
                  <Badge color="orange" variant="light" size="xs">
                    Malformed JSONL
                  </Badge>
                )}
              </Group>
            )}
            {presentation === "compaction" ? (
              <Text size="xs" c="dimmed">
                Context compacted
              </Text>
            ) : (
              presentation !== "metadata" && <RecordContent record={record} />
            )}
          </Stack>
        </Box>
      )}
      {raw && <RawRecordPanel record={record} panelId={panelId} />}
    </Box>
  );
}

function RawRecordPanel({ record, panelId }: { record: EventRecord; panelId: string }): JSX.Element {
  const [bytes, setBytes] = useRetainedDisclosure(`bytes:${record.ordinal}`);
  const bytesId = useId();
  return (
    <Box id={panelId} role="region" aria-label="Raw JSONL record" className="archive-raw-record">
      <Box component="pre">{rawRecordText(record.raw_base64)}</Box>
      <Button
        variant="subtle"
        color="gray"
        size="compact-xs"
        aria-expanded={bytes}
        aria-controls={bytesId}
        onClick={() => setBytes(!bytes)}
      >
        Exact line bytes (base64)
      </Button>
      {bytes && (
        <Box id={bytesId} component="pre">
          {record.raw_base64}
        </Box>
      )}
    </Box>
  );
}
