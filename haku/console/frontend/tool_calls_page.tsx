import { Button, Checkbox, Group, Loader, Text } from "@mantine/core";
import { useCallback, useState } from "react";

import { approvalDisplayFields, statusColor, terminalStatusLabel } from "./approval_state";
import { displayableError, fetchToolCalls, type ToolCallRecord } from "./client";
import { PendingToolCallActions } from "./pending_tool_call_actions";
import { ToolCallCard } from "./tool_call_card";
import { useToolCallDecision } from "./tool_call_decision";
import { useConsoleEvents } from "./console_events";
import { useVariant } from "./variant_control";

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
      status={{
        label: deciding ? "Running" : terminalStatusLabel(record.status),
        color: deciding ? "blue" : statusColor(record.status),
      }}
      error={record.error}
      result={record.result}
      footer={pending ? <PendingToolCallActions busy={deciding} onApprove={onApprove} onDeny={onDeny} /> : null}
    />
  );
}

// The console's own page view of the whole tool-call audit ledger — a bigger,
// persistent counterpart to the approvals drawer's ephemeral "Recent" list. The shared shell
// keeps the framed haku-ui mounted behind this page.
// A pending call that streams in (via the live WS signal) can be approved/denied here too,
// through the same exact-Origin-gated endpoints the approvals panel uses, without going back to the shell.
export function ToolCallsPage() {
  const [records, setRecords] = useState<ToolCallRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Auto-approved calls are Haku's routine background traffic; hiding them by default keeps the
  // ledger scannable for the calls an operator actually had to weigh in on. The server does the
  // filtering (GET /api/tool-calls?auto_approved=false), so toggling this refetches rather than
  // reslicing an already-capped page — otherwise auto-approved traffic filling the HISTORY_LIMIT
  // window would hide older manual calls the client never even fetched.
  const [showAutoApproved, setShowAutoApproved] = useState(false);

  const load = useCallback((showAutoApprovedNow: boolean) => {
    setLoading(true);
    fetchToolCalls(HISTORY_LIMIT, showAutoApprovedNow).then(
      (calls) => {
        setRecords(calls);
        setError(null);
        setLoading(false);
      },
      (e: unknown) => {
        setError(displayableError(e));
        setLoading(false);
      }
    );
  }, []);

  // Live: initial load on mount plus a refetch whenever a tool call is submitted, approved,
  // denied, or finishes anywhere — the same WS signal the approvals panel uses.
  useConsoleEvents(() => load(showAutoApproved));

  const decisions = useToolCallDecision({ onSettled: () => load(showAutoApproved) });

  return (
    <div className="haku-page">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <Group gap="xs" wrap="nowrap" align="center">
            <Text fw={700}>Past tool calls</Text>
            {records && (
              <Text size="sm" c="dimmed">
                {records.length}
              </Text>
            )}
          </Group>
          <Group gap="sm" wrap="nowrap" align="center">
            <Checkbox
              size="xs"
              label="Show auto-approved"
              aria-label="Show auto-approved"
              checked={showAutoApproved}
              onChange={(event) => {
                const checked = event.currentTarget.checked;
                setShowAutoApproved(checked);
                load(checked);
              }}
            />
            <Button size="xs" variant="light" loading={loading} onClick={() => load(showAutoApproved)}>
              Refresh
            </Button>
          </Group>
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
                {showAutoApproved
                  ? "No tool calls recorded yet."
                  : "No matching tool calls — auto-approved calls are hidden."}
              </Text>
            </section>
          )}
          {records?.map((record) => (
            <ToolCallRow
              key={record.tool_call_id}
              record={record}
              deciding={decisions.isDeciding(record.tool_call_id)}
              onApprove={() => void decisions.approve(record)}
              onDeny={(reason) => void decisions.deny(record, reason)}
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
