import {
  Anchor,
  AppShell,
  Button,
  Center,
  Divider,
  Group,
  MantineProvider,
  Paper,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { useEffect, useMemo, useState, type FormEvent, type JSX } from "react";

import RunTriggerModal from "$components/RunTriggerModal";
import { HashRouter, Link, Route, Routes, useLocation, useParams } from "react-router";
import { RunModalContext } from "$lib/runModalContext";
import {
  clearToken,
  captureTokenFromUrl,
  getToken,
  markSessionAuthenticated,
  setToken,
  useNeedsToken,
} from "$lib/stores/token";
import type { RunModalPrefill } from "$lib/types";
import DefinitionDetailPage from "./pages/DefinitionDetailPage";
import ExamplesPage from "./pages/ExamplesPage";
import OverviewPage from "./pages/OverviewPage";
import RunDetailPage from "./pages/RunDetailPage";
import RunsPage from "./pages/RunsPage";
import SnapshotDetailPage from "./pages/SnapshotDetailPage";
import SnapshotsPage from "./pages/SnapshotsPage";

const navItems = [
  { path: "/", label: "Overview" },
  { path: "/runs", label: "Runs" },
  { path: "/snapshots", label: "Ground Truth" },
];

function isActive(path: string, currentPath: string): boolean {
  return path === "/" ? currentPath === "/" : currentPath.startsWith(path);
}

function RunRoute(): JSX.Element {
  const { runId } = useParams();
  if (runId === undefined) throw new Error("missing runId route parameter");
  return <RunDetailPage runId={runId} />;
}

function DefinitionRoute(): JSX.Element {
  const { definitionId } = useParams();
  if (definitionId === undefined) throw new Error("missing definitionId route parameter");
  return <DefinitionDetailPage definitionId={definitionId} />;
}

function SnapshotRoute(): JSX.Element {
  const { "*": slug } = useParams();
  if (slug === undefined) throw new Error("missing snapshot slug route parameter");
  return <SnapshotDetailPage slug={slug} />;
}

function NotFound(): JSX.Element {
  return (
    <Center py={80}>
      <Stack align="center" gap="xs">
        <Title order={2}>Page Not Found</Title>
        <Text c="dimmed">The page you&apos;re looking for doesn&apos;t exist.</Text>
        <Button component={Link} to="/" variant="subtle">
          Go home
        </Button>
      </Stack>
    </Center>
  );
}

function PropsApp(): JSX.Element {
  const { pathname } = useLocation();
  const needsToken = useNeedsToken();
  const [showRunModal, setShowRunModal] = useState(false);
  const [modalPrefill, setModalPrefill] = useState<RunModalPrefill | undefined>();
  const [tokenInput, setTokenInput] = useState("");
  const [usernameInput, setUsernameInput] = useState("");
  const [passwordInput, setPasswordInput] = useState("");
  const [checking, setChecking] = useState(true);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [showTokenLogin, setShowTokenLogin] = useState(false);
  const [ssoAvailable, setSsoAvailable] = useState(true);

  const tokenHasInput = tokenInput.trim().length > 0;
  const credsHaveInput = usernameInput.trim().length > 0 || passwordInput.length > 0;

  useEffect(() => {
    let alive = true;
    async function initAuth(): Promise<void> {
      captureTokenFromUrl();
      if (getToken()) return;

      try {
        const response = await fetch("/auth/me", { credentials: "include" });
        if (!alive) return;
        if (response.ok) {
          const data = (await response.json()) as { email: string };
          setUserEmail(data.email);
          markSessionAuthenticated();
        } else if (response.status === 404) {
          setSsoAvailable(false);
          setShowTokenLogin(true);
        }
      } catch {
        // The token form remains available if SSO discovery is unreachable.
      }
    }

    void initAuth().finally(() => {
      if (alive) setChecking(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  function handleOpenRunModal(prefill?: RunModalPrefill): void {
    setModalPrefill(prefill);
    setShowRunModal(true);
  }

  function handleCloseRunModal(): void {
    setShowRunModal(false);
    setModalPrefill(undefined);
  }

  function handleLogin(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const token = tokenInput.trim();
    const username = usernameInput.trim();
    if (token) {
      setToken(token);
      setTokenInput("");
    } else if (username && passwordInput) {
      setToken(btoa(`${username}:${passwordInput}`));
      setUsernameInput("");
      setPasswordInput("");
    }
  }

  async function handleLogout(): Promise<void> {
    clearToken();
    await fetch("/auth/logout", { credentials: "include" }).catch(() => undefined);
    window.location.href = "/";
  }

  const runModal = useMemo(() => ({ open: handleOpenRunModal }), []);

  if (checking) {
    return (
      <Center className="min-h-screen bg-gray-50 dark:bg-gray-900">
        <Stack align="center" gap="sm">
          <div className="text-sm text-gray-500 dark:text-gray-400">Loading…</div>
        </Stack>
      </Center>
    );
  }

  if (needsToken) {
    return (
      <Center className="min-h-screen bg-gray-50 dark:bg-gray-900 px-4">
        <Paper shadow="md" radius="md" p="xl" className="w-full max-w-md dark:bg-gray-800">
          <Stack gap="md">
            <Title order={2}>Props</Title>
            {ssoAvailable && (
              <Button component="a" href="/auth/login" fullWidth>
                Sign in with Authentik
              </Button>
            )}
            {showTokenLogin ? (
              <>
                {ssoAvailable && (
                  <Group gap="sm" wrap="nowrap">
                    <Divider className="flex-1" />
                    <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
                      or use a token
                    </Text>
                    <Divider className="flex-1" />
                  </Group>
                )}
                <form onSubmit={handleLogin}>
                  <Stack gap="sm">
                    <Stack gap="xs" className={tokenHasInput ? "opacity-40" : ""}>
                      <TextInput
                        placeholder="Username"
                        autoComplete="username"
                        disabled={tokenHasInput}
                        value={usernameInput}
                        onChange={(event) => setUsernameInput(event.currentTarget.value)}
                      />
                      <PasswordInput
                        placeholder="Password"
                        autoComplete="current-password"
                        disabled={tokenHasInput}
                        value={passwordInput}
                        onChange={(event) => setPasswordInput(event.currentTarget.value)}
                      />
                    </Stack>
                    <Group gap="sm" wrap="nowrap">
                      <Divider className="flex-1" />
                      <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
                        or token
                      </Text>
                      <Divider className="flex-1" />
                    </Group>
                    <TextInput
                      placeholder="base64 token"
                      disabled={credsHaveInput}
                      className={credsHaveInput ? "opacity-40" : ""}
                      value={tokenInput}
                      onChange={(event) => setTokenInput(event.currentTarget.value)}
                    />
                    <Button type="submit">Sign in</Button>
                  </Stack>
                </form>
              </>
            ) : (
              <Button variant="subtle" color="gray" onClick={() => setShowTokenLogin(true)}>
                Use a token instead
              </Button>
            )}
          </Stack>
        </Paper>
      </Center>
    );
  }

  return (
    <RunModalContext.Provider value={runModal}>
      <AppShell header={{ height: 60 }}>
        <AppShell.Header px="md" className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
          <Group h="100%" justify="space-between" wrap="nowrap">
            <Title order={3} m={0}>
              <Anchor component={Link} to="/" c="inherit" underline="never">
                Props
              </Anchor>
            </Title>
            <Group component="nav" gap={4}>
              {navItems.map(({ path, label }) => (
                <Button
                  key={path}
                  component={Link}
                  to={path}
                  size="sm"
                  variant={isActive(path, pathname) ? "light" : "subtle"}
                >
                  {label}
                </Button>
              ))}
            </Group>
            <Group gap="sm">
              {userEmail && (
                <Text size="xs" c="dimmed">
                  {userEmail}
                </Text>
              )}
              <Button variant="subtle" color="gray" size="sm" onClick={() => void handleLogout()}>
                Logout
              </Button>
            </Group>
          </Group>
        </AppShell.Header>
        <AppShell.Main className="bg-gray-50 dark:bg-gray-900 p-6">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:runId" element={<RunRoute />} />
            <Route path="/definitions/:definitionId" element={<DefinitionRoute />} />
            <Route path="/examples" element={<ExamplesPage />} />
            <Route path="/snapshots" element={<SnapshotsPage />} />
            <Route path="/snapshots/*" element={<SnapshotRoute />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </AppShell.Main>
      </AppShell>
      <RunTriggerModal open={showRunModal} onClose={handleCloseRunModal} prefill={modalPrefill} />
    </RunModalContext.Provider>
  );
}

export default function App(): JSX.Element {
  return (
    <MantineProvider defaultColorScheme="auto">
      <Notifications position="top-right" autoClose={8000} />
      <HashRouter>
        <PropsApp />
      </HashRouter>
    </MantineProvider>
  );
}
