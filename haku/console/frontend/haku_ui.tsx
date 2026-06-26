// Haku's own UI service — a separate, Authentik-gated origin running in
// haku-sandbox — embedded in a sandboxed cross-origin iframe as the "Free-form UI"
// tab. The console never renders Haku's UI itself; it only frames this origin (the
// backend CSP frame-src is what permits the embed). `allow-same-origin` keeps the
// framed app on its OWN origin (a different subdomain, so NOT the console's) so its
// Authentik session cookie works; being cross-origin, it cannot reach into the
// parent. No `allow="fullscreen"` and no allow-top-navigation, so it stays
// contained. See plans/free_form_ui_iframe.md.
export function HakuUiFrame({ uiUrl }: { uiUrl: string }) {
  return (
    <iframe
      src={uiUrl}
      title="Haku UI"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
      className="mt-4 w-full"
      style={{ height: "80vh", border: 0 }}
    />
  );
}
