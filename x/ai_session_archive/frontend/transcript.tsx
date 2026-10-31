import { Badge, Box, Button, Group, Paper, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { VirtualizedHistory } from "../../../util/frontend/transcript/virtualized_history";
import { DisclosureBody, DisclosureCard } from "../../../util/frontend/disclosure";
import {
  RetainedDisclosure,
  RetainedDisclosureProvider,
  useRetainedDisclosure,
} from "../../../util/frontend/transcript/retained_disclosures";
import { UserMessage } from "../../../util/frontend/transcript/message";
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
          key: String(row.records.at(-1)!.ordinal),
          revision: `${row.kind}:${row.records.length}:${row.records[0].ordinal}`,
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
  const id = `group:${row.records.at(-1)!.ordinal}`;
  const [open] = useRetainedDisclosure(id);
  if (row.kind === "record") return <EventRow record={row.records[0]} />;
  return (
    <DisclosureCard open={open}>
      <RetainedDisclosure
        id={id}
        summary={`${row.kind === "activity" ? "Tools and reasoning" : "Session details"} · ${row.records.length} ${row.records.length === 1 ? "record" : "records"}`}
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
