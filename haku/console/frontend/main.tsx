import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "./app.tsx";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
createRoot(container).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
