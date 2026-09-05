import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

// What each harness page still has to load once it has mounted. Both of App.svelte's views fetch
// their own data in `onMount` and render nothing of it until that lands: a provider's authorize
// link exists only once /api/oauth/providers has answered, and the footer only once /api/info has.
// A page added to the harness states its own conditions here.
const READY_SELECTORS = {
  OAuthPage: ['a[href^="/oauth/authorize/"]', "footer"],
};

const page = process.env.VISUAL_TEST_PAGE;
const outputName = process.env.VISUAL_TEST_OUTPUT_NAME;
const colorScheme = process.env.VISUAL_TEST_COLOR_SCHEME || "light";
const viewportWidth = parseInt(process.env.VISUAL_TEST_VIEWPORT_W || "1200", 10);
const viewportHeight = parseInt(process.env.VISUAL_TEST_VIEWPORT_H || "800", 10);

if (!page || !outputName) {
  console.error("VISUAL_TEST_PAGE and VISUAL_TEST_OUTPUT_NAME env vars are required");
  process.exit(1);
}

const readySelectors = READY_SELECTORS[page];
if (!readySelectors) {
  console.error(`no ready selectors for page ${JSON.stringify(page)} — add them to READY_SELECTORS`);
  process.exit(1);
}

// OAuthPage is a genuine full-page view.
const options = { element: "#app", outputName, colorScheme, readySelectors };
if (viewportWidth !== 1200 || viewportHeight !== 800) {
  options.viewport = { width: viewportWidth, height: viewportHeight };
}

await main(page, options);
