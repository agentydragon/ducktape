import { Button, Group, Loader, Text } from "@mantine/core";
import { useCallback, useState } from "react";

import { approvalDisplayFields, statusColor, terminalStatusLabel } from "./approval_state.ts";
import { approveToolCall, denyToolCall, fetchToolCalls, type ToolCallRecord } from "./client.ts";
import { ArrowLeftIcon } from "./icons.tsx";
import { PendingToolCallActions } from "./pending_tool_call_actions.tsx";
import { toastError, toastSuccess } from "./toast.ts";
import { ToolCallCard } from "./tool_call_card.tsx";
import { useToolCallEvents } from "./tool_call_events.ts";
import { useVariant } from "./variant_control.tsx";

// Matches the backend's `le=500` cap on GET /api/tool-calls (mcp_approval.py).
const HISTORY_LIMIT = 500;

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
  const [variant, setVariant] = useVariant("compact");
  const fields = approvalDisplayFields(record);
  const pending = record.status === "pending_approval";
  return (
    <ToolCallCard
      fields={fields}
      args={record.arguments}
      variant={variant}
      onVariantChange={setVariant}
      status={{ label: terminalStatusLabel(record.status), color: statusColor(record.status) }}
      error={record.error}
      result={record.result}
      footer={pending ? <PendingToolCallActions busy={deciding} onApprove={onApprove} onDeny={onDeny} /> : null}
    />
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
