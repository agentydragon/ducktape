import { useCallback, useEffect, useMemo, useState, FormEvent } from "react";
import {
  ActionIcon,
  AppShell,
  Autocomplete,
  Badge,
  Box,
  Button,
  Card,
  Container,
  Group,
  Loader,
  Pagination,
  ScrollArea,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import { IconMoon, IconSun } from "@tabler/icons-react";
import JsonView from "@uiw/react-json-view";

import type { components } from "./generated/admin-api-types";

type ResponseRecord = components["schemas"]["ResponseRecordModel"];
type ResponseList = components["schemas"]["ResponseListModel"];
type FrameRecord = components["schemas"]["FrameRecordModel"];
type FrameList = components["schemas"]["FrameListModel"];
type ApiKeyRecord = components["schemas"]["APIKeyModel"];
type ApiKeyList = components["schemas"]["APIKeyListModel"];
type CreateKeyResponse = components["schemas"]["CreateKeyResponse"];
type UpstreamKeyList = components["schemas"]["UpstreamKeyListModel"];

type ResponseStatusEvent = {
  type: "response_status";
  key: string;
  response_id?: string | null;
  status: string;
  error?: string | null;
};

type FrameEvent = {
  type: "frame";
  key: string;
  response_id?: string | null;
  ordinal: number;
  frame_type?: string | null;
  event_id?: string | null;
};

type ApiKeyCreatedEvent = {
  type: "api_key_created";
  id: string;
  name: string;
  upstream_alias: string;
};

type ApiKeyRevokedEvent = {
  type: "api_key_revoked";
  id: string;
};

type LiveEvent = ResponseStatusEvent | FrameEvent | ApiKeyCreatedEvent | ApiKeyRevokedEvent;

const RESPONSE_LIMIT = 50;

const statusBadgeColor = (status: string): string => {
  switch (status) {
    case "complete":
      return "green";
    case "error":
      return "red";
    case "in_progress":
      return "blue";
    case "queued":
      return "yellow";
    default:
      return "gray";
  }
};

const formatDate = (value?: string | null) => {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
};

const JsonBlock = ({ label, value }: { label: string; value: unknown }) => (
  <Stack gap="xs">
    <Text fw={600}>{label}</Text>
    <Card withBorder padding="sm" radius="md">
      <JsonView value={value ?? {}} displayDataTypes={false} enableClipboard collapsed={2} />
    </Card>
  </Stack>
);

const ColorSchemeToggle = () => {
  const { setColorScheme } = useMantineColorScheme();
  const computed = useComputedColorScheme("light");
  const nextScheme = computed === "dark" ? "light" : "dark";

  return (
    <ActionIcon
      variant="subtle"
      color={computed === "dark" ? "yellow" : "blue"}
      onClick={() => setColorScheme(nextScheme)}
      aria-label="Toggle color scheme"
    >
      {computed === "dark" ? <IconSun size={18} /> : <IconMoon size={18} />}
    </ActionIcon>
  );
};

const App = () => {
  const [responses, setResponses] = useState<ResponseRecord[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIdentifier, setSelectedIdentifier] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ResponseRecord | null>(null);
  const [frames, setFrames] = useState<FrameRecord[]>([]);
  const [tab, setTab] = useState<"responses" | "keys">("responses");
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [newKeyName, setNewKeyName] = useState("");
  const [newKeyAlias, setNewKeyAlias] = useState("default");
  const [availableAliases, setAvailableAliases] = useState<string[]>(["default"]);
  const [creatingKey, setCreatingKey] = useState(false);
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [liveConnected, setLiveConnected] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);

  const totalPages = Math.max(1, Math.ceil(total / RESPONSE_LIMIT));

  const fetchResponses = useCallback(
    async (pageArg = page) => {
      try {
        setLoading(true);
        const offset = (pageArg - 1) * RESPONSE_LIMIT;
        const res = await fetch(`/api/responses?limit=${RESPONSE_LIMIT}&offset=${offset}`);
        if (!res.ok) {
          throw new Error("Failed to fetch responses");
        }
        const data: ResponseList = await res.json();
        setResponses(data.items ?? []);
        setTotal(data.total ?? data.items?.length ?? 0);
        setError(null);
        setPage(pageArg);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setLoading(false);
      }
    },
    [page]
  );

  const fetchDetail = useCallback(async (identifier: string) => {
    try {
      const res = await fetch(`/api/responses/${identifier}`);
      if (!res.ok) {
        return;
      }
      const detail: ResponseRecord = await res.json();
      setSelectedDetail(detail);
    } catch (err) {
      console.warn("Failed to fetch response detail", err);
    }
  }, []);

  const fetchFrames = useCallback(async (identifier: string) => {
    try {
      const res = await fetch(`/api/responses/${identifier}/frames?limit=200`);
      if (!res.ok) {
        throw new Error("Failed to fetch frames");
      }
      const data: FrameList = await res.json();
      setFrames(data.items ?? []);
    } catch (err) {
      console.warn("Failed to fetch frames", err);
    }
  }, []);

  const handleSelectResponse = useCallback(
    async (record: ResponseRecord) => {
      const identifier = record.response_id || record.cache_key;
      setSelectedIdentifier(identifier);
      await Promise.all([fetchDetail(identifier), fetchFrames(identifier)]);
    },
    [fetchDetail, fetchFrames]
  );

  const refreshKeys = useCallback(async () => {
    try {
      const res = await fetch("/api/keys");
      if (!res.ok) {
        throw new Error("Failed to fetch API keys");
      }
      const data: ApiKeyList = await res.json();
      setKeys(data.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    }
  }, []);

  const fetchUpstreamAliases = useCallback(async () => {
    try {
      const res = await fetch("/api/upstream-keys");
      if (!res.ok) {
        throw new Error("Failed to fetch upstream aliases");
      }
      const data: UpstreamKeyList = await res.json();
      const items = data.items ?? [];
      setAvailableAliases(items.length ? items : ["default"]);
    } catch (err) {
      console.warn("Failed to fetch upstream aliases", err);
      setAvailableAliases(["default"]);
    }
  }, []);

  useEffect(() => {
    fetchResponses(1);
    fetchUpstreamAliases();
  }, [fetchResponses, fetchUpstreamAliases]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!liveConnected) {
        fetchResponses(page);
      }
    }, 15000);
    return () => window.clearInterval(interval);
  }, [fetchResponses, page, liveConnected]);

  useEffect(() => {
    if (tab === "keys") {
      refreshKeys();
    }
  }, [tab, refreshKeys]);

  useEffect(() => {
    const source = new EventSource("/api/responses/live");
    source.onmessage = (event) => {
      try {
        const payload: LiveEvent = JSON.parse(event.data);
        setLiveConnected(true);
        switch (payload.type) {
          case "response_status": {
            fetchResponses(page);
            const identifier = payload.response_id ?? payload.key;
            if (identifier && selectedIdentifier === identifier) {
              fetchDetail(identifier);
              fetchFrames(identifier);
            }
            break;
          }
          case "frame": {
            const identifier = payload.response_id ?? payload.key;
            if (identifier && selectedIdentifier === identifier) {
              fetchFrames(identifier);
              fetchDetail(identifier);
            }
            break;
          }
          case "api_key_created":
          case "api_key_revoked": {
            if (tab === "keys") {
              refreshKeys();
            }
            break;
          }
          default:
            break;
        }
      } catch (err) {
        console.warn("Failed to process SSE event", err);
      }
    };
    source.onerror = () => {
      setLiveConnected(false);
      source.close();
    };
    return () => {
      source.close();
    };
  }, [fetchResponses, fetchDetail, fetchFrames, page, selectedIdentifier, tab, refreshKeys]);

  const handleCreateKey = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setCreatingKey(true);
      setMintedToken(null);
      try {
        const res = await fetch("/api/keys", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newKeyName, alias: newKeyAlias }),
        });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({}));
          throw new Error(payload.detail || "Failed to create API key");
        }
        const data: CreateKeyResponse = await res.json();
        setMintedToken(data.token);
        setNewKeyName("");
        fetchUpstreamAliases();
        refreshKeys();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setCreatingKey(false);
      }
    },
    [newKeyName, newKeyAlias, refreshKeys, fetchUpstreamAliases]
  );

  const responseTableRows = useMemo(
    () =>
      responses.map((record) => {
        const identifier = record.response_id || record.cache_key;
        const isActive = selectedIdentifier === identifier;
        return (
          <Table.Tr
            key={record.cache_key}
            onClick={() => handleSelectResponse(record)}
            style={{ cursor: "pointer" }}
            bg={isActive ? "blue.0" : undefined}
          >
            <Table.Td>{formatDate(record.created_at)}</Table.Td>
            <Table.Td>
              <Badge color={statusBadgeColor(record.status)}>{record.status}</Badge>
            </Table.Td>
            <Table.Td>{record.model}</Table.Td>
            <Table.Td>{record.latency_ms != null ? `${record.latency_ms} ms` : "—"}</Table.Td>
            <Table.Td>{record.api_key?.name || "—"}</Table.Td>
          </Table.Tr>
        );
      }),
    [responses, handleSelectResponse, selectedIdentifier]
  );

  const responsesTab = (
    <Stack gap="md">
      <Card withBorder radius="md" padding="md">
        <Group justify="space-between" align="center" mb="md">
          <Title order={3}>Requests</Title>
          <Group gap="sm">
            {!liveConnected && <Badge color="red">Live feed offline</Badge>}
            <Button variant="default" onClick={() => fetchResponses(page)} disabled={loading}>
              Refresh
            </Button>
          </Group>
        </Group>
        {loading ? (
          <Group justify="center" py="lg">
            <Loader />
          </Group>
        ) : error && tab === "responses" ? (
          <Text c="red">{error}</Text>
        ) : (
          <>
            <ScrollArea h="55vh">
              <Table striped highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Started</Table.Th>
                    <Table.Th>Status</Table.Th>
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Latency</Table.Th>
                    <Table.Th>API Key</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {responseTableRows.length ? (
                    responseTableRows
                  ) : (
                    <Table.Tr>
                      <Table.Td colSpan={5}>
                        <Text ta="center" c="dimmed">
                          No responses captured yet.
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </ScrollArea>
            <Group justify="center" mt="md">
              <Pagination value={page} onChange={(p) => fetchResponses(p)} total={totalPages} />
            </Group>
          </>
        )}
      </Card>

      <Card withBorder radius="md" padding="md" mih="30vh">
        <Title order={3} mb="md">
          Details
        </Title>
        {selectedDetail ? (
          <Stack gap="md">
            <Text>
              <Text fw={600} span>
                Key:
              </Text>{" "}
              {selectedDetail.cache_key}
            </Text>
            {selectedDetail.response_id && (
              <Text>
                <Text fw={600} span>
                  Response ID:
                </Text>{" "}
                {selectedDetail.response_id}
              </Text>
            )}
            <Text>
              <Text fw={600} span>
                Status:
              </Text>{" "}
              <Badge color={statusBadgeColor(selectedDetail.status)}>{selectedDetail.status}</Badge>
            </Text>
            {selectedDetail.error && (
              <Text>
                <Text fw={600} span>
                  Status reason:
                </Text>{" "}
                {selectedDetail.error}
              </Text>
            )}
            <Text>
              <Text fw={600} span>
                Model:
              </Text>{" "}
              {selectedDetail.model}
            </Text>
            <Text>
              <Text fw={600} span>
                API Key:
              </Text>{" "}
              {selectedDetail.api_key?.name || "—"}
            </Text>
            <Text>
              <Text fw={600} span>
                Latency:
              </Text>{" "}
              {selectedDetail.latency_ms != null ? `${selectedDetail.latency_ms} ms` : "—"}
            </Text>
            <Text>
              <Text fw={600} span>
                Last updated:
              </Text>{" "}
              {formatDate(selectedDetail.updated_at)}
            </Text>
            <JsonBlock label="Request" value={selectedDetail.request_body ?? {}} />
            {selectedDetail.final_response && <JsonBlock label="Response" value={selectedDetail.final_response} />}
            {selectedDetail.response_error && <JsonBlock label="Error" value={selectedDetail.response_error} />}
            {frames.length > 0 && (
              <Stack gap="sm">
                <Text fw={600}>Streaming frames ({frames.length})</Text>
                <ScrollArea h={220} offsetScrollbars>
                  <Stack gap="sm">
                    {frames.map((frame) => (
                      <Card key={frame.ordinal} withBorder radius="md" padding="sm">
                        <Text fw={600} size="sm" mb="xs">
                          #{frame.ordinal} · {frame.frame_type ?? "frame"} · {formatDate(frame.created_at)}
                        </Text>
                        <JsonView value={frame.frame ?? {}} displayDataTypes={false} enableClipboard collapsed={2} />
                      </Card>
                    ))}
                  </Stack>
                </ScrollArea>
              </Stack>
            )}
          </Stack>
        ) : (
          <Text c="dimmed">Select a response to view details.</Text>
        )}
      </Card>
    </Stack>
  );

  const keysTab = (
    <Stack gap="md">
      <Card withBorder radius="md" padding="md">
        <Title order={3} mb="md">
          Create API Key
        </Title>
        <form onSubmit={handleCreateKey}>
          <Stack gap="sm">
            <TextInput
              required
              label="Name"
              value={newKeyName}
              onChange={(event) => setNewKeyName(event.currentTarget.value)}
            />
            <Autocomplete
              label="Alias"
              value={newKeyAlias}
              data={availableAliases}
              variant="filled"
              onChange={setNewKeyAlias}
              placeholder="Select or enter an alias"
              withinPortal={false}
            />
            <Group justify="flex-end" mt="sm">
              <Button type="submit" loading={creatingKey} disabled={!newKeyName}>
                Create key
              </Button>
            </Group>
          </Stack>
        </form>
        {mintedToken && (
          <Box mt="md">
            <Text fw={600}>New token:</Text>
            <Card withBorder padding="sm" radius="md" mt="xs">
              <Text ff="monospace" size="sm">
                {mintedToken}
              </Text>
            </Card>
            <Text size="sm" c="red" mt="xs">
              Copy this token now; it will not be shown again.
            </Text>
          </Box>
        )}
      </Card>

      <Card withBorder radius="md" padding="md">
        <Group justify="space-between" align="center" mb="md">
          <Title order={3}>Existing Keys</Title>
          <Button variant="default" onClick={refreshKeys}>
            Refresh
          </Button>
        </Group>
        <ScrollArea h="50vh">
          <Table striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>Alias</Table.Th>
                <Table.Th>Prefix</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Created</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {keys.length ? (
                keys.map((key) => (
                  <Table.Tr key={key.id}>
                    <Table.Td>{key.name}</Table.Td>
                    <Table.Td>{key.upstream_alias}</Table.Td>
                    <Table.Td>{key.token_prefix}</Table.Td>
                    <Table.Td>{key.revoked_ts ? "revoked" : "active"}</Table.Td>
                    <Table.Td>{formatDate(key.created_at)}</Table.Td>
                  </Table.Tr>
                ))
              ) : (
                <Table.Tr>
                  <Table.Td colSpan={5}>
                    <Text ta="center" c="dimmed">
                      No keys created yet.
                    </Text>
                  </Table.Td>
                </Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </Card>
    </Stack>
  );

  return (
    <AppShell withBorder={false} padding="md">
      <AppShell.Header>
        <Container size="xl">
          <Group justify="space-between" align="center" h="100%">
            <Title order={2}>rspcache Admin</Title>
            <Group gap="sm" align="center">
              <Tabs value={tab} onChange={(value) => setTab(value as "responses" | "keys")}>
                <Tabs.List>
                  <Tabs.Tab value="responses">Responses</Tabs.Tab>
                  <Tabs.Tab value="keys">API Keys</Tabs.Tab>
                </Tabs.List>
              </Tabs>
              <ColorSchemeToggle />
            </Group>
          </Group>
        </Container>
      </AppShell.Header>
      <AppShell.Main>
        <Container size="xl" py="md">
          {tab === "responses" ? responsesTab : keysTab}
        </Container>
      </AppShell.Main>
    </AppShell>
  );
};

export default App;
