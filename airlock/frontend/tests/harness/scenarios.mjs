/**
 * Every visual scenario the harness can mount, and how the sweep captures each.
 *
 * The one list: harness.ts mounts from it and tests/visual_test_runner.mjs sweeps it, so BUILD
 * names no scenario. Plain `.mjs` rather than `.ts` because both readers need it and only one of
 * them is compiled -- the harness is bundled by esbuild, the runner is run by node.
 */

/** An iPhone X's CSS viewport: the broker's consent screen is reached from a phone. */
const PHONE = { width: 375, height: 812 };

// What the page still has to load once it has mounted. Both of App.svelte's views fetch their own
// data in `onMount` and render nothing of it until that lands: a provider's authorize link exists
// only once /api/oauth/providers has answered, and the footer only once /api/info has.
const OAUTH_READY = ['a[href^="/oauth/authorize/"]', "footer"];

/**
 * Four scenarios over the app's one harness page, varying theme and viewport. `element: "#app"`
 * throughout, because the page under test is a genuine full-page view.
 */
export const SCENARIOS = {
  OAuthPage: { element: "#app", readySelectors: OAUTH_READY },
  OAuthPage_dark: { element: "#app", readySelectors: OAUTH_READY, colorScheme: "dark" },
  OAuthPage_mobile: { element: "#app", readySelectors: OAUTH_READY, viewport: PHONE },
  OAuthPage_mobile_dark: { element: "#app", readySelectors: OAUTH_READY, viewport: PHONE, colorScheme: "dark" },
};
