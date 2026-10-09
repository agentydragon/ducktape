/** Read-only operator projection: curated entry summaries, no raw provider payloads or acknowledgement controls. */
import { Accordion, Divider, Drawer, Group, Paper, ScrollArea, Select, Stack, Text } from "@mantine/core";
import { useEffect, useState, type JSX } from "react";

import { followStream, type StreamConnection } from "./live_stream";

type Source = {
  provider: string;
  request_id?: string;
  repository?: string;
  subject?: { kind: string; number?: number; name?: string; sha?: string };
};
type GitHubRefresh = {
  last_success_at: string | null;
  error_kind: string | null;
  error: string | null;
  error_since: string | null;
  error_observed_at: string | null;
  retry_at: string | null;
  refreshing_until: string | null;
};
type GitHubStatus = {
  access: (GitHubRefresh & {
    app_id: number;
    installation_id: number;
    repository_id: number;
    checked_at: string | null;
    valid_until: string | null;
    currently_valid: boolean;
  })[];
  subject: GitHubRefresh & { repository_id: number; kind: string; subject_key: string };
};
type Subscription = {
  github: GitHubStatus | null;
  id: string;
  source: Source;
  cancelled: boolean;
  expires_at: string;
  last_success_at: string | null;
  error_kind: "rate_limited" | "unavailable" | "access_denied" | "source_changed" | "processing_error" | null;
  error_since: string | null;
  error_observed_at: string | null;
  error: string | null;
  next_source_check_at: string | null;
};
function SharedRefresh({ label, state }: { label: string; state: GitHubRefresh }): JSX.Element {
  return (
    <Text size="xs" c={state.error ? "red" : "dimmed"}>
      {label}:{" "}
      {state.error ?? (state.last_success_at ? `last success ${timestamp(state.last_success_at)}` : "not yet observed")}
      {state.error_since ? ` · since ${timestamp(state.error_since)}` : ""}
      {state.error_observed_at ? ` · last observed ${timestamp(state.error_observed_at)}` : ""}
      {state.retry_at ? ` · retry ${timestamp(state.retry_at)}` : ""}
      {state.refreshing_until ? ` · refresh lease until ${timestamp(state.refreshing_until)}` : ""}
    </Text>
  );
}
type Inbox = {
  inbox: {
    id: string;
    session_id: string;
    last_cursor: number;
    acknowledged: number;
    covered: number;
    expired_through: number;
    retired: boolean;
    delivery_error: string | null;
  };
  subscriptions: Subscription[];
  notice: {
    through_cursor: number;
    attempted: boolean;
    admitted: boolean;
    confirmed: boolean;
    error: string | null;
  } | null;
  unannounced_count: number;
  pending_acknowledgement_count: number;
  pending_entries: { cursor: number; created_at: string; provider: string; summary: string }[];
  pending_entries_more: boolean;
  notice_due_at: string | null;
  quiet_until: string | null;
  max_wait_at: string | null;
  notice_wait_reason: string | null;
  next_work_at: string | null;
};
type Status = { observed_at: string; inboxes: Inbox[] };
type SubscriptionState = "active" | "expired" | "cancelled";
type SubscriptionFilter = "not-cancelled" | SubscriptionState | "all";

function subscriptionState(sub: Subscription, observedAt?: string): SubscriptionState {
  if (sub.cancelled) return "cancelled";
  return new Date(sub.expires_at).getTime() <= new Date(observedAt ?? sub.expires_at).getTime() ? "expired" : "active";
}

function subscriptionLabel(sub: Subscription, observedAt?: string): string {
  const state = subscriptionState(sub, observedAt);
  if (state !== "active") return state;
  const errorKind =
    sub.error_kind ??
    sub.github?.access.find((access) => access.error_kind)?.error_kind ??
    sub.github?.subject.error_kind;
  if (errorKind) return errorKind.replaceAll("_", " ");
  if (sub.github?.access.some((access) => !access.currently_valid)) return "GitHub access not validated";
  return "no current source error";
}

function sourceLabel(source: Source): string {
  if (source.provider === "actions") return `Action ${source.request_id}`;
  if (source.provider === "github")
    return `${source.repository ?? "GitHub"} · ${source.subject?.kind ?? "event"} ${source.subject?.number ?? source.subject?.name ?? source.subject?.sha ?? ""}`;
  return source.provider;
}

function timestamp(value: string): string {
  return new Date(value).toLocaleString();
}

function entryTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
  const [data, setData] = useState<Status | null>(null);
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
            setData(JSON.parse(message.data) as Status);
            setError(null);
          } catch {
            setError("Invalid notification status snapshot");
          }
        },
      },
      onConnection: setConnection,
    });
  }, [opened, sandbox]);
  const observedAt = data?.observed_at;
  const inboxes =
    data?.inboxes
      .filter(({ inbox }) => !sessionId || inbox.session_id === sessionId)
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
                Snapshot: {timestamp(data.observed_at)}
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
                unannounced_count,
                pending_acknowledgement_count,
                pending_entries,
                pending_entries_more,
                notice_due_at,
                quiet_until,
                max_wait_at,
                notice_wait_reason,
                next_work_at,
              }) => (
                <Paper key={inbox.id} withBorder p="md">
                  <Stack gap="xs">
                    <Group justify="space-between">
                      <Text fw={600}>Session {inbox.session_id}</Text>
                      {inbox.retired && (
                        <Text size="xs" c="dimmed">
                          Retired
                        </Text>
                      )}
                    </Group>
                    {notice && (
                      <Text size="sm">
                        Latest notice through {notice.through_cursor}:{" "}
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
                    {inbox.delivery_error && (
                      <Text c="red" size="sm">
                        Delivery: {inbox.delivery_error}
                      </Text>
                    )}
                    {notice_wait_reason && (
                      <Text size="sm">
                        Notice: {notice_wait_reason.replaceAll("_", " ")}
                        {notice_due_at ? ` · eligible ${timestamp(notice_due_at)}` : ""}
                      </Text>
                    )}
                    <Divider
                      label={`Entries after acknowledgement · ${pending_acknowledgement_count} not acknowledged`}
                    />
                    {pending_entries.length === 0 && (
                      <Text size="sm" c="dimmed">
                        No retained unacknowledged entries
                      </Text>
                    )}
                    {pending_entries.length > 0 && (
                      <Stack gap={4}>
                        {pending_entries.map((entry, index) => (
                          <Stack key={entry.cursor} gap={4}>
                            {entry.cursor > inbox.covered &&
                              (index === 0 || (pending_entries[index - 1]?.cursor ?? 0) <= inbox.covered) && (
                                <Divider
                                  label={`Notice covered through #${inbox.covered} · ${unannounced_count} awaiting notice below`}
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
                                {entryTime(entry.created_at)}
                              </Text>
                            </Group>
                          </Stack>
                        ))}
                        {!pending_entries_more && (pending_entries.at(-1)?.cursor ?? 0) <= inbox.covered && (
                          <Divider
                            label={`Notice covered through #${inbox.covered} · ${unannounced_count} awaiting notice`}
                          />
                        )}
                      </Stack>
                    )}
                    {pending_entries_more && (
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
                          Expires {timestamp(sub.expires_at)}
                          {sub.next_source_check_at
                            ? ` · ${sub.error ? "retry" : "next source check"} ${timestamp(sub.next_source_check_at)}`
                            : ""}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {sub.last_success_at
                            ? `Last successful processing: ${timestamp(sub.last_success_at)}`
                            : "No successful processing recorded"}
                        </Text>
                        {sub.github && (
                          <Stack gap={2}>
                            {sub.github.access.map((access) => (
                              <Stack gap={0} key={`${access.app_id}/${access.installation_id}/${access.repository_id}`}>
                                <SharedRefresh
                                  label={`Shared GitHub access · repository ${access.repository_id} · installation ${access.installation_id}`}
                                  state={access}
                                />
                                <Text size="xs" c="dimmed">
                                  {access.currently_valid
                                    ? `Validated until ${timestamp(access.valid_until!)}`
                                    : "Access not currently validated"}
                                </Text>
                              </Stack>
                            ))}
                            <SharedRefresh
                              label={`Shared GitHub subject · ${sub.github.subject.kind} ${sub.github.subject.subject_key}`}
                              state={sub.github.subject}
                            />
                          </Stack>
                        )}
                        {sub.error && (
                          <Text c="red" size="xs">
                            Source: {sub.error} · since {timestamp(sub.error_since!)} · last observed{" "}
                            {timestamp(sub.error_observed_at!)}
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
                              Cursors: latest {inbox.last_cursor} · notice-covered {inbox.covered} · acknowledged{" "}
                              {inbox.acknowledged} · expired through {inbox.expired_through}
                            </Text>
                            {notice_due_at && (
                              <Text size="xs" c="dimmed">
                                Quiet until {timestamp(quiet_until!)} · maximum wait {timestamp(max_wait_at!)}
                              </Text>
                            )}
                            {next_work_at && (
                              <Text size="xs" c="dimmed">
                                Next scheduled inbox work: {timestamp(next_work_at)} (may be source polling or delivery
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
