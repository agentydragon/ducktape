import { useState, type JSX } from "react";
import { Alert, Anchor, Button, Code, Group, List, Paper, Stack, Text, Textarea, Title } from "@mantine/core";

import { finishPairing, startPairing, type SyncStatus } from "./api";
import { sentence } from "./status";

type Props = {
  paired: boolean;
  /** Called with the fresh server status once pairing completes. */
  onPaired: (status: SyncStatus) => void;
};

/**
 * Claude redirects to a loopback address nothing listens on; the browser fails to load it, and the URL
 * in the address bar is pasted back here to complete the grant.
 */
export function Pairing({ paired, onPaired }: Props): JSX.Element {
  const [authorizationUrl, setAuthorizationUrl] = useState<string | null>(null);
  const [redirectUrl, setRedirectUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof Error ? sentence(reason.message) : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  const start = (): Promise<void> =>
    run(async () => {
      setAuthorizationUrl((await startPairing()).authorization_url);
      setRedirectUrl("");
    });

  const finish = (): Promise<void> =>
    run(async () => {
      const status = await finishPairing(redirectUrl);
      setAuthorizationUrl(null);
      setRedirectUrl("");
      onPaired(status);
    });

  return (
    <Paper component="section" aria-labelledby="pairing-heading" withBorder radius="md" p="md">
      <Stack gap="md">
        <Title order={2} id="pairing-heading">
          {paired ? "Pair again" : "Pair with Claude"}
        </Title>
        {authorizationUrl === null ? (
          <>
            <Text>
              {paired
                ? "Replaces the grant this page holds. The sync switches to the new one straight away."
                : "The sync has no credential yet. Pairing gives it its own grant, separate from any Claude Code login."}
            </Text>
            <Group>
              <Button type="button" onClick={() => void start()} loading={busy}>
                {paired ? "Pair again" : "Start pairing"}
              </Button>
            </Group>
          </>
        ) : (
          <List type="ordered" withPadding spacing="sm">
            <List.Item>
              <Text size="sm">
                <Anchor href={authorizationUrl} target="_blank" rel="noreferrer">
                  Open Claude’s authorize page
                </Anchor>{" "}
                in a browser signed in to the account, and approve.
              </Text>
            </List.Item>
            <List.Item>
              <Text size="sm">
                The browser is then sent to an address starting <Code>http://localhost:54545/callback</Code>. Nothing
                listens there, so the page fails to load; that is expected. Copy the whole address from the address bar.
              </Text>
            </List.Item>
            <List.Item>
              <Stack gap="xs">
                <Textarea
                  id="redirect-url"
                  label="Paste it here"
                  rows={3}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder="http://localhost:54545/callback?code=…&state=…"
                  value={redirectUrl}
                  onChange={(event) => setRedirectUrl(event.currentTarget.value)}
                />
                <Group>
                  <Button
                    type="button"
                    onClick={() => void finish()}
                    disabled={busy || redirectUrl.trim() === ""}
                    loading={busy}
                  >
                    Finish pairing
                  </Button>
                  <Button type="button" variant="default" onClick={() => void start()} disabled={busy}>
                    Start over
                  </Button>
                </Group>
              </Stack>
            </List.Item>
          </List>
        )}
        {error !== null && (
          <Alert color="red" role="alert" title="Pairing failed">
            {error}
          </Alert>
        )}
      </Stack>
    </Paper>
  );
}
