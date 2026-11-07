import { fetchWithLogin, HttpError } from "./client";

export function createAppServiceClient(basePath: string) {
  return {
    async getText(path: string): Promise<string> {
      const response = await fetchWithLogin(`${basePath}${path}`, { headers: { Accept: "application/json" } });
      const text = await response.text();
      if (!response.ok) {
        let payload: unknown;
        try {
          payload = JSON.parse(text);
        } catch {
          payload = text;
        }
        throw new HttpError(response, payload);
      }
      return text;
    },
  };
}
