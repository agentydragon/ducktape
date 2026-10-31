import { useEffect, useState, type JSX } from "react";
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Paper,
  Progress,
  ScrollArea,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { Link, useLocation } from "react-router";

import { httpApi, type SessionBrowserApi } from "./api";
import type { MachineImportProgress, SnapshotImportState } from "./schema";

const IMPORT_PROGRESS_POLL_INTERVAL_MS = 10_000;

export type MachineImportProgressState = {
  progress: MachineImportProgress[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
};

export function useMachineImportProgress(api: SessionBrowserApi): MachineImportProgressState {
  const [progress, setProgress] = useState<MachineImportProgress[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshRequest, setRefreshRequest] = useState(0);

  useEffect(() => {
    let disposed = false;
    let timeout: ReturnType<typeof setTimeout> | null = null;
    let activeRequest: AbortController | null = null;
    const refresh = async (): Promise<void> => {
      const controller = new AbortController();
      activeRequest = controller;
      setLoading(true);
      try {
        const nextProgress = await api.listImportProgress(controller.signal);
        if (!disposed && !controller.signal.aborted) {
          setProgress(nextProgress);
          setError(null);
        }
      } catch (reason) {
        if (!disposed && !controller.signal.aborted) {
          setError(errorMessage(reason, "The archive service did not return import progress."));
        }
      } finally {
        if (!disposed && activeRequest === controller) {
          activeRequest = null;
          setLoading(false);
          timeout = setTimeout(() => void refresh(), IMPORT_PROGRESS_POLL_INTERVAL_MS);
        }
      }
    };
    void refresh();
    return () => {
      disposed = true;
      activeRequest?.abort();
      if (timeout !== null) clearTimeout(timeout);
    };
  }, [api, refreshRequest]);

  return {
    progress,
    loading,
    error,
    refresh: () => setRefreshRequest((current) => current + 1),
  };
}

export function machineImportIsIncomplete(machine: MachineImportProgress): boolean {
  const unaccountedSnapshotCount =
    machine.known_snapshot_count - machine.completed_snapshot_count - machine.failed_snapshot_count;
  return (
    machine.oldest_unfinished_snapshot_state !== null ||
    unaccountedSnapshotCount > 0 ||
    machine.failed_snapshot_count > 0 ||
    machine.codex_metadata_pending_snapshot_count > 0 ||
    machine.codex_metadata_failed_snapshot_count > 0 ||
    machine.pending_file_count > 0 ||
    machine.failed_file_count > 0
  );
}

function machineImportIsActive(machine: MachineImportProgress): boolean {
  const unaccountedSnapshotCount =
    machine.known_snapshot_count - machine.completed_snapshot_count - machine.failed_snapshot_count;
  return (
    (machine.oldest_unfinished_snapshot_state !== null &&
      machine.oldest_unfinished_snapshot_state.endsWith("_pending")) ||
    unaccountedSnapshotCount > 0 ||
    machine.codex_metadata_pending_snapshot_count > 0 ||
    machine.pending_file_count > 0
  );
}

export function MachineImportProgressSummary({
  progress,
  loading,
  error,
  returnTo,
}: {
  progress: MachineImportProgress[];
  loading: boolean;
  error: string | null;
  returnTo: string;
}): JSX.Element {
  const incompleteMachines = progress.filter(machineImportIsIncomplete);
  return (
    <Group
      component="section"
      aria-label="Import progress by machine"
      className="session-import-summary"
      gap="xs"
      wrap="wrap"
    >
      {error !== null && (
        <Text size="xs" c="red" role="status" title={`Could not refresh import progress: ${error}`}>
          Import progress unavailable
        </Text>
      )}
      {progress.length === 0 && loading ? (
        <Loader size="xs" aria-label="Loading import progress" />
      ) : progress.length === 0 ? (
        <Text size="xs" c="dimmed">
          No machines registered
        </Text>
      ) : incompleteMachines.length === 0 ? (
        <Text size="xs" c="dimmed">
          All imports complete
        </Text>
      ) : (
        incompleteMachines.map((machine) => {
          const totalFileCount = machine.processed_file_count + machine.pending_file_count + machine.failed_file_count;
          const processedPercentage = totalFileCount === 0 ? 0 : (machine.processed_file_count / totalFileCount) * 100;
          const pendingPercentage = totalFileCount === 0 ? 0 : (machine.pending_file_count / totalFileCount) * 100;
          const failedPercentage = totalFileCount === 0 ? 0 : (machine.failed_file_count / totalFileCount) * 100;

          return (
            <Box
              component="section"
              key={machine.machine_id}
              aria-label={`${machine.display_name} import status`}
              className="session-import-machine"
            >
              <Text size="xs" fw={500} className="session-import-machine-name" title={machine.display_name}>
                {machine.display_name}
              </Text>
              {machineImportIsActive(machine) ? (
                <Loader
                  size={12}
                  role="status"
                  aria-label={`Syncing ${machine.display_name}`}
                  className="session-import-machine-state"
                />
              ) : (
                <Badge size="xs" variant="light" color="red" className="session-import-machine-state">
                  Needs attention
                </Badge>
              )}
              {totalFileCount > 0 ? (
                <>
                  <Progress.Root
                    role="progressbar"
                    aria-label={`${machine.display_name} file-entry progress`}
                    aria-valuemin={0}
                    aria-valuemax={totalFileCount}
                    aria-valuenow={machine.processed_file_count}
                    aria-valuetext={`${machine.processed_file_count} processed, ${machine.pending_file_count} pending, ${machine.failed_file_count} failed of ${totalFileCount} inventoried file entries`}
                    size="xs"
                    className="session-import-machine-progress"
                  >
                    {processedPercentage > 0 && <Progress.Section value={processedPercentage} color="blue" />}
                    {pendingPercentage > 0 && <Progress.Section value={pendingPercentage} color="yellow" />}
                    {failedPercentage > 0 && <Progress.Section value={failedPercentage} color="red" />}
                  </Progress.Root>
                  <Text size="xs" c="dimmed" className="session-import-machine-counts">
                    {machine.processed_file_count}/{totalFileCount}
                    {machine.failed_file_count > 0 ? ` · ${machine.failed_file_count} failed` : ""}
                  </Text>
                </>
              ) : (
                <Text size="xs" c="dimmed" className="session-import-machine-counts">
                  {snapshotStateLabel(machine.oldest_unfinished_snapshot_state)}
                </Text>
              )}
            </Box>
          );
        })
      )}
      <Button
        component={Link}
        to={{ pathname: "/", search: "?view=import-progress" }}
        state={{ returnTo }}
        size="xs"
        variant="subtle"
      >
        Import details
      </Button>
    </Group>
  );
}

export function MachineImportProgressPage({ api }: { api?: SessionBrowserApi }): JSX.Element {
  const location = useLocation();
  const { progress, loading, error, refresh } = useMachineImportProgress(api ?? httpApi);
  const returnTo = returnPath(location.state);

  return (
    <Stack gap="sm" style={{ height: "100%", minHeight: 0 }}>
      <Group justify="space-between" align="center" wrap="wrap">
        <Box>
          <Title order={1} size="h3">
            Import progress details
          </Title>
          <Text size="sm" c="dimmed">
            Full snapshot, file-entry, and Codex metadata status for every registered machine.
          </Text>
        </Box>
        <Button component={Link} to={returnTo} replace variant="default">
          Back to sessions
        </Button>
      </Group>
      <MachineImportProgressPanel progress={progress} loading={loading} error={error} onRefresh={refresh} />
    </Stack>
  );
}

function MachineImportProgressPanel({
  progress,
  loading,
  error,
  onRefresh,
}: {
  progress: MachineImportProgress[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}): JSX.Element {
  const progressExplanation = (
    <>
      Snapshot counts include registered snapshots, even if Restic later prunes them, and can grow as more are
      discovered. File counts cover entries inventoried so far; progress uses these entries as its denominator, and that
      total can grow as snapshots are inventoried. Processed means handled, not necessarily imported as a transcript.
      Codex metadata counts cover registered snapshots; completed snapshots can have partial metadata, shown separately.
    </>
  );

  return (
    <Paper component="section" aria-label="Import progress by machine" withBorder radius="sm" p="xs">
      <Stack gap="xs">
        <Group justify="space-between" align="center">
          <Text size="sm" fw={600}>
            Import progress by machine
          </Text>
          <Group gap="xs">
            <Text size="xs" c="dimmed" className="session-import-progress-refresh-note">
              Progress refreshes every 10 seconds; refresh sessions separately
            </Text>
            <Button size="xs" variant="default" loading={loading} onClick={onRefresh}>
              Refresh
            </Button>
          </Group>
        </Group>
        <Text size="xs" c="dimmed">
          {progressExplanation}
        </Text>
        {error !== null && (
          <Alert color="red" title="Could not refresh import progress">
            {error}
          </Alert>
        )}
        {progress.length === 0 ? (
          loading ? (
            <Center h={36}>
              <Loader size="xs" aria-label="Loading import progress" />
            </Center>
          ) : (
            <Text size="xs" c="dimmed">
              No machines are registered yet.
            </Text>
          )
        ) : (
          <ScrollArea className="session-import-progress-machines" type="auto">
            <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="xs">
              {progress.map((machine) => {
                const totalFileCount =
                  machine.processed_file_count + machine.pending_file_count + machine.failed_file_count;
                const processedPercentage =
                  totalFileCount === 0 ? 0 : (machine.processed_file_count / totalFileCount) * 100;
                const pendingPercentage =
                  totalFileCount === 0 ? 0 : (machine.pending_file_count / totalFileCount) * 100;
                const failedPercentage = totalFileCount === 0 ? 0 : (machine.failed_file_count / totalFileCount) * 100;

                return (
                  <Paper key={machine.machine_id} withBorder radius="xs" p="xs">
                    <Stack gap={2}>
                      <Group justify="space-between" gap="xs" wrap="wrap">
                        <Text size="sm" fw={600}>
                          {machine.display_name}
                        </Text>
                        {machine.oldest_unfinished_snapshot_state !== null && (
                          <Badge color={snapshotStateColor(machine.oldest_unfinished_snapshot_state)} variant="light">
                            {snapshotStateLabel(machine.oldest_unfinished_snapshot_state)}
                          </Badge>
                        )}
                      </Group>
                      <Text size="xs" c="dimmed">
                        Snapshots: {machine.completed_snapshot_count} of {machine.known_snapshot_count} registered
                        complete · {machine.failed_snapshot_count} failed
                      </Text>
                      <Text size="xs" c="dimmed">
                        Codex metadata: {machine.codex_metadata_pending_snapshot_count} pending ·{" "}
                        {machine.codex_metadata_failed_snapshot_count} failed ·{" "}
                        {machine.codex_metadata_partial_snapshot_count} partial
                      </Text>
                      <Text size="xs" c="dimmed">
                        Oldest unfinished snapshot:{" "}
                        {machine.oldest_unfinished_snapshot_state === null
                          ? machine.known_snapshot_count === 0
                            ? "none registered"
                            : "none; all registered snapshots complete"
                          : formatDateTime(machine.oldest_unfinished_snapshot_at)}
                      </Text>
                      {totalFileCount === 0 ? (
                        <Text size="xs" c="dimmed">
                          No file entries inventoried yet.
                        </Text>
                      ) : (
                        <>
                          <Text size="xs" c="dimmed">
                            File entries processed: {machine.processed_file_count} of {totalFileCount} inventoried
                          </Text>
                          <Progress.Root
                            role="progressbar"
                            aria-label={`${machine.display_name} file-entry progress`}
                            aria-valuemin={0}
                            aria-valuemax={totalFileCount}
                            aria-valuenow={machine.processed_file_count}
                            aria-valuetext={`${machine.processed_file_count} processed, ${machine.pending_file_count} pending, ${machine.failed_file_count} failed of ${totalFileCount} inventoried file entries`}
                            size="sm"
                          >
                            {processedPercentage > 0 && <Progress.Section value={processedPercentage} color="blue" />}
                            {pendingPercentage > 0 && <Progress.Section value={pendingPercentage} color="yellow" />}
                            {failedPercentage > 0 && <Progress.Section value={failedPercentage} color="red" />}
                          </Progress.Root>
                        </>
                      )}
                      <Text size="xs" c="dimmed">
                        File entries: {machine.processed_file_count} processed · {machine.pending_file_count} pending ·{" "}
                        {machine.failed_file_count} failed
                      </Text>
                    </Stack>
                  </Paper>
                );
              })}
            </SimpleGrid>
          </ScrollArea>
        )}
      </Stack>
    </Paper>
  );
}

function snapshotStateLabel(state: SnapshotImportState | null): string {
  switch (state) {
    case "inventory_pending":
      return "Inventory pending";
    case "inventory_failed":
      return "Inventory failed";
    case "codex_metadata_pending":
      return "Codex metadata pending";
    case "codex_metadata_failed":
      return "Codex metadata failed";
    case "processing_pending":
      return "File processing pending";
    case "processing_failed":
      return "File processing failed";
    case "complete":
      return "Complete";
    case null:
      return "No snapshots registered";
  }
}

function snapshotStateColor(state: SnapshotImportState): "red" | "yellow" | "green" {
  if (state === "inventory_failed" || state === "codex_metadata_failed" || state === "processing_failed") {
    return "red";
  }
  if (state === "inventory_pending" || state === "codex_metadata_pending" || state === "processing_pending") {
    return "yellow";
  }
  return "green";
}

function formatDateTime(value: string | null): string {
  if (value === null) return "Unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function returnPath(state: unknown): string {
  if (typeof state !== "object" || state === null || !("returnTo" in state)) return "/";
  const returnTo = state.returnTo;
  return typeof returnTo === "string" && returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/";
}
