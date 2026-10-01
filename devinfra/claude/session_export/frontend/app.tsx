import { useCallback, useEffect, useState, type JSX } from "react";
import { Alert, Container, Stack, Text, Title } from "@mantine/core";

import { ApiError, getStatus, syncNow, type SyncStatus } from "./api";
import { Overview } from "./overview";
import { Pairing } from "./pairing";
import { SessionViewer } from "./viewer";

/** A cycle moves the counts, and pairing or "Poll now" should show up without a reload. */
const POLL_INTERVAL_MS = 5_000;
/** Relative times move between polls, so tick them like a clock. */
const TICK_MS = 1_000;

export function App(): JSX.Element {
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
    <Container component="main" size="xl" py="xl">
      <Stack gap="md">
        <Title order={1}>Claude session sync</Title>
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
            <SessionViewer />
          </>
        )}
      </Stack>
    </Container>
  );
}
