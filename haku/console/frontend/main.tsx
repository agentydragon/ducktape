import { QueryClientProvider } from "@tanstack/react-query";

import { createConsoleQueryClient } from "./query";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { createRoot } from "react-dom/client";

import App from "./app";
import { hakuTheme } from "./theme";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
const client = createConsoleQueryClient();
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto" theme={hakuTheme}>
    <Notifications position="top-right" />
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </MantineProvider>
);
