import { Box, Stack } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { type JSX, useState } from "react";
import { MemoryRouter } from "react-router";

import type { ActionRequest } from "../../../../action_service/api/contracts/action_service_pb";
import { ActionHistoryCard } from "../history";
import { ActionRequestsContext, PendingActionCard } from "../requests";
import { PendingActionList } from "../sidebar";
import { createAppQueryClient } from "../../transport/query";

export function ActionPresentationsVisual({
  pending,
  receipt,
  sidebarWidth,
  detailWidth,
  mcp,
}: {
  pending: ActionRequest;
  receipt: ActionRequest;
  sidebarWidth: number;
  detailWidth: number;
  mcp: boolean;
}): JSX.Element {
  const [queryClient] = useState(createAppQueryClient);
  const decide = () => {
    throw new Error("Action presentation fixtures do not execute decisions.");
  };
  const actions = { requests: [pending], error: null, loading: false, deciding: null, decide, queryClient };
  const frame = { p: 6, maw: "100%" } as const;
  return (
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ActionRequestsContext.Provider value={actions}>
          <Stack align="center" gap="xs">
            <Box {...frame} w={sidebarWidth + 12} data-action-visual="sidebar">
              <PendingActionList pending={actions.requests} />
            </Box>
            <Box {...frame} w={detailWidth + 12} data-action-visual="approval">
              <PendingActionCard request={pending} deciding={false} onDecide={decide} />
            </Box>
            <Box {...frame} w={detailWidth + 12} data-action-visual="receipt">
              <ActionHistoryCard request={receipt} mcp={mcp} />
            </Box>
            <Box {...frame} w={detailWidth + 12} data-action-visual="history">
              <ActionHistoryCard request={receipt} mcp={mcp} compact />
            </Box>
          </Stack>
        </ActionRequestsContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>
  );
}
