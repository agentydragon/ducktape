import { Badge, Stack, Table, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { api, displayableError, ok, type ComponentStatus } from "./client";
import { TimeAgo } from "./time_ago";
import { TopbarTitle } from "./topbar";

const REFRESH_SECONDS = 15;

/** Whether each service the app depends on answers a health check, as the app itself sees it. */
export function SystemStatus(): JSX.Element {
  const [components, setComponents] = useState<ComponentStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const next = ok(await api.GET("/system/status"));
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
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Component</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Detail</Table.Th>
              <Table.Th>Checked</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {components.map((component) => (
              <Table.Tr key={component.name}>
                <Table.Td>{component.name}</Table.Td>
                <Table.Td>
                  {/* The badge truncates its label to the cell; a phone squeezes the cell below the word. */}
                  <Badge color={component.state === "ok" ? "green" : "red"} miw="max-content">
                    {component.state}
                  </Badge>
                </Table.Td>
                <Table.Td style={{ overflowWrap: "anywhere" }}>{component.detail}</Table.Td>
                <Table.Td>
                  <TimeAgo value={component.checked_at} />
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}
