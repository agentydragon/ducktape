import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import "../session-viewer.css";
import { SessionViewer } from "../session-viewer";
import { sessionFixtureApi } from "../fixtures/session-data";

const FIXED_NOW = Date.parse("2026-10-28T17:00:00Z");
Date.now = () => FIXED_NOW;

createRoot(document.getElementById("root")!).render(
  <MantineProvider defaultColorScheme="light">
    <div style={{ height: "100dvh", boxSizing: "border-box", padding: 16 }}>
      <SessionViewer api={sessionFixtureApi} />
    </div>
  </MantineProvider>
);
