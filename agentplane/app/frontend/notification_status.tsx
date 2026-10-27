/** Read-only operator projection: curated entry summaries, no raw provider payloads or acknowledgement controls. */
import { Accordion, Divider, Drawer, Group, Paper, ScrollArea, Select, Stack, Text } from "@mantine/core";
import { useEffect, useState, type JSX } from "react";

import { displayableError } from "./client";
import { clockTime, isoTime, timeAgo } from "./format_time";
import { followStream } from "./live_stream";
import { type StreamConnection } from "./stream_reader";
import {
  decodeSandboxNotificationStatus,
  type GitHubSubject,
  type InboxStatus,
  type SandboxNotificationStatus,
  type Source,
  type SourceObservation,
  type SubscriptionStatus,
} from "./notification_types";

type SubscriptionState = "active" | "expired" | "cancelled";
type SubscriptionFilter = "not-cancelled" | SubscriptionState | "all";

const SOURCE_FAILURE_KIND = "SOURCE_FAILURE_KIND_";
const NOTICE_WAIT_REASON = "NOTICE_WAIT_REASON_";

/** An enum value's name without its prefix, as lowercase words: `NOTICE_WAIT_REASON_DEBOUNCING` is "debouncing". */
function enumWords(name: string, prefix: string): string {
  return name.slice(prefix.length).toLowerCase().replaceAll("_", " ");
}

/** int64 arrives as a string, which would compare as text. */
function isAfter(cursor: string, other: string): boolean {
  return BigInt(cursor) > BigInt(other);
}

function SharedRefresh({ label, state }: { label: string; state: SourceObservation }): JSX.Element {
  return (
    <Text size="xs" c={state.error ? "red" : "dimmed"}>
      {label}:{" "}
      {state.error ?? (state.lastSuccessAt ? `last success ${timestamp(state.lastSuccessAt)}` : "not yet observed")}
      {state.errorSince ? ` · since ${timestamp(state.errorSince)}` : ""}
      {state.errorObservedAt ? ` · last observed ${timestamp(state.errorObservedAt)}` : ""}
      {state.retryAt ? ` · retry ${timestamp(state.retryAt)}` : ""}
      {state.refreshingUntil ? ` · refresh lease until ${timestamp(state.refreshingUntil)}` : ""}
    </Text>
  );
}

function subscriptionState(sub: SubscriptionStatus, observedAt?: string): SubscriptionState {
  if (sub.cancelled) return "cancelled";
  return new Date(sub.expiresAt).getTime() <= new Date(observedAt ?? sub.expiresAt).getTime() ? "expired" : "active";
}

function subscriptionLabel(sub: SubscriptionStatus, observedAt?: string): string {
  const state = subscriptionState(sub, observedAt);
  if (state !== "active") return state;
  const errorKind =
    sub.errorKind ?? sub.github?.access.find((access) => access.errorKind)?.errorKind ?? sub.github?.subject.errorKind;
  if (errorKind) return enumWords(errorKind, SOURCE_FAILURE_KIND);
  if (sub.github?.access.some((access) => !access.currentlyValid)) return "GitHub access not validated";
  return "no current source error";
}

function subjectLabel(subject: GitHubSubject | undefined): string {
  if (subject === undefined) return "event";
  if (subject.pullRequest !== undefined) return `pull request ${subject.pullRequest.number}`;
  if (subject.issue !== undefined) return `issue ${subject.issue.number}`;
  if (subject.branch !== undefined) return `branch ${subject.branch.name}`;
  if (subject.commit !== undefined) return `commit ${subject.commit.sha}`;
  throw new TypeError("notification GitHub subject has no kind");
}

function sourceLabel(source: Source): string {
  if (source.actions !== undefined) return `Action ${source.actions.requestId}`;
  if (source.github !== undefined) return `${source.github.repository} · ${subjectLabel(source.github.subject)}`;
  throw new TypeError("notification source has no provider");
}

/** Plain text for the strings built from it; the ISO time stands where a tooltip cannot. */
function timestamp(value: string): string {
  return `${timeAgo(value)} (${isoTime(value)})`;
}

export function NotificationStatus({
  sandbox,
  sessionId,
  opened,
  onClose,
}: {
  sandbox: string;
  sessionId?: string;
  opened: boolean;
  onClose: () => void;
}): JSX.Element {
  const [data, setData] = useState<SandboxNotificationStatus | null>(null);
  const [subscriptionFilter, setSubscriptionFilter] = useState<SubscriptionFilter>("not-cancelled");
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    if (!opened) return;
    setData(null);
    setError(null);
    return followStream(`/sandboxes/${encodeURIComponent(sandbox)}/notifications/stream`, {
      events: {
        snapshot: (message) => {
          try {
            setData(decodeSandboxNotificationStatus(message.data));
            setError(null);
            return undefined;
          } catch (reason) {
            const failure = displayableError(reason);
            setError(failure);
            return failure;
          }
        },
      },
      onConnection: setConnection,
    });
  }, [opened, sandbox]);
  const observedAt = data?.observedAt;
  const inboxStatuses: InboxStatus[] = data?.inboxes ?? [];
  const inboxes =
    inboxStatuses
      .filter(({ inbox }) => !sessionId || inbox.sessionId === sessionId)
      .map((status) => ({
        ...status,
        subscriptions: status.subscriptions.filter((sub) => {
          const state = subscriptionState(sub, observedAt);
          return (
            subscriptionFilter === "all" ||
            (subscriptionFilter === "not-cancelled" ? state !== "cancelled" : state === subscriptionFilter)
          );
        }),
      })) ?? [];
  return (
    <>
      <Drawer opened={opened} onClose={onClose} title={`Notifications · ${sandbox}`} position="right" size="lg">
        <ScrollArea h="calc(100vh - 110px)">
          <Stack gap="md" pr="sm">
            <Text size="sm" c="dimmed">
              Read-only status. Runner confirmation does not mean the agent handled a notice.
            </Text>
            {data && (
              <Text size="xs" c="dimmed">
                Snapshot: {timestamp(data.observedAt)}
                {connection.phase !== "live" ? " · disconnected; showing last snapshot" : ""}
              </Text>
            )}
            {!data && connection.phase === "connecting" && <Text>Loading notification status…</Text>}
            {!data && connection.phase === "reconnecting" && (
              <Text role="status">Reconnecting to notification status…</Text>
            )}
            {error && (
              <Text role="alert" c="red">
                {error}
                {data ? " · showing last snapshot" : ""}
              </Text>
            )}
            <Select
              label="Show subscriptions"
              value={subscriptionFilter}
              onChange={(value) => {
                if (
                  value === "not-cancelled" ||
                  value === "active" ||
                  value === "expired" ||
                  value === "cancelled" ||
                  value === "all"
                )
                  setSubscriptionFilter(value);
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
            {data && inboxes.length === 0 && (
              <Text>No inbox for {sessionId ? "this runner session" : "this Sandbox incarnation"}.</Text>
            )}
            {inboxes.map(
              ({
                inbox,
                subscriptions,
                notice,
                unannouncedCount,
                pendingAcknowledgementCount,
                pendingEntries,
                pendingEntriesMore,
                noticeDueAt,
                quietUntil,
                maxWaitAt,
                noticeWaitReason,
                nextWorkAt,
              }) => (
                <Paper key={inbox.id} withBorder p="md">
                  <Stack gap="xs">
                    <Group justify="space-between">
                      <Text fw={600}>Session {inbox.sessionId}</Text>
                      {inbox.retired && (
                        <Text size="xs" c="dimmed">
                          Retired
                        </Text>
                      )}
                    </Group>
                    {notice && (
                      <Text size="sm">
                        Latest notice through {notice.throughCursor}:{" "}
                        {notice.error
                          ? `error: ${notice.error}`
                          : notice.confirmed
                            ? "harness confirmed"
                            : notice.admitted
                              ? "runner admitted; awaiting confirmation"
                              : notice.attempted
                                ? "delivery attempted"
                                : "prepared"}
                      </Text>
                    )}
                    {inbox.deliveryError && (
                      <Text c="red" size="sm">
                        Delivery: {inbox.deliveryError}
                      </Text>
                    )}
                    {noticeWaitReason && (
                      <Text size="sm">
                        Notice: {enumWords(noticeWaitReason, NOTICE_WAIT_REASON)}
                        {noticeDueAt ? ` · eligible ${timestamp(noticeDueAt)}` : ""}
                      </Text>
                    )}
                    <Divider
                      label={`Entries after acknowledgement · ${pendingAcknowledgementCount} not acknowledged`}
                    />
                    {pendingEntries.length === 0 && (
                      <Text size="sm" c="dimmed">
                        No retained unacknowledged entries
                      </Text>
                    )}
                    {pendingEntries.length > 0 && (
                      <Stack gap={4}>
                        {pendingEntries.map((entry, index) => (
                          <Stack key={entry.cursor} gap={4}>
                            {isAfter(entry.cursor, inbox.covered) &&
                              (index === 0 || !isAfter(pendingEntries[index - 1]?.cursor ?? "0", inbox.covered)) && (
                                <Divider
                                  label={`Notice covered through #${inbox.covered} · ${unannouncedCount} awaiting notice below`}
                                />
                              )}
                            <Group gap="xs" wrap="nowrap" align="flex-start" py={4}>
                              <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                                #{entry.cursor}
                              </Text>
                              <Text size="sm" style={{ overflowWrap: "anywhere", flex: 1 }}>
                                {entry.summary}
                              </Text>
                              <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                                {clockTime(entry.createdAt)}
                              </Text>
                            </Group>
                          </Stack>
                        ))}
                        {!pendingEntriesMore && !isAfter(pendingEntries.at(-1)?.cursor ?? "0", inbox.covered) && (
                          <Divider
                            label={`Notice covered through #${inbox.covered} · ${unannouncedCount} awaiting notice`}
                          />
                        )}
                      </Stack>
                    )}
                    {pendingEntriesMore && (
                      <Text size="xs" c="dimmed">
                        Showing the first 100 retained entries after acknowledgement; more entries remain.
                      </Text>
                    )}
                    <Divider label="Subscriptions" />
                    {subscriptions.length === 0 && (
                      <Text size="sm" c="dimmed">
                        No subscriptions to show
                      </Text>
                    )}
                    {subscriptions.map((sub) => (
                      <Stack gap={2} key={sub.id}>
                        <Text size="sm">
                          {sourceLabel(sub.source)} · {subscriptionLabel(sub, observedAt)}
                        </Text>
                        <Text size="xs" c="dimmed">
                          Expires {timestamp(sub.expiresAt)}
                          {sub.nextSourceCheckAt
                            ? ` · ${sub.error ? "retry" : "next source check"} ${timestamp(sub.nextSourceCheckAt)}`
                            : ""}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {sub.lastSuccessAt
                            ? `Last successful processing: ${timestamp(sub.lastSuccessAt)}`
                            : "No successful processing recorded"}
                        </Text>
                        {sub.github && (
                          <Stack gap={2}>
                            {sub.github.access.map((access) => (
                              <Stack gap={0} key={`${access.appId}/${access.installationId}/${access.repositoryId}`}>
                                <SharedRefresh
                                  label={`Shared GitHub access · repository ${access.repositoryId} · installation ${access.installationId}`}
                                  state={access}
                                />
                                <Text size="xs" c="dimmed">
                                  {access.currentlyValid
                                    ? `Validated until ${timestamp(access.validUntil!)}`
                                    : "Access not currently validated"}
                                </Text>
                              </Stack>
                            ))}
                            <SharedRefresh
                              label={`Shared GitHub subject · ${subjectLabel(sub.github.subject.subject)}`}
                              state={sub.github.subject}
                            />
                          </Stack>
                        )}
                        {sub.error && (
                          <Text c="red" size="xs">
                            Source: {sub.error}
                            {sub.errorSince ? ` · since ${timestamp(sub.errorSince)}` : ""}
                            {sub.errorObservedAt ? ` · last observed ${timestamp(sub.errorObservedAt)}` : ""}
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
                              Cursors: latest {inbox.lastCursor} · notice-covered {inbox.covered} · acknowledged{" "}
                              {inbox.acknowledged} · expired through {inbox.expiredThrough}
                            </Text>
                            {noticeDueAt && (
                              <Text size="xs" c="dimmed">
                                Quiet until {timestamp(quietUntil!)} · maximum wait {timestamp(maxWaitAt!)}
                              </Text>
                            )}
                            {nextWorkAt && (
                              <Text size="xs" c="dimmed">
                                Next scheduled inbox work: {timestamp(nextWorkAt)} (may be source polling or delivery
                                retry)
                              </Text>
                            )}
                          </Stack>
                        </Accordion.Panel>
                      </Accordion.Item>
                    </Accordion>
                  </Stack>
                </Paper>
              )
            )}
          </Stack>
        </ScrollArea>
      </Drawer>
    </>
  );
}
