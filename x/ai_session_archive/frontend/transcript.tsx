import { Badge, Box, Button, Group, Stack, Text } from "@mantine/core";
import { useId, type JSX, type MouseEvent, type ReactNode } from "react";
import { VirtualizedHistory } from "../../../util/frontend/transcript/virtualized_history";
import { DisclosureCard } from "../../../util/frontend/disclosure";
import {
  RetainedDisclosure,
  RetainedDisclosureProvider,
  useRetainedDisclosure,
} from "../../../util/frontend/transcript/retained_disclosures";
import { InspectionToggle } from "../../../util/frontend/transcript/inspection";
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
        onClick={revealRawRecord}
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
    <Box data-event-ordinal={record.ordinal}>
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
            {presentation !== "assistant" && presentation !== "activity" && (
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
            {presentation !== "metadata" && <RecordContent record={record} />}
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
