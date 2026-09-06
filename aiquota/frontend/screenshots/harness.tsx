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

import { Dashboard } from "../dashboard";
import type { QuotasView } from "../quotas";
import scenes from "../fixtures/scenes.json";

import "../styles.css";

// A JSON module's `kind: "success"` widens to `string`, which no amount of shape agreement
// will make assignable to the tagged union — hence the cast rather than a plain annotation.
const SCENES = scenes as unknown as Record<string, QuotasView>;

const name = new URLSearchParams(globalThis.location.search).get("scene") ?? "";
const quotas = SCENES[name];
if (!quotas) throw new Error(`unknown scene ${JSON.stringify(name)}; have ${Object.keys(SCENES).join(", ")}`);

const root = document.getElementById("root");
if (!root) throw new Error("harness page has no #root");

createRoot(root).render(
  <Dashboard
    quotas={quotas}
    now={Date.parse(quotas.fetched_at)}
    error={null}
    refreshing={false}
    onRefresh={() => undefined}
  />
);
