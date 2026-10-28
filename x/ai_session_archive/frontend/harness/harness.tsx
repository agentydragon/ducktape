import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";

import "../session-viewer.css";
import { App } from "../app";
import { sessionFixtureApi } from "../fixtures/session-data";

const FIXED_NOW = Date.parse("2026-10-28T17:00:00Z");
Date.now = () => FIXED_NOW;

createRoot(document.getElementById("root")!).render(
  <MantineProvider defaultColorScheme="light">
    <MemoryRouter>
      <App api={sessionFixtureApi} />
    </MemoryRouter>
  </MantineProvider>
);
