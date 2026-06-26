import { useState } from "react";
import { Button, Modal } from "@mantine/core";

// Embeds Haku's own UI service — a separate, Authentik-gated origin running in
// haku-sandbox — in a sandboxed cross-origin iframe. The console never renders
// Haku's UI itself; it only frames this origin (the backend CSP frame-src is what
// permits the embed). `allow-same-origin` keeps the framed app on its OWN origin (a
// different subdomain, so NOT the console's) so its Authentik session cookie works;
// being cross-origin, it cannot reach into the parent. No `allow="fullscreen"` and no
// allow-top-navigation, so it stays contained. See plans/free_form_ui_iframe.md.
export function HakuUiButton({ uiUrl }: { uiUrl: string | null | undefined }) {
  const [opened, setOpened] = useState(false);
  if (!uiUrl) return null;
  return (
    <>
      <Button variant="default" onClick={() => setOpened(true)}>
        Haku UI
      </Button>
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title="Haku UI"
        size="90%"
        styles={{ body: { height: "80vh", padding: 0 } }}
      >
        <iframe
          src={uiUrl}
          title="Haku UI"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          style={{ width: "100%", height: "100%", border: 0 }}
        />
      </Modal>
    </>
  );
}
