// Visual regression harness for the Airlock OAuth credential broker.
import "../../app.css";
import { mount } from "svelte";
import App from "../../App.svelte";
import HarnessIndex from "./HarnessIndex.svelte";
import type { OAuthProviderStatus } from "../../types.ts";

const DEPLOYMENT_INFO = {
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

const pages = { OAuthPage: {} };
const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");
const appElement = document.getElementById("app")!;

if (pageName && pageName in pages) {
  const authority = "https://mock-auth";
  const clientId = "mock-client";
  sessionStorage.setItem(
    `oidc.user:${authority}:${clientId}`,
    JSON.stringify({
      id_token: "mock-id-token",
      access_token: "mock-access-token",
      token_type: "Bearer",
      scope: "openid profile email",
      profile: { iss: authority, sub: "mock-user", aud: clientId, exp: 9999999999, iat: 1700000000 },
      expires_at: 9999999999,
    })
  );

  window.fetch = async (input: RequestInfo | URL): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const pathname = url.startsWith("/") ? url.replace(/\?.*$/, "") : new URL(url).pathname;
    const json = (data: unknown): Response =>
      new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });

    if (pathname === "/auth/config") {
      return json({ authority, client_id: clientId, redirect_uri: "http://localhost/auth/callback" });
    }
    if (pathname === "/api/oauth/providers") return json(OAUTH_PROVIDERS);
    if (pathname === "/api/info") return json(DEPLOYMENT_INFO);
    throw new Error(`Unmocked fetch: ${url}`);
  };

  mount(App, { target: appElement });
} else {
  mount(HarnessIndex, {
    target: appElement,
    props: { pages: Object.keys(pages), error: pageName },
  });
}
