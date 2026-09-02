import { Container } from "@mantine/core";
import { HashRouter, Route, Routes, useNavigate, useParams } from "react-router";

import { SandboxPage } from "./sandbox_page";
import { SandboxList } from "./sandboxes";
import { SessionView } from "./session";

// Hash routing: the API serves the bundle at "/" only, so no path has to reach the server.
function sandboxPath(name: string): string {
  return `/sandboxes/${encodeURIComponent(name)}`;
}

function sessionPath(name: string, sessionId: string): string {
  return `${sandboxPath(name)}/sessions/${encodeURIComponent(sessionId)}`;
}

function required(value: string | undefined, name: string): string {
  if (value === undefined) throw new Error(`route parameter ${name} is missing`);
  return value;
}

function ListRoute(): JSX.Element {
  const navigate = useNavigate();
  return <SandboxList onOpen={(name) => void navigate(sandboxPath(name))} />;
}

function SandboxRoute(): JSX.Element {
  const name = required(useParams().name, "name");
  const navigate = useNavigate();
  return (
    <SandboxPage
      name={name}
      onBack={() => void navigate("/")}
      onOpenSession={(sessionId) => void navigate(sessionPath(name, sessionId))}
    />
  );
}

function SessionRoute(): JSX.Element {
  const params = useParams();
  const name = required(params.name, "name");
  const navigate = useNavigate();
  return (
    <SessionView
      sandbox={name}
      sessionId={required(params.sessionId, "sessionId")}
      onBack={() => void navigate(sandboxPath(name))}
    />
  );
}

export default function App(): JSX.Element {
  return (
    <HashRouter>
      <Container size="xl" py="md">
        <Routes>
          <Route path="/" element={<ListRoute />} />
          <Route path="/sandboxes/:name" element={<SandboxRoute />} />
          <Route path="/sandboxes/:name/sessions/:sessionId" element={<SessionRoute />} />
          <Route path="*" element={<ListRoute />} />
        </Routes>
      </Container>
    </HashRouter>
  );
}
