import { main } from '../../../util/frontend_visual_test/visual-test-lib.mjs';

await main('ListPage', import.meta.url, {
  baselineName: 'ListPage_mobile',
  viewport: { width: 375, height: 812 },
});
