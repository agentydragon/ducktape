/**
 * Mounts the dashboard on one generated scene, picked by `?scene=` — nothing on the network.
 *
 * The scenes are the `/v1/quotas` payloads `//aiquota/testing:export_web_fixtures_bin` renders
 * from the shared scenario YAML, the same files the GNOME extension's renders and the CLI's
 * snapshot use, so the three surfaces are reviewed on identical states.
 *
 * "Now" is the scene's own `fetched_at`, which is what the Python renderers used: ages,
 * countdowns and stale tags then read exactly as they do in the terminal, and the render is
 * the same picture on every run.
 */

import { createRoot } from "react-dom/client";

import type { AllQuotasView } from "../api/schema";
import { App } from "../app";
import { Dashboard } from "../dashboard";
import scenes from "../fixtures/scenes.json";

import "../board.css";
import "../page.css";

// A JSON module's `kind: "success"` widens to `string`, which no amount of shape agreement
// will make assignable to the tagged union — hence the cast rather than a plain annotation.
const SCENES = scenes as unknown as Record<string, AllQuotasView>;

const query = new URLSearchParams(globalThis.location.search);
const name = query.get("scene") ?? "";
const quotas = SCENES[name];
if (!quotas) throw new Error(`unknown scene ${JSON.stringify(name)}; have ${Object.keys(SCENES).join(", ")}`);

const root = document.getElementById("root");
if (!root) throw new Error("harness page has no #root");

if (query.has("live")) {
  class FixtureEventSource extends EventTarget {
    static CLOSED = 2;
    readyState = 1;
    onerror: (() => void) | null = null;
    private view = (event: Event): void => {
      this.dispatchEvent(new MessageEvent("view", { data: (event as MessageEvent).data }));
    };
    private error = (): void => {
      this.onerror?.();
    };
    constructor() {
      super();
      document.documentElement.dataset.quotaStream = "connected";
      globalThis.addEventListener("quota-fixture-view", this.view);
      globalThis.addEventListener("quota-fixture-error", this.error);
    }
    close(): void {
      this.readyState = FixtureEventSource.CLOSED;
      delete document.documentElement.dataset.quotaStream;
      globalThis.removeEventListener("quota-fixture-view", this.view);
      globalThis.removeEventListener("quota-fixture-error", this.error);
    }
  }
  globalThis.EventSource = FixtureEventSource as unknown as typeof EventSource;
}

createRoot(root).render(
  query.has("live") ? (
    <App />
  ) : (
    <Dashboard
      quotas={quotas}
      now={Date.parse(quotas.fetched_at)}
      error={null}
      connecting={false}
      onRefresh={() => undefined}
    />
  )
);
