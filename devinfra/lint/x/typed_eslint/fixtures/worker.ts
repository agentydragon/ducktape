import { loadValue } from "./api";

export async function notifyClients(scope: ServiceWorkerGlobalScope): Promise<void> {
  const value = await loadValue();
  for (const client of await scope.clients.matchAll()) client.postMessage(value);
}
