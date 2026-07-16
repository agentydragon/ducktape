// Shared mount for per-server preview screenshots. Each server's `preview_harness.tsx` imports
// this and passes its fixtures; render.mjs (in this dir) drives one page load per fixture ×
// variant × color scheme and screenshots the card.
//
// Each server harness imports its own `preview_mock.ts` before this module, so the widget graph
// sees only that server's canned MCP responses.

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import { hakuTheme } from "../../theme.ts";
import type { PreviewVariant } from "../vocabulary.tsx";
import { PreviewCard, previewFixtureSlugs, type PreviewFixture } from "./card.tsx";

// Exposed so render.mjs can name/label each fixture's PNGs without duplicating the slug logic.
const PREVIEW_WINDOW = window as unknown as {
  __PREVIEW_FIXTURES__?: { slug: string; label: string }[];
  __FIXTURE__?: number;
  __VARIANT__?: PreviewVariant;
  __COLOR_SCHEME__?: "light" | "dark";
};

export function mountPreviewCards(fixtures: PreviewFixture[]): void {
  PREVIEW_WINDOW.__PREVIEW_FIXTURES__ = previewFixtureSlugs(fixtures);

  const fixtureIndex = PREVIEW_WINDOW.__FIXTURE__ ?? 0;
  const variant = PREVIEW_WINDOW.__VARIANT__ ?? "compact";
  const colorScheme = PREVIEW_WINDOW.__COLOR_SCHEME__ ?? "light";
  const fixture = fixtures[fixtureIndex];
  if (!fixture) throw new Error(`no preview fixture at index ${fixtureIndex}`);

  const container = document.getElementById("app");
  if (!container) throw new Error("missing #app");
  createRoot(container).render(
    <MantineProvider forceColorScheme={colorScheme} theme={hakuTheme}>
      <PreviewCard fixture={fixture} variant={variant} />
    </MantineProvider>
  );
}
