import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { LineController } from "chart.js";

const require = createRequire(import.meta.url);
const formats = [
  ["ESM", LineController],
  ["CommonJS", require("chart.js").LineController],
];

for (const [format, Controller] of formats) {
  const overflow = ({ previous, target = 2, active = false, showLine = true, pointSize = 0, count = 2 } = {}) => {
    const modes = [];
    const controller = {
      _cachedMeta: {
        dataset: { active, options: previous === undefined ? undefined : { borderWidth: previous } },
        data: Array.from({ length: count }, () => ({ size: () => pointSize })),
      },
      options: { showLine },
      resolveDatasetElementOptions(mode) {
        modes.push(mode);
        return { borderWidth: target };
      },
      resolveDataElementOptions() {
        return {};
      },
    };
    const actual = Controller.prototype.getMaxOverflow.call(controller);
    assert.deepEqual(modes, [active ? "active" : "default"]);
    return actual;
  };

  test(`${format}: initial and repeated layouts use the same target border`, () => {
    assert.equal(overflow(), 1);
    assert.equal(overflow({ previous: 2 }), 1);
    assert.equal(overflow({ previous: 2, target: 0 }), 0);
    assert.equal(overflow({ previous: 0, target: 0 }), 0);
    assert.equal(overflow({ previous: 0, target: 6 }), 3);
  });

  test(`${format}: active line target and visibility govern overflow`, () => {
    assert.equal(overflow({ previous: 2, target: 6, active: true }), 3);
    assert.equal(overflow({ previous: 6, target: 2 }), 1);
    assert.equal(overflow({ previous: 6, target: 6, active: true, showLine: false }), 0);
    assert.equal(overflow({ target: 6, showLine: false }), 0);
  });

  test(`${format}: endpoint and empty-series overflow retain their existing rules`, () => {
    assert.equal(overflow({ pointSize: 10 }), 5);
    assert.equal(overflow({ pointSize: 10, showLine: false }), 5);
    assert.equal(overflow({ target: 6, count: 0 }), 6);
    assert.equal(overflow({ target: 6, showLine: false, count: 0 }), 0);
  });
}
