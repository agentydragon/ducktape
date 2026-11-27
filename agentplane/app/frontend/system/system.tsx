import { Badge, Stack, Table, Text, Title } from "@mantine/core";
import { timestampDate } from "@bufbuild/protobuf/wkt";
import { type JSX, useEffect, useState } from "react";

import { displayableError } from "../transport/client";
import { getJournalStats, getSystemStatus, type CheckedComponentStatus } from "./system_status_client";
import { JournalStore, SystemComponentState, type GetJournalStatsResponse } from "../../app_service_pb";
import { TimeAgo } from "../ui/time_ago";
import { TopbarTitle } from "../shell/topbar";
import "./system.css";

const REFRESH_SECONDS = 15;

/** Whether each service the app depends on answers a health check, as the app itself sees it. */
export function SystemStatus(): JSX.Element {
  const journal = useJournalStats();
  const [components, setComponents] = useState<CheckedComponentStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const next = await getSystemStatus();
        if (cancelled) return;
        setComponents(next);
        setError(null);
      } catch (reason) {
        if (!cancelled) setError(displayableError(reason));
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_SECONDS * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <Stack>
      <TopbarTitle>
        <Title order={1} size="h4">
          System
        </Title>
      </TopbarTitle>
      <Text c="dimmed" size="sm">
        Whether each service the app depends on answers a health check. Refreshes every {REFRESH_SECONDS} seconds.
      </Text>
      {error !== null && (
        <Text c="red" size="sm" role="alert">
          Could not read the system status: {error}
        </Text>
      )}
      {components === null ? (
        error === null && (
          <Text c="dimmed" size="sm">
            Checking…
          </Text>
        )
      ) : (
        <Table className="agentplane-system-status-table">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Component</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Detail</Table.Th>
              <Table.Th>Checked</Table.Th>
            </Table.Tr>
          </Table.Thead>
          {components.map((component) => {
            const store =
              component.journalStore === JournalStore.HISTORY
                ? "history"
                : component.journalStore === JournalStore.SAMPLER
                  ? "sampler"
                  : undefined;
            return (
              <Table.Tbody key={component.name}>
                <Table.Tr>
                  <Table.Td className="agentplane-system-status-component">{component.name}</Table.Td>
                  <Table.Td className="agentplane-system-status-state">
                    <span className="agentplane-system-status-mobile-label">Status: </span>
                    {/* Keep the status readable if its table cell gets narrower than the badge label. */}
                    <Badge color={component.state === SystemComponentState.OK ? "green" : "red"} miw="max-content">
                      {component.state === SystemComponentState.OK ? "ok" : "down"}
                    </Badge>
                  </Table.Td>
                  <Table.Td
                    className="agentplane-system-status-detail"
                    data-empty={component.detail === undefined ? "true" : "false"}
                    style={{ overflowWrap: "anywhere" }}
                  >
                    {component.detail !== undefined && (
                      <>
                        <span className="agentplane-system-status-mobile-label">Detail: </span>
                        {component.detail}
                      </>
                    )}
                  </Table.Td>
                  <Table.Td className="agentplane-system-status-checked">
                    <span className="agentplane-system-status-mobile-label">Checked: </span>
                    <TimeAgo value={timestampDate(component.checkedAt)} />
                  </Table.Td>
                </Table.Tr>
                {store !== undefined && (
                  <Table.Tr className="agentplane-system-storage-row">
                    <Table.Td colSpan={4}>
                      <JournalStorage store={store} journal={journal} />
                    </Table.Td>
                  </Table.Tr>
                )}
              </Table.Tbody>
            );
          })}
        </Table>
      )}
    </Stack>
  );
}

// Keep counts and byte totals exact, including beyond Number.MAX_SAFE_INTEGER.
function bytes(n: bigint | undefined): string {
  if (n === undefined) return "Unknown";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
  let scale = 1n;
  let unit = 0;
  while (unit + 1 < units.length && n >= scale * 1024n) {
    scale *= 1024n;
    unit++;
  }
  if (unit === 0) return `${n} B`;
  const tenths = (10n * n + scale / 2n) / scale;
  return `${tenths / 10n}.${tenths % 10n} ${units[unit]}`;
}

type JournalRead = { stats: GetJournalStatsResponse | undefined; error: string | undefined };
function useJournalStats(): JournalRead {
  const [stats, setStats] = useState<GetJournalStatsResponse>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    let cancelled = false;
    let timer: number;
    async function refresh() {
      try {
        const next = await getJournalStats();
        if (!cancelled) {
          setStats(next);
          setError(undefined);
        }
      } catch (reason) {
        if (!cancelled) setError(displayableError(reason));
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void refresh(), 60_000);
      }
    }
    void refresh();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);
  return { stats, error };
}

function JournalStorage({
  store,
  journal: { stats, error },
}: {
  store: "sampler" | "history";
  journal: JournalRead;
}): JSX.Element {
  const m = stats?.[store];
  const at = m?.measuredAt && timestampDate(m.measuredAt);
  return (
    <Stack gap={4}>
      <Text fw={600} size="sm">
        Journal storage
      </Text>
      {error !== undefined && (
        <Text size="sm" c="red" role="alert">
          Could not refresh journal storage: {error}
        </Text>
      )}
      {stats === undefined ? (
        error === undefined && <Text size="sm">Loading journal storage…</Text>
      ) : m === undefined || at === undefined ? (
        <Text size="sm" c="dimmed">
          No measurement available
        </Text>
      ) : (
        <>
          <Text size="sm">
            {m.sessions.toLocaleString()} sessions · {m.legacySessions.toLocaleString()} with legacy gzip · Measured{" "}
            <TimeAgo value={at} />
            {Date.now() - at.getTime() > 10 * 60_000 && " (stale)"}
          </Text>
          <Table horizontalSpacing="xs" aria-label={store + " journal storage"}>
            <Table.Thead>
              <Table.Tr>
                {["Format", "Blocks", "Stored", "Uncompressed"].map((h) => (
                  <Table.Th key={h}>{h}</Table.Th>
                ))}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(
                [
                  ["APB v1", m.apbBlocks, m.apbBytes, m.apbUncompressedBytes],
                  ["Legacy gzip", m.legacyBlocks, m.legacyBytes, m.legacyUncompressedBytes],
                  [
                    "All formats",
                    m.blocks,
                    m.storedBytes,
                    m.classifiedBlocks === m.blocks &&
                    m.apbUncompressedBytes !== undefined &&
                    m.legacyUncompressedBytes !== undefined
                      ? m.apbUncompressedBytes + m.legacyUncompressedBytes
                      : undefined,
                  ],
                ] as const
              ).map(([format, count, stored, raw]) => (
                <Table.Tr key={format}>
                  <Table.Th>{format}</Table.Th>
                  <Table.Td>{count.toLocaleString()}</Table.Td>
                  <Table.Td title={stored.toLocaleString() + " bytes"}>{bytes(stored)}</Table.Td>
                  <Table.Td title={raw === undefined ? undefined : raw.toLocaleString() + " bytes"}>
                    {bytes(raw)}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </>
      )}
    </Stack>
  );
}
