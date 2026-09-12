import { Modal, Tabs } from "@mantine/core";

import { Connections } from "./connections";
import { McpServers } from "./mcp_servers";
import { PushSettings } from "./push";

export type SettingsTab = "oauth-clients" | "mcp-servers" | "notifications";

const TABS: SettingsTab[] = ["oauth-clients", "mcp-servers", "notifications"];

export function isSettingsTab(value: string | null): value is SettingsTab {
  return TABS.includes(value as SettingsTab);
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
      </Tabs>
    </Modal>
  );
}
