import { useCallback, useEffect, useState, type JSX } from "react";
import { Alert, Button, Container, Group, Stack, Text, Title } from "@mantine/core";

import { ApiError, getStatus, syncNow, type SyncStatus } from "./api";
import { Overview } from "./overview";
import { Pairing } from "./pairing";
import { SessionViewer } from "./viewer";

/** A cycle moves the counts, and pairing or "Poll now" should show up without a reload. */
const POLL_INTERVAL_MS = 5_000;
/** Relative times move between polls, so tick them like a clock. */
const TICK_MS = 1_000;

function SyncPage(): JSX.Element {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<{ title: string; message: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setStatus(await getStatus());
      setError(null);
    } catch (reason) {
      setError({
        title: "Sync status unavailable",
        message: reason instanceof ApiError || reason instanceof Error ? reason.message : "Could not load the status.",
      });
    }
  }, []);

  useEffect(() => {
    void refresh();
    const poll = globalThis.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => globalThis.clearInterval(poll);
  }, [refresh]);

  useEffect(() => {
    const tick = globalThis.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => globalThis.clearInterval(tick);
  }, []);

  const requestSync = useCallback(async (): Promise<void> => {
    try {
      await syncNow();
      await refresh();
    } catch (reason) {
      setError({
        title: "Sync could not start",
        message: reason instanceof Error ? reason.message : "Could not start a sync.",
      });
    }
  }, [refresh]);

  return (
    <Stack gap="md">
      {error !== null && (
        <Alert color="red" role="alert" title={error.title}>
          {error.message}
        </Alert>
      )}
      {status === null ? (
        error === null && <Text>Loading…</Text>
      ) : (
        <>
          <Overview status={status} now={now} onSyncNow={() => void requestSync()} />
          <Pairing paired={status.credential !== null} onPaired={setStatus} />
        </>
      )}
    </Stack>
  );
}

type AppProps = { pathname?: string };

export function App({ pathname = window.location.pathname }: AppProps = {}): JSX.Element {
  const page = pathname === "/sync" ? "sync" : "sessions";

  return (
    <Container size="xl" py="xl">
      <Stack gap="md">
        <Group component="header" justify="space-between" align="center" gap="md" wrap="wrap">
          <Title order={1}>Claude session sync</Title>
          <Group component="nav" aria-label="Pages" gap="xs">
            <Button
              component="a"
              href="/sessions"
              variant={page === "sessions" ? "light" : "subtle"}
              aria-current={page === "sessions" ? "page" : undefined}
            >
              Sessions
            </Button>
            <Button
              component="a"
              href="/sync"
              variant={page === "sync" ? "light" : "subtle"}
              aria-current={page === "sync" ? "page" : undefined}
            >
              Sync status
            </Button>
          </Group>
        </Group>
        <Stack component="main" gap="md">
          {page === "sync" ? <SyncPage /> : <SessionViewer />}
        </Stack>
      </Stack>
    </Container>
  );
}
