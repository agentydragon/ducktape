import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { createRoot } from "react-dom/client";

import App from "./app.tsx";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto">
    <Notifications position="top-right" />
    <App />
  </MantineProvider>
);
