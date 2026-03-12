import { main } from '../../../util/testing/frontend_visual/visual-test-lib.mjs';

const page = process.env.VISUAL_TEST_PAGE;
const baselineName = process.env.VISUAL_TEST_BASELINE;
const colorScheme = process.env.VISUAL_TEST_COLOR_SCHEME || 'light';
const viewportWidth = parseInt(process.env.VISUAL_TEST_VIEWPORT_W || '1200', 10);
const viewportHeight = parseInt(process.env.VISUAL_TEST_VIEWPORT_H || '800', 10);

if (!page || !baselineName) {
  console.error('VISUAL_TEST_PAGE and VISUAL_TEST_BASELINE env vars are required');
  process.exit(1);
}

const options = { baselineName, colorScheme };
if (viewportWidth !== 1200 || viewportHeight !== 800) {
  options.viewport = { width: viewportWidth, height: viewportHeight };
}

await main(page, import.meta.url, options);
