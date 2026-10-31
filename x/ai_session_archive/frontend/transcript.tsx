import { Badge, Box, Group, Paper, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { VirtualizedHistory } from "../../../util/frontend/virtualized_history";
import { DisclosureBody, DisclosureCard } from "../../../util/frontend/disclosure";
import {
  RetainedDisclosure,
  RetainedDisclosureProvider,
  useRetainedDisclosure,
} from "../../../util/frontend/retained_disclosures";
import { UserMessage } from "../../../util/frontend/message";
import { recordLabel, RecordContent, rawRecordText } from "./event-content";
import { recordPresentation, transcriptRows, type TranscriptRow } from "./transcript-rows";
import type { EventRecord } from "./schema";

function formatDateTime(value: string | null): string {
  if (value === null) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ArchiveTranscript({
  scopeId,
  records,
  tail,
}: {
  scopeId: string;
  records: readonly EventRecord[];
  tail: ReactNode;
}): JSX.Element {
  return (
    <RetainedDisclosureProvider key={scopeId}>
      <VirtualizedHistory
        scopeId={`archive:${scopeId}`}
        initialPosition="start"
        followTail={false}
        label="Conversation records"
        showJumpToLatest={false}
        rows={transcriptRows(records).map((row) => ({
          key: String(row.records[0].ordinal),
          revision: `${row.kind}:${row.records.length}:${row.records.at(-1)!.ordinal}`,
          value: row,
          anchorKeys: row.records.map((record) => String(record.ordinal)),
          presentation: { kind: row.kind, entityCount: row.records.length },
        }))}
        renderRow={(row) => <TranscriptRowView row={row} />}
        tail={tail}
      />
    </RetainedDisclosureProvider>
  );
}

function TranscriptRowView({ row }: { row: TranscriptRow }): JSX.Element {
  const id = `group:${row.records[0].ordinal}`;
  const [open] = useRetainedDisclosure(id);
  if (row.kind === "record") return <EventRow record={row.records[0]} />;
  return (
    <DisclosureCard open={open}>
      <RetainedDisclosure
        id={id}
        summary={`${row.kind === "activity" ? "Tools and reasoning" : "Session details"} · ${row.records.length} records`}
      >
        <Stack gap="xs">
          {row.records.map((record) => (
            <EventRow key={record.ordinal} record={record} />
          ))}
        </Stack>
      </RetainedDisclosure>
    </DisclosureCard>
  );
}

function EventRow({ record }: { record: EventRecord }): JSX.Element {
  return (
    <Box data-event-ordinal={record.ordinal}>
      {recordPresentation(record) === "user" ? (
        <UserMessage>
          <Text size="xs" c="dimmed">
            Line {record.ordinal} · {formatDateTime(record.timestamp)}
          </Text>
          <RecordContent record={record} />
          <RawRecord record={record} />
        </UserMessage>
      ) : (
        <Paper withBorder radius="sm" p="sm">
          <Stack gap="xs">
            <Group justify="space-between" align="start" gap="xs">
              <Group gap="xs">
                <Badge variant="light">{recordLabel(record)}</Badge>
                <Text size="xs" c="dimmed">
                  Line {record.ordinal}
                </Text>
                {record.parse_error != null && (
                  <Badge color="orange" variant="light">
                    Malformed JSONL
                  </Badge>
                )}
              </Group>
              <Text size="xs" c="dimmed">
                {formatDateTime(record.timestamp)}
              </Text>
            </Group>
            <RecordContent record={record} />
            <RawRecord record={record} />
          </Stack>
        </Paper>
      )}
    </Box>
  );
}

function RawRecord({ record }: { record: EventRecord }): JSX.Element {
  return (
    <RetainedDisclosure id={`raw:${record.ordinal}`} summary="Raw JSONL record">
      <DisclosureBody>
        <Box
          component="pre"
          style={{
            maxHeight: 360,
            overflow: "auto",
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            fontSize: "var(--mantine-font-size-xs)",
          }}
        >
          {rawRecordText(record.raw_base64)}
        </Box>
        <RetainedDisclosure id={`bytes:${record.ordinal}`} summary="Exact line bytes (base64)">
          <DisclosureBody>
            <Box
              component="pre"
              style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "var(--mantine-font-size-xs)" }}
            >
              {record.raw_base64}
            </Box>
          </DisclosureBody>
        </RetainedDisclosure>
      </DisclosureBody>
    </RetainedDisclosure>
  );
}
