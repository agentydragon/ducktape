import { QueryClient } from "@tanstack/react-query";

export function createConsoleQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        networkMode: "always",
        gcTime: 0,
      },
    },
  });
}
