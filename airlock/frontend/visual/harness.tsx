import "@mantine/core/styles.css";

import { Alert, Anchor, Container, MantineProvider, Stack, Title } from "@mantine/core";
import { type JSX } from "react";
import { createRoot } from "react-dom/client";

import App from "../App";
import type { DeploymentInfo, OAuthProviderStatus } from "../types";
import { SCENARIOS } from "./scenarios.mjs";

const DEPLOYMENT_INFO: DeploymentInfo = {
  image_tag: "devel-20260529194300-3b9e37c",
  source_commit: "3b9e37c50911c40c11a51903de961c2db0f50f59",
  source_commit_url: "https://github.com/agentydragon/ducktape/commit/3b9e37c50911c40c11a51903de961c2db0f50f59",
};

const OAUTH_PROVIDERS: OAuthProviderStatus[] = [
  {
    name: "google",
    display_name: "Google",
    provider_type: "oauth2",
    requested_scopes: ["email", "profile"],
    status: { state: "connected", expires_at: "2025-01-16T10:30:00Z", scope: "email profile" },
  },
  {
    name: "bsc",
    display_name: "Blue Shield of California (FHIR sandbox)",
    provider_type: "oauth2",
    requested_scopes: ["openid", "interop", "PatientEOB", "PatientRead"],
    status: { state: "connected", expires_at: "2026-08-01T10:30:00Z", scope: "openid interop" },
  },
  {
    name: "drive",
    display_name: "Google Drive",
    provider_type: "oauth2",
    requested_scopes: ["drive.readonly"],
    status: {
      state: "expired",
      expires_at: "2025-01-15T08:00:00Z",
      scope: "drive.readonly",
      last_refresh_error: "ClientResponseError('invalid_grant: Token has been expired or revoked.', status=400)",
    },
  },
];

function HarnessIndex({ pages, error }: { pages: string[]; error: string | null }): JSX.Element {
  return (
    <Container py="xl">
      <Stack>
        {error && (
          <Alert color="red" title="Unknown page">
            {error}
          </Alert>
        )}
        <Title order={1}>Airlock Visual Test Harness</Title>
        {pages.map((name) => (
          <Anchor key={name} href={"/?page=" + name}>
            {name}
          </Anchor>
        ))}
      </Stack>
    </Container>
  );
}

const params = new URLSearchParams(window.location.search);
const scenarioName = params.get("page");
const appElement = document.getElementById("app");
if (!appElement) throw new Error("No #app element");

const showApp = scenarioName !== null && Object.prototype.hasOwnProperty.call(SCENARIOS, scenarioName);
if (showApp) {
  window.fetch = async (input: RequestInfo | URL): Promise<Response> => {
    const requestUrl = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
    const url = new URL(requestUrl, window.location.href);
    const json = (data: unknown): Response =>
      new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });

    if (url.pathname === "/api/oauth/providers") return json(OAUTH_PROVIDERS);
    if (url.pathname === "/api/info") return json(DEPLOYMENT_INFO);
    throw new Error("Unmocked fetch: " + url);
  };
}

createRoot(appElement).render(
  <MantineProvider defaultColorScheme="auto">
    {showApp ? <App /> : <HarnessIndex pages={Object.keys(SCENARIOS)} error={scenarioName} />}
  </MantineProvider>
);
