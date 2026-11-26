import { useEffect, useState, type JSX } from "react";
import { Button, Group, Modal, Stack, Tabs, Text, Title } from "@mantine/core";

import { Connections } from "../connections/management";
import { McpServers } from "../actions/mcp/mcp_servers";
import { PushSettings } from "../actions/push/settings";

import { readThreadCacheUsage, type ThreadCacheUsage } from "../sessions/thread_cache";

export type SettingsTab = "oauth-clients" | "mcp-servers" | "notifications" | "storage";

const TABS: SettingsTab[] = ["oauth-clients", "mcp-servers", "notifications", "storage"];

export function isSettingsTab(value: string | null): value is SettingsTab {
  return TABS.includes(value as SettingsTab);
}

function bytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[index]}`;
}

function StorageSettings(): JSX.Element {
  const [revision, refresh] = useState(0);
  const [snapshot, setSnapshot] = useState<{
    cache: ThreadCacheUsage | null | "error";
    site?: StorageEstimate;
  } | null>(null);
  useEffect(() => {
    let active = true;
    void Promise.all([
      readThreadCacheUsage().catch(() => "error" as const),
      navigator.storage?.estimate?.().catch(() => undefined),
    ]).then(([cache, site]) => {
      if (active) setSnapshot({ cache, site });
    });
    return () => {
      active = false;
    };
  }, [revision]);
  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Title order={3}>Thread history cache</Title>
        <Button
          size="xs"
          variant="default"
          disabled={!snapshot}
          onClick={() => {
            setSnapshot(null);
            refresh((n) => n + 1);
          }}
        >
          Refresh
        </Button>
      </Group>
      {!snapshot ? (
        <Text role="status">Reading storage usage…</Text>
      ) : (
        <>
          {snapshot.cache === "error" ? (
            <Text role="alert">Could not read cache usage.</Text>
          ) : snapshot.cache === null ? (
            <Text>This browser keeps thread history in memory only.</Text>
          ) : (
            <div>
              <Text fw={600}>{bytes(snapshot.cache.bytes)} of saved history</Text>
              <Text size="sm">
                {snapshot.cache.windows.toLocaleString()} thread {snapshot.cache.windows === 1 ? "window" : "windows"} ·{" "}
                {snapshot.cache.bodies.toLocaleString()} message {snapshot.cache.bodies === 1 ? "body" : "bodies"}
              </Text>
              <Text c="dimmed" size="sm">
                Content size before database overhead. Signing out clears saved history from this browser.
              </Text>
            </div>
          )}
          <div>
            <Text fw={600}>Site storage</Text>
            <Text size="sm">
              {snapshot.site?.usage !== undefined && snapshot.site.quota !== undefined
                ? `${bytes(snapshot.site.usage)} used of ${bytes(snapshot.site.quota)} estimated quota for this site.`
                : "The browser did not provide a storage estimate."}
            </Text>
          </div>
        </>
      )}
    </Stack>
  );
}

export function Settings({
  opened,
  tab,
  onTabChange,
  onClose,
}: {
  opened: boolean;
  tab: SettingsTab;
  onTabChange: (tab: SettingsTab) => void;
  onClose: () => void;
}): JSX.Element {
  return (
    <Modal opened={opened} onClose={onClose} title="Settings" size="xl">
      <Tabs value={tab} onChange={(value) => isSettingsTab(value) && onTabChange(value)}>
        <Tabs.List>
          <Tabs.Tab value="oauth-clients">OAuth clients</Tabs.Tab>
          <Tabs.Tab value="mcp-servers">MCP servers</Tabs.Tab>
          <Tabs.Tab value="notifications">Notifications</Tabs.Tab>
          <Tabs.Tab value="storage">Storage</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="oauth-clients" pt="sm">
          <Connections />
        </Tabs.Panel>
        <Tabs.Panel value="mcp-servers" pt="sm">
          <McpServers />
        </Tabs.Panel>
        <Tabs.Panel value="notifications" pt="sm">
          <PushSettings />
        </Tabs.Panel>
        <Tabs.Panel value="storage" pt="sm">
          {opened && tab === "storage" && <StorageSettings />}
        </Tabs.Panel>
      </Tabs>
    </Modal>
  );
}
