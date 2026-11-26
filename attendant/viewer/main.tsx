import "@mantine/core/styles.css";
import "./viewer.css";

import { Button, Group, MantineProvider, NativeSelect, Paper, Stack, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { Desktop } from "./Desktop";

const messages: Record<string, string> = {
  verifying: "Verifying session…",
  invalid_token: "The owner token was rejected.",
  invalid_origin: "Open Attendant at its configured address and try again.",
  ready: "Session verified.",
  sign_in_needed: "Sign in to this provider in the remote browser, then verify the session.",
  challenge_required: "Complete the provider’s browser challenge, then verify the session.",
  busy: "The browser is busy. Try again in a moment.",
  denied: "This operation was denied.",
  upstream_error: "The provider could not be reached. Try again in a moment.",
};

function errorCode(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

async function api(path: string, options?: RequestInit): Promise<Response> {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const { detail } = await response.json();
    if (detail === "oidc_login_required") window.location.assign("/auth/login");
    throw new Error(detail);
  }
  return response;
}

function post(path: string, body?: object): Promise<Response> {
  return api(
    path,
    body === undefined
      ? { method: "POST" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
  );
}

async function providers(): Promise<{ providers: string[] }> {
  const response = await api("/owner/providers");
  return response.json();
}

function Login({ notice, onLogin }: { notice: string; onLogin: (names: string[]) => void }) {
  const [token, setToken] = useState("");
  const [message, setMessage] = useState(notice);
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setMessage("");
    try {
      await post("/owner/login", { token });
      onLogin((await providers()).providers);
    } catch (error) {
      setMessage(messages[errorCode(error)] ?? "Sign-in is unavailable. Try again in a moment.");
    } finally {
      setToken("");
      setPending(false);
    }
  }

  return (
    <Paper withBorder className="login-panel" p="lg" radius="md">
      <form onSubmit={submit}>
        <Stack>
          <Title order={2} size="h3">
            Owner sign-in
          </Title>
          <TextInput
            label="Owner token"
            type="password"
            autoComplete="current-password"
            required
            autoFocus
            value={token}
            onChange={(event) => setToken(event.currentTarget.value)}
          />
          <Button type="submit" loading={pending}>
            Sign in
          </Button>
          {message && (
            <Text size="sm" role="status">
              {message}
            </Text>
          )}
        </Stack>
      </form>
    </Paper>
  );
}

function App() {
  const [names, setNames] = useState<string[] | null>();
  const [provider, setProvider] = useState("");
  const [notice, setNotice] = useState("");
  const [state, setState] = useState("");
  const [pending, setPending] = useState(false);
  const [attached, setAttached] = useState(false);

  const detach = useCallback(() => setAttached(false), []);

  function signedIn(names: string[]) {
    setNames(names);
    setProvider(names[0] ?? "");
    setState("");
  }

  function signedOut(message: string) {
    setNames(null);
    setNotice(message);
    setAttached(false);
  }

  useEffect(() => {
    let active = true;
    providers().then(
      (result) => {
        if (active) signedIn(result.providers);
      },
      (error: unknown) => {
        if (active)
          signedOut(
            errorCode(error) === "owner_sign_in_required" ? "" : "Attendant is unavailable. Try again in a moment."
          );
      }
    );
    return () => {
      active = false;
    };
  }, []);

  type Action = "open" | "verify" | "logout" | "connect";
  async function act(action: Action) {
    setPending(true);
    try {
      const path = `/owner/providers/${encodeURIComponent(provider)}`;
      switch (action) {
        case "logout":
          if ((await post("/owner/logout")).headers.get("Location") === "/auth/signed-out") {
            window.location.assign("/auth/signed-out");
            break;
          }
          signedOut("");
          break;
        case "verify": {
          setState("verifying");
          const response = await post(`${path}/verify`);
          const result: { state: string } = await response.json();
          setState(result.state);
          break;
        }
        case "open":
          setState("");
          await post(`${path}/open`);
          setAttached(true);
          break;
        case "connect":
          await providers();
          setAttached(true);
          break;
      }
    } catch (error) {
      const code = errorCode(error);
      if (code === "owner_sign_in_required") signedOut(action === "logout" ? "" : "Sign in again to continue.");
      else setState(code || "upstream_error");
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <header className="masthead">
        <Group justify="space-between">
          <Title order={1} size="h3">
            Attendant
          </Title>
          {names && (
            <Button variant="default" disabled={pending} onClick={() => act("logout")}>
              Sign out
            </Button>
          )}
        </Group>
      </header>
      <main>
        {names === null && <Login notice={notice} onLogin={signedIn} />}
        {names && (
          <Stack component="section" aria-label="Operator workspace">
            <Group align="end">
              <NativeSelect
                label="Provider"
                data={names}
                value={provider}
                disabled={pending || !names.length}
                onChange={(event) => {
                  setProvider(event.currentTarget.value);
                  setState("");
                }}
              />
              <Button disabled={pending || !provider} onClick={() => act("open")}>
                Open sign-in
              </Button>
              <Button variant="default" disabled={pending || !provider} onClick={() => act("verify")}>
                Verify session
              </Button>
            </Group>
            <Text size="sm" role="status" className="provider-status">
              {names.length === 0
                ? "No providers configured."
                : (messages[state] ?? (state && "Session state could not be determined."))}
            </Text>
            <Desktop attached={attached} pending={pending} onDetach={detach} onConnect={() => act("connect")} />
          </Stack>
        )}
      </main>
    </>
  );
}

const root = document.getElementById("root");
if (root)
  createRoot(root).render(
    <MantineProvider defaultColorScheme="auto" withCssVariables={false} withGlobalClasses={false}>
      <App />
    </MantineProvider>
  );
