import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import { App } from "./app";
import "@mantine/core/styles.css";
import "./board.css";
import "./page.css";

createRoot(document.getElementById("root")!).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
