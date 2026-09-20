import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  Divider,
  Group,
  Loader,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { getApiClient } from "./api";
import type { OAuthProviderStatus } from "./types";

type ScopeRow = {
  scope: string;
  requested: boolean;
  granted: boolean;
};

type ScopeComparison = {
  rows: ScopeRow[];
  missing: string[];
  extra: string[];
  drift: boolean;
};

function compareScopes(requested: string[], granted: string): ScopeComparison {
  const requestedScopes = [...new Set(requested)];
  const requestedSet = new Set(requestedScopes);
  const grantedSet = new Set(granted ? granted.split(/\s+/).filter(Boolean) : []);
  const missing = requestedScopes.filter((scope) => !grantedSet.has(scope)).sort();
  const extra = [...grantedSet].filter((scope) => !requestedSet.has(scope)).sort();

  return {
    rows: [...requestedScopes, ...extra].map((scope) => ({
      scope,
      requested: requestedSet.has(scope),
      granted: grantedSet.has(scope),
    })),
    missing,
    extra,
    drift: missing.length + extra.length > 0,
  };
}

function ProviderStatusBadge({ provider }: { provider: OAuthProviderStatus }): JSX.Element {
  switch (provider.status.state) {
    case "connected":
      return <Badge color="green">Connected</Badge>;
    case "expired":
      return <Badge color="red">Token expired — refresh failing</Badge>;
    case "disconnected":
      return <Badge color="yellow">Not connected</Badge>;
    default:
      return <Badge color="gray">Unknown status</Badge>;
  }
}

function formatExpiry(iso: string): string {
  return new Date(iso).toLocaleString();
}

function ProviderCard({ provider }: { provider: OAuthProviderStatus }): JSX.Element {
  const tokenStatus =
    provider.status.state === "connected" || provider.status.state === "expired" ? provider.status : null;
  const grantedScope = tokenStatus?.scope ?? "";
  const scopes = compareScopes(provider.requested_scopes, grantedScope);

  return (
    <Paper withBorder radius="md" p="lg">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <Stack gap="xs" style={{ minWidth: 0 }}>
            <Group gap="sm">
              <Title order={3} size="h4">
                {provider.display_name}
              </Title>
              <Badge variant="outline" color="gray">
                {provider.provider_type}
              </Badge>
            </Group>
            <Group gap="xs">
              <Text size="sm" c="dimmed">
                Status
              </Text>
              <ProviderStatusBadge provider={provider} />
            </Group>
          </Stack>
          <Button component="a" href={"/oauth/authorize/" + provider.name} style={{ flexShrink: 0 }}>
            {provider.status.state === "connected" ? "Reconnect" : "Connect"}
          </Button>
        </Group>

        <Divider />

        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Stack gap={4}>
            <Text size="xs" fw={600} c="dimmed">
              Requested scopes
            </Text>
            <Code style={{ overflowWrap: "anywhere" }}>
              {provider.requested_scopes.length > 0 ? provider.requested_scopes.join(" ") : "None"}
            </Code>
          </Stack>
          {tokenStatus !== null && (
            <Stack gap={4}>
              <Text size="xs" fw={600} c="dimmed">
                Access token
              </Text>
              <Text size="sm">
                {tokenStatus.state === "expired" ? "Expired" : "Expires"} {formatExpiry(tokenStatus.expires_at)}
              </Text>
            </Stack>
          )}
        </SimpleGrid>

        <Stack gap="xs">
          <Group justify="space-between" align="center">
            <Text size="sm" fw={600}>
              Granted scopes
            </Text>
            {tokenStatus !== null && scopes.drift && (
              <Badge color="orange" variant="light">
                Scope drift
              </Badge>
            )}
          </Group>
          <Table.ScrollContainer>
            <Table withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Scope</Table.Th>
                  <Table.Th ta="center">Requested</Table.Th>
                  <Table.Th ta="center">Granted</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {scopes.rows.length === 0 ? (
                  <Table.Tr>
                    <Table.Td colSpan={3}>
                      <Text size="sm" style={{ fontStyle: "italic" }} c="dimmed">
                        No scopes
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ) : (
                  scopes.rows.map((row) => (
                    <Table.Tr key={row.scope}>
                      <Table.Td>
                        <Text size="sm" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                          {row.scope}
                        </Text>
                      </Table.Td>
                      <Table.Td ta="center">
                        <Text fw={700} c={row.requested ? "green" : "dimmed"} aria-label={row.requested ? "yes" : "no"}>
                          {row.requested ? "✓" : "✕"}
                        </Text>
                      </Table.Td>
                      <Table.Td ta="center">
                        <Text fw={700} c={row.granted ? "green" : "dimmed"} aria-label={row.granted ? "yes" : "no"}>
                          {row.granted ? "✓" : "✕"}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))
                )}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {tokenStatus !== null && scopes.drift && (
            <Alert color="orange" variant="light" title="Granted scopes differ from the request">
              <Stack gap={4}>
                {scopes.missing.length > 0 && (
                  <Text size="sm">
                    Missing: <Code>{scopes.missing.join(" ")}</Code>
                  </Text>
                )}
                {scopes.extra.length > 0 && (
                  <Text size="sm">
                    Extra: <Code>{scopes.extra.join(" ")}</Code>
                  </Text>
                )}
                <Text size="sm">Reconnect this provider to update its authorization.</Text>
              </Stack>
            </Alert>
          )}
          {tokenStatus?.state === "expired" && tokenStatus.last_refresh_error && (
            <Alert color="red" variant="light" title="Refresh error">
              <Box component="code" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
                {tokenStatus.last_refresh_error}
              </Box>
            </Alert>
          )}
        </Stack>
      </Stack>
    </Paper>
  );
}

export function OAuthProviders(): JSX.Element {
  const [providers, setProviders] = useState<OAuthProviderStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getApiClient()
      .listOAuthProviders()
      .then((result) => {
        if (active) setProviders(result);
      })
      .catch((failure: unknown) => {
        if (active) setError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>OAuth Providers</Title>
        <Text size="sm" c="dimmed" mt={4}>
          Connect accounts and review their granted permissions.
        </Text>
      </div>

      {loading && (
        <Group justify="center" py="xl">
          <Loader size="sm" />
          <Text c="dimmed">Loading providers…</Text>
        </Group>
      )}
      {!loading && error && (
        <Alert color="red" title="Failed to load providers" role="alert">
          {error}
        </Alert>
      )}
      {!loading && !error && providers.length === 0 && (
        <Paper withBorder radius="md" p="lg">
          <Text c="dimmed">No OAuth providers configured.</Text>
        </Paper>
      )}
      {!loading && !error && providers.map((provider) => <ProviderCard key={provider.name} provider={provider} />)}
    </Stack>
  );
}
