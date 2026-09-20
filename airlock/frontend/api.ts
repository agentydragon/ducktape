/** Authenticated REST client for the Airlock OAuth credential broker. */
import { isAuthenticationFailurePage, redirectToLogin } from "./auth";
import type { DeploymentInfo, OAuthProviderStatus } from "./types";

async function apiFetch<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401) {
    if (isAuthenticationFailurePage()) throw new Error("Sign-in failed. Please try again.");
    redirectToLogin();
    throw new Error("Redirecting to login");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`API error ${response.status}: ${text}`);
  }
  return response.json();
}

export class AirlockApiClient {
  async listOAuthProviders(): Promise<OAuthProviderStatus[]> {
    return apiFetch<OAuthProviderStatus[]>("/api/oauth/providers");
  }

  async getDeploymentInfo(): Promise<DeploymentInfo> {
    return apiFetch<DeploymentInfo>("/api/info");
  }
}

let client: AirlockApiClient | null = null;

export function getApiClient(): AirlockApiClient {
  client ??= new AirlockApiClient();
  return client;
}
