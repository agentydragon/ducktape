/** Authenticated REST client for the Airlock OAuth credential broker. */
import { getAccessToken } from "./auth.ts";
import type { DeploymentInfo, OAuthProviderStatus } from "./types.ts";

async function apiFetch<T>(path: string): Promise<T> {
  const token = await getAccessToken();
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
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
