import { Container } from "@mantine/core";
import { useEffect, useState } from "react";

import { SandboxPage } from "./sandbox_page";
import { SandboxList } from "./sandboxes";
import { SessionView } from "./session";

type Route =
  | { view: "list" }
  | { view: "sandbox"; name: string }
  | { view: "session"; name: string; sessionId: string };

function parseHash(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "sandboxes" && parts[1] && parts[2] === "sessions" && parts[3]) {
    return { view: "session", name: parts[1], sessionId: parts[3] };
  }
  if (parts[0] === "sandboxes" && parts[1]) return { view: "sandbox", name: parts[1] };
  return { view: "list" };
}

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = (): void => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

function go(path: string): void {
  window.location.hash = path;
}

export default function App(): JSX.Element {
  const route = useRoute();
  return (
    <Container size="xl" py="md">
      {route.view === "list" && <SandboxList onOpen={(name) => go(`/sandboxes/${encodeURIComponent(name)}`)} />}
      {route.view === "sandbox" && (
        <SandboxPage
          name={route.name}
          onBack={() => go("/")}
          onOpenSession={(sessionId) =>
            go(`/sandboxes/${encodeURIComponent(route.name)}/sessions/${encodeURIComponent(sessionId)}`)
          }
        />
      )}
      {route.view === "session" && (
        <SessionView
          sandbox={route.name}
          sessionId={route.sessionId}
          onBack={() => go(`/sandboxes/${encodeURIComponent(route.name)}`)}
        />
      )}
    </Container>
  );
}
