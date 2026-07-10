import { Badge, Button, Group, Loader, Stack, Text } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { approvalDisplayFields, shortDate, statusColor, terminalStatusLabel } from "./approval_state.ts";
import { fetchToolCalls, type ToolCallRecord } from "./client.ts";
import { Field } from "./field.tsx";
import { ArrowLeftIcon } from "./icons.tsx";
import { ToolArgumentsField } from "./tool_arguments_field.tsx";

// Matches the backend's `le=500` cap on GET /api/tool-calls (mcp_approval.py).
const HISTORY_LIMIT = 500;

function ToolCallRow({ record }: { record: ToolCallRecord }) {
  const fields = approvalDisplayFields(record);
  return (
    <section className="haku-shell-card">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" gap="sm" wrap="nowrap">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text fw={700}>{fields.title}</Text>
            <Text size="xs" c="dimmed">
              {fields.serverId}.{fields.toolName}
            </Text>
          </Stack>
          <Badge color={statusColor(record.status)} variant="light">
            {terminalStatusLabel(record.status)}
          </Badge>
        </Group>
        {record.error && (
          <Text size="sm" c="red">
            {record.error}
          </Text>
        )}
        <dl className="haku-shell-fields">
          <div className="haku-shell-field-grid">
            <Field label="Caller">{fields.callerPrincipal ?? "—"}</Field>
            <Field label="Requested">{shortDate(fields.createdAt) ?? "—"}</Field>
          </div>
          {fields.rationale && <Field label="Rationale">{fields.rationale}</Field>}
          {fields.denialReason && <Field label="Denial reason">{fields.denialReason}</Field>}
          <ToolArgumentsField
            serverId={fields.serverId}
            toolName={fields.toolName}
            args={record.arguments}
            argumentsJson={fields.argumentsJson}
          />
          {record.result && (
            <details className="haku-shell-disclosure">
              <summary>Result</summary>
              <pre className="haku-shell-json">{JSON.stringify(record.result, null, 2)}</pre>
            </details>
          )}
          <details className="haku-shell-disclosure">
            <summary>Tool call id</summary>
            <code>{fields.toolCallId}</code>
          </details>
        </dl>
      </Stack>
    </section>
  );
}

// The console's own full-page view of the whole tool-call audit ledger — a bigger,
// persistent counterpart to the shell drawer's ephemeral "Recent" list. Its own route
// (routing.ts → "/tool-calls"), so the framed haku-ui is unmounted while it's open.
export function ToolCallsPage({ onBack }: { onBack: () => void }) {
  const [records, setRecords] = useState<ToolCallRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetchToolCalls(HISTORY_LIMIT).then(
      (calls) => {
        setRecords(calls);
        setError(null);
        setLoading(false);
      },
      (e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      }
    );
  }, []);

  useEffect(load, [load]);

  return (
    <div className="haku-history-page">
      <header className="haku-history-header">
        <div className="haku-history-bar">
          <Group gap="xs" wrap="nowrap" align="center">
            <Button size="xs" variant="subtle" color="gray" leftSection={<ArrowLeftIcon />} onClick={onBack}>
              Back
            </Button>
            <Text fw={700}>Past tool calls</Text>
            {records && (
              <Text size="sm" c="dimmed">
                {records.length}
              </Text>
            )}
          </Group>
          <Button size="xs" variant="light" loading={loading} onClick={load}>
            Refresh
          </Button>
        </div>
      </header>
      <div className="haku-history-scroll">
        <div className="haku-history-list">
          {error && (
            <Text c="red" size="sm">
              Failed to load tool calls: {error}
            </Text>
          )}
          {!records && !error && (
            <Group justify="center" p="xl">
              <Loader />
            </Group>
          )}
          {records && records.length === 0 && (
            <section className="haku-shell-card">
              <Text size="sm" c="dimmed">
                No tool calls recorded yet.
              </Text>
            </section>
          )}
          {records?.map((record) => (
            <ToolCallRow key={record.tool_call_id} record={record} />
          ))}
          {records && records.length === HISTORY_LIMIT && (
            <Text size="xs" c="dimmed" ta="center">
              Showing the {HISTORY_LIMIT} most recent tool calls.
            </Text>
          )}
        </div>
      </div>
    </div>
  );
}
