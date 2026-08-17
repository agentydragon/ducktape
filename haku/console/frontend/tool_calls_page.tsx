import { Button, Checkbox, Group, Loader, Text } from "@mantine/core";
import { useCallback, useRef, useState } from "react";

import { approvalDisplayFields, statusColor, terminalStatusLabel } from "./approval_state";
import { displayableError, fetchToolCalls, type ToolCallPage, type ToolCallRecord } from "./client";
import { useCoalescedRefresh } from "./coalesced_refresh";
import { PendingToolCallActions } from "./pending_tool_call_actions";
import { ToolCallCard } from "./tool_call_card";
import { useToolCallDecision } from "./tool_call_decision";
import { changedSessionId, useConsoleEvents } from "./console_events";
import { useVariant } from "./variant_control";

// One screenful and change, not the ledger's `le=500` cap. Every record carries its whole arguments
// and result payload, so a page of 25 is ~100 KB where 500 is several megabytes, and each row
// builds a syntax-highlighted code block — 500 of them block the main thread for seconds. Older
// calls arrive by following `nextCursor` on demand.
const HISTORY_PAGE_SIZE = 25;

/** The first page, refetched over what is already loaded: the fresh page, then the rows below it
 * that it did not restate. Merging rather than replacing keeps the pages an operator scrolled
 * back through while a live event refreshes the top, and refreshes in place any call that
 * finished since the last read.
 *
 * When the page and the loaded list have no row in common, more than a page has been submitted
 * since the last read, so the rows in between were never fetched. Then the page replaces the list
 * outright — splicing them together would present a silent gap as a continuous history. */
export function mergeNewestPage(page: ToolCallPage, loaded: ToolCallPage | null): ToolCallPage {
  if (!loaded || loaded.records.length === 0) return page;
  const fresh = new Set(page.records.map((record) => record.tool_call_id));
  const older = loaded.records.filter((record) => !fresh.has(record.tool_call_id));
  if (older.length === loaded.records.length) return page;
  // `loaded.nextCursor` is the position after the deepest page loaded, which the refreshed first
  // page knows nothing about.
  return { records: [...page.records, ...older], nextCursor: loaded.nextCursor };
}

/** The next page appended below what is loaded, dropping any row that arrived in both (a call
 * submitted between the two requests shifts the ledger under the cursor). */
export function appendPage(page: ToolCallPage, loaded: ToolCallPage | null): ToolCallPage {
  if (!loaded) return page;
  const seen = new Set(loaded.records.map((record) => record.tool_call_id));
  const added = page.records.filter((record) => !seen.has(record.tool_call_id));
  return { records: [...loaded.records, ...added], nextCursor: page.nextCursor };
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

// The console's own page view of the whole tool-call audit ledger — the persistent counterpart to
// the approvals drawer's ephemeral "Recent" list, with the framed haku-ui still mounted behind it.
// A pending call that streams in over the live WS signal can be decided here too, through the same
// exact-Origin-gated endpoints the approvals panel uses.
export function ToolCallsPage() {
  const [loaded, setLoaded] = useState<ToolCallPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // Auto-approved calls are Haku's routine background traffic; hiding them by default keeps the
  // ledger scannable for the calls an operator actually had to weigh in on. The server does the
  // filtering (GET /api/tool-calls?auto_approved=false), so toggling this refetches rather than
  // reslicing an already-capped page — otherwise auto-approved traffic filling a page would hide
  // older manual calls the client never even fetched.
  const [showAutoApproved, setShowAutoApproved] = useState(false);

  // What every fetch below reads, rather than the state: the live-event callback is registered
  // once (so it would close over a stale filter), and the checkbox handler needs the fetch to use
  // its new value before that state has committed.
  const filterRef = useRef(showAutoApproved);
  // Whether the next fetched page replaces what is loaded instead of merging over it. A ref, not
  // an argument, because a replacing request made while a refresh is in flight has to survive
  // into the catch-up: a filter change asked for a different slice of the ledger, and merging
  // that page over the previous filter's rows would show the two spliced together.
  const replaceRef = useRef(false);

  // At most one refetch in flight, a burst of live events collapsing into one catch-up: every
  // record carries its whole arguments and result, so overlapping fetches of the newest page cost
  // real bandwidth for answers the next one discards.
  const { refresh, busy: refreshing } = useCoalescedRefresh(async () => {
    const replaceLoaded = replaceRef.current;
    replaceRef.current = false;
    try {
      const page = await fetchToolCalls(HISTORY_PAGE_SIZE, filterRef.current);
      setLoaded((previous) => (replaceLoaded ? page : mergeNewestPage(page, previous)));
      setError(null);
    } catch (e: unknown) {
      setError(displayableError(e));
    }
  });

  const refreshReplacing = useCallback(() => {
    replaceRef.current = true;
    refresh();
  }, [refresh]);

  const loadMore = useCallback(async () => {
    const cursor = loaded?.nextCursor;
    if (cursor == null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await fetchToolCalls(HISTORY_PAGE_SIZE, filterRef.current, cursor);
      setLoaded((previous) => appendPage(page, previous));
      setError(null);
    } catch (e: unknown) {
      setError(displayableError(e));
    } finally {
      setLoadingMore(false);
    }
  }, [loaded?.nextCursor, loadingMore]);

  // Live: initial load on mount plus a refetch of the first page whenever a tool call is submitted,
  // approved, denied, or finishes anywhere — the same WS signal the approvals panel uses. Pages
  // already scrolled back through survive it (see `mergeNewestPage`). Session invalidations are
  // skipped: they say nothing about the ledger, and a streaming turn emits one every coalescing
  // window.
  useConsoleEvents((event) => {
    if (changedSessionId(event) === null) refresh();
  });

  const decisions = useToolCallDecision({ onSettled: refresh });

  const records = loaded?.records;
  const hasMore = loaded?.nextCursor != null;
  return (
    <div className="haku-page">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <Group gap="xs" wrap="nowrap" align="center">
            <Text fw={700}>Past tool calls</Text>
            {records && (
              <Text size="sm" c="dimmed">
                {records.length}
                {hasMore ? "+" : ""}
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
                filterRef.current = checked;
                // A different filter is a different ledger slice, so its pages replace rather than
                // merge with the ones already loaded.
                refreshReplacing();
              }}
            />
            <Button size="xs" variant="light" loading={refreshing} onClick={refreshReplacing}>
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
          {hasMore && (
            <Group justify="center">
              <Button size="xs" variant="light" loading={loadingMore} onClick={() => void loadMore()}>
                Load older calls
              </Button>
            </Group>
          )}
        </div>
      </div>
    </div>
  );
}
