import assert from "node:assert/strict";

import { launchDeterministicBrowser } from "./launcher.mjs";

async function platformFamilies(page, selector) {
  const client = await page.createCDPSession();
  await client.send("DOM.enable");
  await client.send("CSS.enable");
  const { root } = await client.send("DOM.getDocument");
  const { nodeId } = await client.send("DOM.querySelector", { nodeId: root.nodeId, selector });
  const { fonts } = await client.send("CSS.getPlatformFontsForNode", { nodeId });
  await client.detach();
  return fonts.map(({ familyName }) => familyName);
}

const browser = await launchDeterministicBrowser();
try {
  const page = await browser.newPage();
  await page.setContent(`
    <style>
      #serif { font-family: serif; }
      #sans { font-family: sans-serif; }
      #mono { font-family: monospace; }
      #system { font-family: system-ui; }
      #explicit { font-family: "Liberation Mono"; }
    </style>
    <div id="serif">Serif sample</div>
    <div id="sans">Sans sample</div>
    <div id="mono">Mono sample</div>
    <div id="system">System sample</div>
    <pre id="pre"><code id="code">const answer = 42;</code></pre>
    <div id="explicit">Explicit sample</div>
  `);

  const expected = [
    ["#serif", "Liberation Serif"],
    ["#sans", "Liberation Sans"],
    ["#mono", "Liberation Mono"],
    ["#system", "Liberation Sans"],
    ["#pre", "Liberation Mono"],
    ["#code", "Liberation Mono"],
    ["#explicit", "Liberation Mono"],
  ];
  for (const [selector, family] of expected) {
    const families = await platformFamilies(page, selector);
    assert.ok(families.includes(family), `${selector} used ${families.join(", ")}, expected ${family}`);
  }
  await page.close();
} finally {
  await browser.close();
}

console.log("font_environment.test.mjs passed");
