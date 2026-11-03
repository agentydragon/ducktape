/** Read-only, Session-addressed inbox diagnostics. Payloads and acknowledgement remain private. */
import { Accordion, Divider, Drawer, Group, Paper, ScrollArea, Select, Stack, Text } from "@mantine/core";
import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";
import { useEffect, useState, type JSX } from "react";
import { ActionState } from "../../action_service/action_service_pb";
import {
  GitHubEventName,
  NoticeWaitReason,
  SourceFailureKind,
  WatchInboxResponseSchema,
  WatchInboxEntriesResponseSchema,
  WatchSubscriptionsResponseSchema,
  type GitHubAccessStatus,
  type GitHubRefreshStatus,
  type GitHubSubject,
  type GitHubSubjectStatus,
  type Inbox,
  type InboxEntrySummary,
  type Source,
  type Subscription,
} from "../../notification_service/notifications_pb";
import { clockTime, isoTime, timeAgo } from "./format_time";
import { followWatch } from "./watch_stream";
import { inboxWatchUrl } from "./threads/thread_notices";

type SubscriptionFilter = "not-cancelled" | "active" | "expired" | "cancelled" | "all";

function iso(value: Timestamp | undefined): string | undefined {
  return value ? (toJson(TimestampSchema, value) as string) : undefined;
}
function time(value: Timestamp | undefined): string {
  const text = iso(value);
  return text ? `${timeAgo(text)} (${isoTime(text)})` : "unknown";
}
function words(value: string): string {
  return value
    .replace(/^(?:SOURCE_FAILURE_KIND|NOTICE_WAIT_REASON)_/, "")
    .toLowerCase()
    .replaceAll("_", " ");
}
function githubEvent(value: GitHubEventName): string {
  return (GitHubEventName[value] ?? "unknown").replace(/^GITHUB_EVENT_NAME_/, "").toLowerCase();
}
function SharedRefresh({
  label,
  state,
}: {
  label: string;
  state: GitHubAccessStatus | GitHubSubjectStatus | GitHubRefreshStatus;
}): JSX.Element {
  return (
    <Text size="xs" c={state.error ? "red" : "dimmed"}>
      {label}: {state.error ?? (state.lastSuccessAt ? `last success ${time(state.lastSuccessAt)}` : "not yet observed")}
      {state.errorSince ? ` · since ${time(state.errorSince)}` : ""}
      {state.errorObservedAt ? ` · last observed ${time(state.errorObservedAt)}` : ""}
      {state.retryAt ? ` · retry ${time(state.retryAt)}` : ""}
      {state.refreshingUntil ? ` · refresh lease until ${time(state.refreshingUntil)}` : ""}
    </Text>
  );
}
function subjectLabel(subject: GitHubSubject | undefined): string {
  switch (subject?.subject.case) {
    case "pullRequest":
      return `pull request ${subject.subject.value.number}`;
    case "issue":
      return `issue ${subject.subject.value.number}`;
    case "branch":
      return `branch ${subject.subject.value.name}`;
    case "commit":
      return `commit ${subject.subject.value.sha}`;
    default:
      return "event";
  }
}
function sourceLabel(source: Source | undefined): string {
  switch (source?.provider.case) {
    case "actions":
      return `Action ${source.provider.value.requestId}`;
    case "github":
      return `${source.provider.value.repository} · ${subjectLabel(source.provider.value.subject)}`;
    default:
      return "Unknown source";
  }
}
function subscriptionLabel(sub: Subscription): string {
  if (sub.cancelled) return "cancelled";
  if (sub.expired) return "expired";
  const failure =
    sub.errorKind ??
    sub.github?.access.find((access) => access.errorKind !== undefined)?.errorKind ??
    sub.github?.subject?.errorKind;
  if (failure !== undefined) return words(SourceFailureKind[failure] ?? "unknown");
  if (sub.github?.access.some((access) => !access.currentlyValid)) return "GitHub access not validated";
  return "no current source error";
}
function entryLabel(entry: InboxEntrySummary): string {
  switch (entry.event?.provider.case) {
    case "actions": {
      const event = entry.event.provider.value;
      const state = entry.actionState;
      return `Action ${event?.requestId ?? ""} · event ${event?.sequence ?? 0}${state === undefined ? "" : ` · ${(ActionState[state] ?? "unknown").toLowerCase()}`}`;
    }
    case "github": {
      const event = entry.event.provider.value;
      const conclusion = entry.githubConclusion;
      return `GitHub ${githubEvent(event?.event ?? 0)}${event?.action ? ` · ${event.action}` : ""}${conclusion ? ` · ${conclusion}` : ""}`;
    }
    case "githubObservation":
      return `GitHub ${githubEvent(entry.event.provider.value.event)} observation · sequence ${entry.event.provider.value.sequence}`;
    case "githubMergeability":
      return `GitHub PR #${entry.event.provider.value.pullRequestNumber} mergeability`;
    case "message":
      return `Message from ${entry.event.provider.value.sender?.namespace ?? ""}/${entry.event.provider.value.sender?.name ?? ""}`;
    default:
      return "Unknown inbox entry";
  }
}

function InboxDetails({ sessionId, filter }: { sessionId: string; filter: SubscriptionFilter }): JSX.Element {
  const [inbox, setInbox] = useState<Inbox | null>(null);
  const [entries, setEntries] = useState<Map<bigint, InboxEntrySummary>>(new Map());
  const [subscriptions, setSubscriptions] = useState<Map<string, Subscription>>(new Map());
  const [more, setMore] = useState(false);
  const [live, setLive] = useState(new Set<string>());
  const [reconnecting, setReconnecting] = useState(false);
  useEffect(() => {
    setInbox(null);
    setEntries(new Map());
    setSubscriptions(new Map());
    setMore(false);
    setLive(new Set());
    setReconnecting(false);
    const base = `/upstream/notifications/v1/sessions/${encodeURIComponent(sessionId)}`;
    function connection(which: string) {
      return (status: { phase: string }): void => {
        if (status.phase === "reconnecting") setReconnecting(true);
        setLive((old) => {
          const next = new Set(old);
          if (status.phase === "live") next.add(which);
          else next.delete(which);
          return next;
        });
      };
    }
    let inboxReady = false,
      entriesReady = false,
      subscriptionsReady = false;
    const inboxStop = followWatch(inboxWatchUrl(sessionId), {
      initialFrameTimeoutMs: 5000,
      idleTimeoutMs: 45000,
      onOpen: () => {
        inboxReady = false;
      },
      onConnection: connection("inbox"),
      onFrame: (json) => {
        const response = fromJson(WatchInboxResponseSchema, json as JsonValue);
        if (response.state.case === "heartbeat") {
          if (!inboxReady) throw new Error("Heartbeat before inbox state");
          return;
        }
        if (response.state.case === "inbox") setInbox(response.state.value);
        else if (response.state.case === "noInbox") setInbox(null);
        else throw new Error("Missing inbox state");
        inboxReady = true;
      },
    });
    const entriesStop = followWatch(`${base}/inbox/entries:watch`, {
      initialFrameTimeoutMs: 5000,
      idleTimeoutMs: 45000,
      onOpen: () => {
        entriesReady = false;
      },
      onConnection: connection("entries"),
      onFrame: (json) => {
        const response = fromJson(WatchInboxEntriesResponseSchema, json as JsonValue);
        if (response.frame.case === "heartbeat") {
          if (!entriesReady) throw new Error("Heartbeat before entries snapshot");
          return;
        }
        if (response.frame.case === "snapshot") {
          setEntries(new Map(response.frame.value.entries.map((entry) => [entry.cursor, entry])));
          setMore(response.frame.value.more);
          entriesReady = true;
        } else if (response.frame.case === "delta" && entriesReady) {
          const delta = response.frame.value;
          setEntries((old) => {
            const next = new Map(old);
            for (const id of delta.removedCursors) next.delete(id);
            for (const entry of delta.upserted) next.set(entry.cursor, entry);
            return next;
          });
          setMore(delta.more);
        } else throw new Error("Invalid inbox entries frame");
      },
    });
    const subscriptionsStop = followWatch(`${base}/subscriptions:watch`, {
      initialFrameTimeoutMs: 5000,
      idleTimeoutMs: 45000,
      onOpen: () => {
        subscriptionsReady = false;
      },
      onConnection: connection("subscriptions"),
      onFrame: (json) => {
        const response = fromJson(WatchSubscriptionsResponseSchema, json as JsonValue);
        if (response.frame.case === "heartbeat") {
          if (!subscriptionsReady) throw new Error("Heartbeat before subscription snapshot");
          return;
        }
        if (response.frame.case === "snapshot") {
          setSubscriptions(new Map(response.frame.value.subscriptions.map((sub) => [sub.id, sub])));
          subscriptionsReady = true;
        } else if (response.frame.case === "delta" && subscriptionsReady) {
          const delta = response.frame.value;
          setSubscriptions((old) => {
            const next = new Map(old);
            for (const id of delta.removedIds) next.delete(id);
            for (const sub of delta.upserted) next.set(sub.id, sub);
            return next;
          });
        } else throw new Error("Invalid subscriptions frame");
      },
    });
    return () => {
      inboxStop();
      entriesStop();
      subscriptionsStop();
    };
  }, [sessionId]);
  if (!inbox)
    return (
      <Text size="sm">
        {live.size === 3
          ? `No inbox for Session ${sessionId}.`
          : reconnecting
            ? "Reconnecting to notification status…"
            : "Loading notification status…"}
      </Text>
    );
  const ordered = [...entries.values()].sort((a, b) => (a.cursor < b.cursor ? -1 : a.cursor > b.cursor ? 1 : 0));
  const shown = [...subscriptions.values()].filter((sub) => {
    const state = sub.cancelled ? "cancelled" : sub.expired ? "expired" : "active";
    return filter === "all" || (filter === "not-cancelled" ? state !== "cancelled" : filter === state);
  });
  const reason = inbox.delivery?.waitReason;
  const due = inbox.delivery?.noticeDueAt;
  return (
    <Paper withBorder p="md">
      <Stack gap="xs">
        <Group justify="space-between">
          <Text fw={600}>Session {inbox.sessionId}</Text>
          {inbox.retired && (
            <Text size="xs" c="dimmed">
              Retired
            </Text>
          )}
        </Group>
        {live.size !== 3 && (
          <Text size="xs" c="dimmed">
            Disconnected; showing last snapshot
          </Text>
        )}
        {inbox.latestNotice && (
          <Text size="sm">
            Latest notice through {inbox.latestNotice.throughCursor.toString()}:{" "}
            {inbox.latestNotice.error
              ? `error: ${inbox.latestNotice.error}`
              : inbox.latestNotice.confirmed
                ? "harness confirmed"
                : inbox.latestNotice.admitted
                  ? "runner admitted; awaiting confirmation"
                  : inbox.latestNotice.attempted
                    ? "delivery attempted"
                    : "prepared"}
          </Text>
        )}
        {inbox.deliveryError && (
          <Text c="red" size="sm">
            Delivery: {inbox.deliveryError}
          </Text>
        )}
        {ordered.length === 0 && (inbox.delivery?.unannouncedCount ?? 0) > 0 && (
          <Text size="sm">{inbox.delivery?.unannouncedCount} awaiting notice</Text>
        )}
        {reason !== undefined && (
          <Text size="sm">
            Notice: {words(NoticeWaitReason[reason] ?? "unknown")}
            {due ? ` · eligible ${time(due)}` : ""}
          </Text>
        )}
        <Divider
          label={`Entries after acknowledgement · ${inbox.delivery?.pendingAcknowledgementCount ?? 0} not acknowledged`}
        />
        {ordered.length === 0 && (
          <Text size="sm" c="dimmed">
            No retained unacknowledged entries
          </Text>
        )}
        {ordered.map((entry, index) => (
          <div key={entry.cursor.toString()}>
            {entry.cursor > inbox.covered && (index === 0 || ordered[index - 1].cursor <= inbox.covered) && (
              <Divider
                label={`Notice covered through #${inbox.covered.toString()} · ${inbox.delivery?.unannouncedCount ?? 0} awaiting notice below`}
              />
            )}
            <Group gap="xs" wrap="nowrap" align="flex-start" py={4}>
              <Text size="xs" c="dimmed">
                #{entry.cursor.toString()}
              </Text>
              <Text size="sm" style={{ overflowWrap: "anywhere", flex: 1 }}>
                {entryLabel(entry)}
              </Text>
              <Text size="xs" c="dimmed">
                {iso(entry.createdAt) ? clockTime(iso(entry.createdAt)!) : ""}
              </Text>
            </Group>
          </div>
        ))}
        {more && (
          <Text size="xs" c="dimmed">
            Showing the first 100 retained entries after acknowledgement; more entries remain.
          </Text>
        )}
        <Divider label="Subscriptions" />
        {shown.length === 0 && <Text size="sm">No subscriptions to show</Text>}
        {shown.map((sub) => (
          <Stack gap={2} key={sub.id}>
            <Text size="sm">
              {sourceLabel(sub.source)} · {subscriptionLabel(sub)}
            </Text>
            <Text size="xs" c="dimmed">
              Expires {time(sub.expiresAt)}
              {sub.nextSourceCheckAt
                ? ` · ${sub.error ? "retry" : "next source check"} ${time(sub.nextSourceCheckAt)}`
                : ""}
            </Text>
            <Text size="xs" c="dimmed">
              {sub.lastSuccessAt
                ? `Last successful processing: ${time(sub.lastSuccessAt)}`
                : "No successful processing recorded"}
            </Text>
            {sub.github?.access.map((access) => (
              <Stack gap={0} key={`${access.appId}/${access.installationId}/${access.repositoryId}`}>
                <SharedRefresh
                  label={`Shared GitHub access · repository ${access.repositoryId} · installation ${access.installationId}`}
                  state={access}
                />
                <Text size="xs" c="dimmed">
                  {access.currentlyValid
                    ? `Validated until ${time(access.validUntil)}`
                    : "Access not currently validated"}
                </Text>
              </Stack>
            ))}
            {sub.github?.subject && (
              <SharedRefresh
                label={`Shared GitHub subject · ${subjectLabel(sub.github.subject.subject)}`}
                state={sub.github.subject}
              />
            )}
            {sub.github?.checkRuns && <SharedRefresh label="Shared check runs" state={sub.github.checkRuns} />}
            {sub.github?.commitStatuses && (
              <SharedRefresh label="Shared commit statuses" state={sub.github.commitStatuses} />
            )}
            {sub.github?.reviews && <SharedRefresh label="Shared reviews" state={sub.github.reviews} />}
            {sub.error && (
              <Text c="red" size="sm">
                Source: {sub.error}
                {sub.errorSince ? ` · since ${time(sub.errorSince)}` : ""}
                {sub.errorObservedAt ? ` · last observed ${time(sub.errorObservedAt)}` : ""}
              </Text>
            )}
          </Stack>
        ))}
        <Accordion variant="contained" chevronPosition="right">
          <Accordion.Item value="diagnostics">
            <Accordion.Control>Inbox diagnostics</Accordion.Control>
            <Accordion.Panel>
              <Stack gap="xs">
                <Text size="xs" c="dimmed">
                  Inbox {inbox.id}
                </Text>
                <Text size="sm">
                  Cursors: latest {inbox.lastCursor.toString()} · notice-covered {inbox.covered.toString()} ·
                  acknowledged {inbox.acknowledged.toString()} · expired through {inbox.expiredThrough.toString()}
                </Text>
                {inbox.delivery?.quietUntil && (
                  <Text size="xs" c="dimmed">
                    Quiet until {time(inbox.delivery.quietUntil)} · maximum wait {time(inbox.delivery.maxWaitAt)}
                  </Text>
                )}
                {inbox.delivery?.nextWorkAt && (
                  <Text size="xs" c="dimmed">
                    Next scheduled inbox work: {time(inbox.delivery.nextWorkAt)} (may be source polling or delivery
                    retry)
                  </Text>
                )}
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </Paper>
  );
}

export function NotificationStatus({
  sandbox,
  sessionIds,
  opened,
  onClose,
}: {
  sandbox: string;
  sessionIds: readonly string[];
  opened: boolean;
  onClose: () => void;
}): JSX.Element {
  const [filter, setFilter] = useState<SubscriptionFilter>("not-cancelled");
  return (
    <Drawer opened={opened} onClose={onClose} title={`Notifications · ${sandbox}`} position="right" size="lg">
      <ScrollArea h="calc(100vh - 110px)">
        <Stack gap="md" pr="sm">
          <Text size="sm" c="dimmed">
            Read-only status. Runner confirmation does not mean the agent handled a notice.
          </Text>
          <Select
            label="Show subscriptions"
            value={filter}
            onChange={(value) => {
              if (
                value === "not-cancelled" ||
                value === "active" ||
                value === "expired" ||
                value === "cancelled" ||
                value === "all"
              )
                setFilter(value);
            }}
            data={[
              { value: "not-cancelled", label: "Not cancelled" },
              { value: "active", label: "Active" },
              { value: "expired", label: "Expired" },
              { value: "cancelled", label: "Cancelled" },
              { value: "all", label: "All" },
            ]}
            size="sm"
            w={200}
            allowDeselect={false}
          />
          {opened &&
            sessionIds.map((sessionId) => <InboxDetails key={sessionId} sessionId={sessionId} filter={filter} />)}
          {opened && sessionIds.length === 0 && <Text>No loaded Sessions.</Text>}
        </Stack>
      </ScrollArea>
    </Drawer>
  );
}
