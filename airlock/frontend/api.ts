/** Authenticated REST client for the Airlock OAuth credential broker. */
import { getAccessToken, markAuthenticationAccepted, recoverAfterUnauthorized } from "./auth";
import type { DeploymentInfo, OAuthProviderStatus } from "./types";

async function apiFetch<T>(path: string): Promise<T> {
  const token = await getAccessToken();
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401 && (await recoverAfterUnauthorized())) {
    throw new Error("Redirecting to login");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`API error ${response.status}: ${text}`);
  }
  markAuthenticationAccepted();
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
