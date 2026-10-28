import { Container } from "@mantine/core";
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { SessionViewer } from "./session-viewer";

export function App(): JSX.Element {
  return (
    <Container fluid p="md" style={{ height: "100dvh", boxSizing: "border-box" }}>
      <Routes>
        <Route path="/" element={<SessionViewer />} />
        <Route path="/sessions/:sessionId" element={<SessionViewer />} />
      </Routes>
    </Container>
  );
}
