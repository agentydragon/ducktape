import { Container } from "@mantine/core";
import type { JSX } from "react";

import { SessionViewer } from "./session-viewer";

export function App(): JSX.Element {
  return (
    <Container fluid p="md" style={{ height: "100dvh", boxSizing: "border-box" }}>
      <SessionViewer />
    </Container>
  );
}
