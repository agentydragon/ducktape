import { main } from '../../../util/frontend_visual_test/visual-test-lib.mjs';

await main('DetailPage', import.meta.url, {
  baselineName: 'DetailPage_mobile',
  viewport: { width: 375, height: 812 },
});
