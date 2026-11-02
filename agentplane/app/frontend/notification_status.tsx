/** Read-only operator projection: curated entry summaries, no raw provider payloads or acknowledgement controls. */
import { Accordion, Divider, Drawer, Group, Paper, ScrollArea, Select, Stack, Text } from "@mantine/core";
import { useEffect, useState, type JSX } from "react";
import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import { clockTime, isoTime, timeAgo } from "./format_time";
import { followStream } from "./live_stream";
import { decodeNotificationWatchFrame } from "./notification_watch";
import { followWatch } from "./watch_stream";
import { type StreamConnection } from "./stream_reader";
import {
  NoticeWaitReason,
  SandboxNotificationStatusSchema,
  type SandboxNotificationStatus,
  type SubscriptionStatus,
} from "../../notification_service/notifications_operator_pb";
import {
  SourceFailureKind,
  type GitHubSubject,
  type Source,
  type GitHubAccessStatus,
  type GitHubSubjectStatus,
} from "../../notification_service/notifications_pb";
import type { Timestamp } from "@bufbuild/protobuf/wkt";

type SubscriptionState = "active" | "expired" | "cancelled";
type SubscriptionFilter = "not-cancelled" | SubscriptionState | "all";

/** Generated enum names omit their protobuf prefix. */
function enumWords(name: string): string {
  return name.toLowerCase().replaceAll("_", " ");
}

/** Protobuf int64 values are bigint. */
function isAfter(cursor: bigint, other: bigint): boolean {
  return cursor > other;
}

function SharedRefresh({
  label,
  state,
}: {
  label: string;
  state: GitHubAccessStatus | GitHubSubjectStatus;
}): JSX.Element {
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

function subscriptionState(sub: SubscriptionStatus, observedAt?: Timestamp): SubscriptionState {
  if (sub.cancelled) return "cancelled";
  return date(sub.expiresAt).getTime() <= date(observedAt ?? sub.expiresAt).getTime() ? "expired" : "active";
}

function subscriptionLabel(sub: SubscriptionStatus, observedAt?: Timestamp): string {
  const state = subscriptionState(sub, observedAt);
  if (state !== "active") return state;
  const errorKind =
    sub.errorKind ?? sub.github?.access.find((access) => access.errorKind)?.errorKind ?? sub.github?.subject?.errorKind;
  if (errorKind) return enumWords(SourceFailureKind[errorKind]);
  if (sub.github?.access.some((access) => !access.currentlyValid)) return "GitHub access not validated";
  return "no current source error";
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
  if (source?.provider.case === "actions") return `Action ${source.provider.value.requestId}`;
  if (source?.provider.case === "github")
    return `${source.provider.value.repository} · ${subjectLabel(source.provider.value.subject)}`;
  return "Source";
}

/** Plain text for the strings built from it; the ISO time stands where a tooltip cannot. */
function date(value: Timestamp | undefined): Date {
  return new Date(Number(value?.seconds ?? 0n) * 1000 + Math.floor((value?.nanos ?? 0) / 1_000_000));
}

function timestamp(value: Timestamp | undefined): string {
  const iso = date(value).toISOString();
  return `${timeAgo(iso)} (${isoTime(iso)})`;
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
    let firstOpen = true;
    let sawSnapshot = false;
    let stop: () => void = () => {};
    let usingLegacy = false;
    const startLegacy = () => {
      if (usingLegacy) return;
      usingLegacy = true;
      stop();
      // A new bundle can hit an old replica during a rolling deployment.
      stop = followStream(`/sandboxes/${encodeURIComponent(sandbox)}/notifications/stream`, {
        events: {
          snapshot: (message) => {
            setData(fromJson(SandboxNotificationStatusSchema, JSON.parse(message.data) as JsonValue));
            setError(null);
          },
        },
        onConnection: setConnection,
      });
    };
    stop = followWatch(
      `/upstream/notifications/v1/operator/sandboxes/${encodeURIComponent(sandbox)}/notifications:watch`,
      {
        initialFrameTimeoutMs: 5_000,
        onNotFound: startLegacy,
        onOpen: () => {
          if (firstOpen) setData(null);
          firstOpen = false;
          sawSnapshot = false;
        },
        onFrame: (json) => {
          try {
            const frame = decodeNotificationWatchFrame(json, sawSnapshot);
            if (frame.kind === "snapshot") {
              sawSnapshot = true;
              setData(frame.status);
              setError(null);
            } else {
              return "reconnect";
            }
          } catch (reason) {
            setError("Invalid notification status snapshot");
            throw reason;
          }
        },
        onConnection: setConnection,
      }
    );
    return () => stop();
  }, [opened, sandbox]);
  const observedAt = data?.observedAt;
  const inboxes =
    data?.inboxes
      .filter(({ inbox }) => inbox && (!sessionId || inbox.sessionId === sessionId))
      .map((status) => ({
        ...status,
        inbox: status.inbox!,
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
                        Notice: {enumWords(NoticeWaitReason[noticeWaitReason])}
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
                              (index === 0 || !isAfter(pendingEntries[index - 1]?.cursor ?? 0n, inbox.covered)) && (
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
                                {clockTime(date(entry.createdAt).toISOString())}
                              </Text>
                            </Group>
                          </Stack>
                        ))}
                        {!pendingEntriesMore && !isAfter(pendingEntries.at(-1)?.cursor ?? 0n, inbox.covered) && (
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
                                    ? `Validated until ${timestamp(access.validUntil)}`
                                    : "Access not currently validated"}
                                </Text>
                              </Stack>
                            ))}
                            {sub.github.subject && (
                              <SharedRefresh
                                label={`Shared GitHub subject · ${subjectLabel(sub.github.subject.subject)}`}
                                state={sub.github.subject}
                              />
                            )}
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
