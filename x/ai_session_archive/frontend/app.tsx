import { Container } from "@mantine/core";
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import type { SessionBrowserApi } from "./api";
import { SessionViewer } from "./session-viewer";

export function App({ api }: { api?: SessionBrowserApi }): JSX.Element {
  return (
    <Container fluid p="md" style={{ height: "100dvh", boxSizing: "border-box" }}>
      <Routes>
        <Route path="/" element={<SessionViewer api={api} />} />
        <Route path="/sessions/:sessionId" element={<SessionViewer api={api} />} />
      </Routes>
    </Container>
  );
}
