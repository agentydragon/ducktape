import { Badge, Button, Group, Loader, Stack, Text, Textarea } from "@mantine/core";
import { useCallback, useState } from "react";

import { approvalDisplayFields, shortDate, statusColor, terminalStatusLabel } from "./approval_state.ts";
import { approveToolCall, denyToolCall, fetchToolCalls, type ToolCallRecord } from "./client.ts";
import { Field } from "./field.tsx";
import { ArrowLeftIcon } from "./icons.tsx";
import { ACTION_COLOR } from "./theme.ts";
import { toastError, toastSuccess } from "./toast.ts";
import { ToolArgumentsField } from "./tool_arguments_field.tsx";
import { useToolCallEvents } from "./tool_call_events.ts";
import { useVariant, VariantToggle } from "./variant_toggle.tsx";

// Matches the backend's `le=500` cap on GET /api/tool-calls (mcp_approval.py).
const HISTORY_LIMIT = 500;

function PendingActions({
  deciding,
  onApprove,
  onDeny,
}: {
  deciding: boolean;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
}) {
  const [denyReason, setDenyReason] = useState("");
  return (
    <div>
      <Textarea
        size="xs"
        label="Denial reason (optional)"
        placeholder="Why are you denying this?"
        autosize
        minRows={1}
        maxRows={4}
        disabled={deciding}
        value={denyReason}
        onChange={(e) => setDenyReason(e.currentTarget.value)}
      />
      <Group justify="flex-end" gap="xs" mt="xs">
        <Button
          size="compact-sm"
          variant="light"
          color="red"
          loading={deciding}
          onClick={() => onDeny(denyReason.trim() || undefined)}
        >
          Deny
        </Button>
        <Button size="compact-sm" color={ACTION_COLOR} loading={deciding} onClick={onApprove}>
          Approve
        </Button>
      </Group>
    </div>
  );
}

function ToolCallRow({
  record,
  deciding,
  onApprove,
  onDeny,
}: {
  record: ToolCallRecord;
  deciding: boolean;
  onApprove: () => void;
  onDeny: (reason?: string) => void;
}) {
  // Per-row verbosity: the ledger starts compact (scannable) and expands to the full record
  // on demand. The variant propagates to both the arguments field and the detail-only fields.
  const [variant, toggleVariant] = useVariant("compact");
  const fields = approvalDisplayFields(record);
  const pending = record.status === "pending_approval";
  const detailed = variant === "detailed";
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
          <Group gap="xs" wrap="nowrap" style={{ flexShrink: 0 }}>
            <Badge color={statusColor(record.status)} variant="light">
              {terminalStatusLabel(record.status)}
            </Badge>
            <VariantToggle variant={variant} onToggle={toggleVariant} />
          </Group>
        </Group>
        {record.error && (
          <Text size="sm" c="red">
            {record.error}
          </Text>
        )}
        <dl className="haku-shell-fields">
          {detailed && (
            <>
              <div className="haku-shell-field-grid">
                <Field label="Caller">{fields.callerPrincipal ?? "—"}</Field>
                <Field label="Requested">{shortDate(fields.createdAt) ?? "—"}</Field>
              </div>
              {fields.rationale && <Field label="Rationale">{fields.rationale}</Field>}
            </>
          )}
          {fields.denialReason && <Field label="Denial reason">{fields.denialReason}</Field>}
          <ToolArgumentsField
            serverId={fields.serverId}
            toolName={fields.toolName}
            args={record.arguments}
            argumentsJson={fields.argumentsJson}
            variant={variant}
          />
          {detailed && record.result && (
            <details className="haku-shell-disclosure">
              <summary>Result</summary>
              <pre className="haku-shell-json">{JSON.stringify(record.result, null, 2)}</pre>
            </details>
          )}
          {detailed && (
            <details className="haku-shell-disclosure">
              <summary>Tool call id</summary>
              <code>{fields.toolCallId}</code>
            </details>
          )}
        </dl>
        {pending && <PendingActions deciding={deciding} onApprove={onApprove} onDeny={onDeny} />}
      </Stack>
    </section>
  );
}

// The console's own full-page view of the whole tool-call audit ledger — a bigger,
// persistent counterpart to the shell drawer's ephemeral "Recent" list. Its own route
// (routing.ts → "/tool-calls"), so the framed haku-ui is unmounted while it's open.
// A pending call that streams in (via the live WS signal) can be approved/denied here too,
// through the same CSRF-gated endpoints the drawer uses, without going back to the shell.
export function ToolCallsPage({ onBack }: { onBack: () => void }) {
  const [records, setRecords] = useState<ToolCallRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [decidingId, setDecidingId] = useState<string | null>(null);

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

  // Live: initial load on mount plus a refetch whenever a tool call is submitted, approved,
  // denied, or finishes anywhere — the same WS signal the approval drawer uses.
  useToolCallEvents(load);

  const decide = useCallback(
    (id: string, run: () => Promise<ToolCallRecord>, okTitle: string) => {
      setDecidingId(id);
      run().then(
        (record) => {
          toastSuccess(okTitle, `${record.server_id}.${record.tool_name}: ${record.status}`);
          setDecidingId(null);
          load();
        },
        (e: unknown) => {
          toastError("Tool call decision failed", e);
          setDecidingId(null);
          load();
        }
      );
    },
    [load]
  );

  return (
    <div className="haku-page">
      <header className="haku-page-header">
        <div className="haku-page-bar">
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
      <div className="haku-page-scroll">
        <div className="haku-page-list">
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
            <ToolCallRow
              key={record.tool_call_id}
              record={record}
              deciding={decidingId === record.tool_call_id}
              onApprove={() =>
                decide(record.tool_call_id, () => approveToolCall(record.tool_call_id), "Tool call approved")
              }
              onDeny={(reason) =>
                decide(record.tool_call_id, () => denyToolCall(record.tool_call_id, reason), "Tool call denied")
              }
            />
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
