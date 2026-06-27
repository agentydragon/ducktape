import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { createRoot } from "react-dom/client";

import App from "./app.tsx";
import { hakuTheme } from "./theme.ts";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto" theme={hakuTheme}>
    <Notifications position="top-right" />
    <App />
  </MantineProvider>
);
