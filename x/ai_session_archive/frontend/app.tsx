import { Container } from "@mantine/core";
import type { JSX } from "react";
import { Route, Routes, useLocation } from "react-router";

import type { SessionBrowserApi } from "./api";
import { MachineImportProgressPage } from "./import-progress";
import { SessionViewer } from "./session-viewer";

export function App({ api }: { api?: SessionBrowserApi }): JSX.Element {
  return (
    <Container fluid p="md" style={{ height: "100dvh", boxSizing: "border-box" }}>
      <Routes>
        <Route path="/" element={<SessionArchiveHome api={api} />} />
        <Route path="/sessions/:sessionId" element={<SessionViewer api={api} />} />
      </Routes>
    </Container>
  );
}

function SessionArchiveHome({ api }: { api?: SessionBrowserApi }): JSX.Element {
  const location = useLocation();
  return new URLSearchParams(location.search).get("view") === "import-progress" ? (
    <MachineImportProgressPage api={api} />
  ) : (
    <SessionViewer api={api} />
  );
}
