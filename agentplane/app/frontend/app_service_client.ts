import { fetchText } from "./client";

export function createAppServiceClient(basePath: string) {
  return {
    getText(path: string): Promise<string> {
      return fetchText(`${basePath}${path}`, { headers: { Accept: "application/json" } });
    },
  };
}
