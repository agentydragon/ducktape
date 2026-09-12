/**
 * Types for visual-test-lib.mjs, so a TypeScript scenario table is checked against what the sweep
 * actually reads rather than re-declaring it and hoping the two agree.
 *
 * The lib stays plain JS: it is imported by six `.mjs` runners and compiled by nothing, so a
 * declaration file beside it is how its callers get types without the module itself moving.
 */

/** The viewport a scene renders at. Omitted fields fall back to 1200x800 at scale 1. */
export interface Viewport {
  width: number;
  height: number;
  deviceScaleFactor?: number;
}

/** How one scene is captured. Every field here is read by the sweep; nothing else is. */
export interface ScenarioOptions {
  /**
   * The element to screenshot. Required, never defaulted: `#app` for a scene whose real extent is
   * the viewport, a scene-specific selector (conventionally `#shot`) for one whose subject is a
   * single component, so the crop is that component's own bounding box rather than an arbitrarily
   * large page around it. See https://github.com/agentydragon/ducktape/pull/3343.
   */
  element: string;
  viewport?: Viewport;
  /** Filename stem for the published PNG. Defaults to the scenario's key. */
  outputName?: string;
  /** The `prefers-color-scheme` media feature. Defaults to "light". */
  colorScheme?: "light" | "dark";
  /**
   * The scene's own readiness conditions: what must be on the page before it is the scene at all.
   * `waitForStable` knows about fonts and paint but nothing about a scene's content, so anything
   * arriving after mount needs a selector that exists only once it has.
   */
  readySelectors?: string[];
  /** Screenshot the viewport rather than the element, preserving clipping instead of expanding. */
  captureViewport?: boolean;
}

/** Render every scenario this shard owns on one browser, then exit 0 (pass) or 1 (fail). */
export function runScenarios(
  scenarios: Record<string, ScenarioOptions>,
  options: { title: string }
): Promise<never>;

/** Render a single scenario and exit 0 (pass) or 1 (fail). */
export function main(scenarioName: string, options: ScenarioOptions): Promise<never>;
