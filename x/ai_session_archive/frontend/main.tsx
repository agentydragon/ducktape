import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./app";
import "./session-viewer.css";

createRoot(document.getElementById("root")!).render(
  <MantineProvider defaultColorScheme="auto">
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </MantineProvider>
);
