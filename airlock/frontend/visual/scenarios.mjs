/**
 * Visual scenarios for Airlock, swept by visual/runner.mjs over one browser.
 *
 * The four captures cover both supported color schemes at desktop and phone widths.
 */

const PHONE = { width: 375, height: 812 };
const OAUTH_READY = ['form[action^="/oauth/authorize/"] button', "footer"];

export const SCENARIOS = {
  OAuthPage: { element: "#app", readySelectors: OAUTH_READY },
  OAuthPage_dark: { element: "#app", readySelectors: OAUTH_READY, colorScheme: "dark" },
  OAuthPage_mobile: { element: "#app", readySelectors: OAUTH_READY, viewport: PHONE },
  OAuthPage_mobile_dark: { element: "#app", readySelectors: OAUTH_READY, viewport: PHONE, colorScheme: "dark" },
};
