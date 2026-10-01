import type { JSX, ReactNode } from "react";
import { Alert, Badge, Button, Code, Group, Paper, SimpleGrid, Stack, Text, Title } from "@mantine/core";

import type { SyncStatus } from "./api";
import { ago, every, lastEvent, nextPoll, plural, untilExpiry } from "./status";

type Props = {
  status: SyncStatus;
  now: number;
  onSyncNow: () => void;
};

function StatusDetail({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <Stack component="div" gap={2} style={{ minWidth: 0 }}>
      <Text component="dt" size="xs" c="dimmed">
        {label}
      </Text>
      <Text component="dd" size="sm" style={{ margin: 0, overflowWrap: "anywhere" }}>
        {children}
      </Text>
    </Stack>
  );
}

export function Overview({ status, now, onSyncNow }: Props): JSX.Element {
  const { credential, last_cycle: cycle, last_failure: failure, live } = status;
  const stateLabel = status.state === "syncing" ? "Syncing" : status.state === "unpaired" ? "Not paired" : "Idle";
  const stateColor = status.state === "syncing" ? "blue" : status.state === "unpaired" ? "gray" : "green";

  return (
    <Paper component="section" aria-labelledby="overview-heading" withBorder radius="md" p="md">
      <Stack gap="md">
        <Group justify="space-between" align="center" gap="xs">
          <Title order={2} id="overview-heading">
            Sync
          </Title>
          <Badge variant="light" color={stateColor}>
            {stateLabel}
          </Badge>
        </Group>
        <Text>
          {plural(status.sessions, "session")} stored.{" "}
          {credential === null
            ? "Not paired, so nothing is syncing."
            : "Live following keeps the sessions in use current as events happen; polling catches up on everything else."}
        </Text>
        {credential !== null && (
          <>
            <Stack gap="xs">
              <Title order={3}>Live following</Title>
              <Text size="sm" c="dimmed">
                {live.following
                  ? "Streams the events of sessions in use as they happen; a session watch finds new sessions at once."
                  : "Off: only polling runs."}
              </Text>
              {live.following && (
                <SimpleGrid component="dl" cols={{ base: 1, sm: 2 }} spacing="sm">
                  <StatusDetail label="Streaming">{plural(live.streams, "session")}</StatusDetail>
                  <StatusDetail label="Session watch">
                    {live.watching
                      ? "Connected"
                      : "Not connected; new sessions are found by a frequent list check instead"}
                  </StatusDetail>
                  <StatusDetail label="Last event stored">{lastEvent(live.last_event_at, now)}</StatusDetail>
                </SimpleGrid>
              )}
            </Stack>
            <Stack gap="xs">
              <Title order={3}>Polling</Title>
              <Text size="sm" c="dimmed">
                Every {every(status.poll_interval_seconds)} it lists every session and reads the events of any that
                moved on. It needs no live following, and catches whatever that missed.
              </Text>
              <SimpleGrid component="dl" cols={{ base: 1, sm: 2 }} spacing="sm">
                <StatusDetail label="State">{status.state === "syncing" ? "Running now" : "Idle"}</StatusDetail>
                <StatusDetail label="Last poll">
                  {cycle === null
                    ? "None yet"
                    : `${ago(cycle.finished_at, now)}: ${plural(cycle.behind, "session")} had moved on, ${plural(cycle.events_read, "event")} read`}
                </StatusDetail>
                <StatusDetail label="Next poll">
                  {status.state === "syncing" || cycle === null
                    ? "When this one ends"
                    : nextPoll(cycle.finished_at, status.poll_interval_seconds, now)}
                </StatusDetail>
                <StatusDetail label="Behind">
                  {plural(status.sessions_behind, "session")} (sessions with a live stream are not counted)
                </StatusDetail>
              </SimpleGrid>
              <Group>
                <Button type="button" onClick={onSyncNow} disabled={status.state === "syncing"}>
                  Poll now
                </Button>
              </Group>
            </Stack>
            <Stack gap="xs">
              <Title order={3}>Grant</Title>
              <SimpleGrid component="dl" cols={{ base: 1, sm: 2 }} spacing="sm">
                <StatusDetail label="Organization">
                  <Code>{credential.organization_uuid}</Code>
                </StatusDetail>
                <StatusDetail label="Scopes">
                  <Code>{credential.scopes.join(" ")}</Code>
                </StatusDetail>
                <StatusDetail label="Access token">
                  Refreshes {untilExpiry(credential.access_token_expires_at, now)}
                </StatusDetail>
              </SimpleGrid>
            </Stack>
          </>
        )}
        {failure !== null && (
          <Alert color="red" role="alert" title="Last poll failed">
            The last poll failed {ago(failure.at, now)}: {failure.message}
          </Alert>
        )}
        {live.problems.map((problem) => (
          <Alert key={problem.source} color="red" role="alert" title={`${problem.source} is retrying`}>
            {problem.source} last failed {ago(problem.at, now)} and is retrying: {problem.message}
          </Alert>
        ))}
        {live.failure !== null && (
          <Alert color="red" role="alert" title="Live following stopped">
            Live following stopped {ago(live.failure.at, now)}, and polling carries on: {live.failure.message}
          </Alert>
        )}
      </Stack>
    </Paper>
  );
}
